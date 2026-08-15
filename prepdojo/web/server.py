"""PrepDojo Web UI：FastAPI + 无构建静态前端（localhost 单用户）。

LLM 未配置时：判题完全可用；AI 点评 / 八股打分返回明确提示。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config
from ..db import DB
from ..judge import judge_submission
from ..llm import LLMClient, LLMNotConfigured

STATIC_DIR = Path(__file__).resolve().parent / "static"


class SubmitReq(BaseModel):
    problem_id: str
    language: str
    code: str


class GradeReq(BaseModel):
    card_id: str
    answer: str
    style: str = "standard"  # standard / strict / pressure


class FollowupReq(BaseModel):
    card_id: str
    question: str
    answer: str
    context_answer: Optional[str] = None
    style: str = "standard"


class ChatReq(BaseModel):
    messages: list[dict]  # [{"role": "user"/"assistant", "content": "..."}]
    code: Optional[str] = None
    language: str = "python"
    last_submission_id: Optional[int] = None


def _llm(cfg: Config) -> Optional[LLMClient]:
    if not cfg.llm_ready:
        return None
    try:
        return LLMClient(cfg.base_url, cfg.api_key, cfg.model, cfg.timeout, cfg.temperature)
    except LLMNotConfigured:
        return None


def create_app(cfg: Config, db: DB) -> FastAPI:
    app = FastAPI(title="PrepDojo", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def health():
        return {"ok": True, "llm_ready": cfg.llm_ready,
                "model": cfg.model if cfg.llm_ready else None}

    # ---------- 设置：LLM 配置（前端可改，热更新） ----------

    @app.get("/api/llm/config")
    def llm_config():
        masked = ""
        if cfg.api_key:
            k = cfg.api_key
            masked = k[:5] + "***" + k[-4:] if len(k) > 12 else "***"
        return {"base_url": cfg.base_url, "model": cfg.model,
                "api_key_masked": masked, "configured": cfg.llm_ready}

    @app.post("/api/llm/config")
    def llm_config_save(body: dict):
        import yaml as _yaml

        from ..config import CONFIG_PATH, ensure_dirs

        ensure_dirs()
        base_url = (body.get("base_url") or cfg.base_url).strip()
        model = (body.get("model") or cfg.model).strip()
        api_key = body.get("api_key", "").strip()  # 空则保留原 key
        new_key = api_key or cfg.api_key
        data = {"llm": {"api_key": new_key, "base_url": base_url, "model": model,
                        "temperature": cfg.temperature, "timeout": cfg.timeout}}
        CONFIG_PATH.write_text(_yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                               encoding="utf-8")
        # 热更新内存配置
        cfg.api_key, cfg.base_url, cfg.model = new_key, base_url, model
        return {"ok": True, "llm_ready": cfg.llm_ready, "model": cfg.model}

    @app.get("/api/llm/models")
    def llm_models():
        """扫描 base_url 下可用模型列表。"""
        if not cfg.api_key:
            raise HTTPException(400, "请先填写 API key 并保存")
        try:
            resp = httpx.get(f"{cfg.base_url.rstrip('/')}/models",
                             headers={"Authorization": f"Bearer {cfg.api_key}"},
                             timeout=30)
            if resp.status_code == 401:
                raise HTTPException(401, "API key 无效")
            resp.raise_for_status()
            ids = sorted(m.get("id", "") for m in resp.json().get("data", []))
            return {"models": [i for i in ids if i], "current": cfg.model}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"获取模型列表失败: {e}")

    # ---------- 设置：知识库管理 ----------

    @app.get("/api/fs/browse")
    def fs_browse(path: str = "~"):
        """本机目录浏览（localhost 单用户工具）。返回子目录与可导入文件统计。"""
        from ..extract import SUPPORTED_EXT

        p = Path(path).expanduser()
        if not p.exists():
            raise HTTPException(404, f"路径不存在: {p}")
        if not p.is_dir():
            raise HTTPException(400, "请提供目录路径")
        dirs, importable = [], []
        try:
            for ch in sorted(p.iterdir()):
                if ch.name.startswith("."):
                    continue
                if ch.is_dir():
                    dirs.append({"name": ch.name + "/", "path": str(ch)})
                elif ch.is_file() and ch.suffix.lower() in SUPPORTED_EXT:
                    importable.append({"name": ch.name, "path": str(ch)})
        except PermissionError:
            raise HTTPException(403, "无权限读取该目录")
        return {"current": str(p), "parent": str(p.parent) if str(p) != str(p.anchor) else None,
                "dirs": dirs, "importable_files": importable,
                "importable_count": len(importable)}

    @app.get("/api/sources")
    def list_sources():
        rows = db.execute(
            "SELECT id, path, title, n_cards, ingested_at FROM sources ORDER BY id DESC"
        ).fetchall()
        return {"sources": [
            {"id": r["id"], "title": r["title"] or Path(r["path"]).name,
             "path": r["path"], "n_cards": r["n_cards"],
             "ingested_at": r["ingested_at"]} for r in rows]}

    @app.delete("/api/sources/{sid}")
    def delete_source(sid: int):
        db.execute("DELETE FROM cards WHERE source_id=?", (sid,))
        db.execute("DELETE FROM sources WHERE id=?", (sid,))
        return {"ok": True}

    @app.post("/api/ingest/start")
    async def ingest_start(body: dict):
        """SSE：流式导入知识目录（AI thinking / 输出 / 进度全事件）。"""
        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "缺少 path")
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..ingest import ingest_dir

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue()

            def on_event(kind: str, data: dict) -> None:
                q.put((kind, data))

            def run():
                try:
                    stats = ingest_dir(Path(path), db, cfg, llm,
                                       on_event=on_event, sleep_s=0.05)
                    q.put(("all_done", stats))
                except Exception as e:
                    q.put(("error", {"message": str(e)}))
                finally:
                    q.put(("_end", {}))

            task = loop.run_in_executor(None, run)
            while True:
                kind, data = await loop.run_in_executor(None, q.get)
                if kind == "_end":
                    break
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"
            await task

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/stats")
    def stats():
        return db.stats()

    @app.get("/api/tags")
    def tags():
        return {"tags": db.all_tags()}

    # ---------- 代码题 ----------

    @app.get("/api/problems")
    def problems():
        return {"problems": db.list_problems()}

    @app.get("/api/problems/wrong")
    def wrong_problems():
        """错题本：提交过但从未 AC 的题（AC 即自动移出）。"""
        ids = db.wrong_problem_ids()
        allp = {p["id"]: p for p in db.list_problems()}
        return {"wrong": [allp[i] for i in ids if i in allp]}

    @app.get("/api/problems/{pid}")
    def problem_detail(pid: str):
        p = db.get_problem(pid)
        if not p:
            raise HTTPException(404, "题目不存在")
        return p

    @app.post("/api/submit")
    def submit(req: SubmitReq):
        p = db.get_problem(req.problem_id)
        if not p:
            raise HTTPException(404, "题目不存在")
        if req.language not in p["languages"]:
            raise HTTPException(400, f"该题不支持 {req.language}")
        from ..db import DB as _DB  # noqa: F401  (局部引用避免循环)

        cases = _load_cases(db, req.problem_id)
        res = judge_submission(
            req.code, req.language, cases,
            time_limit_ms=p["time_limit_ms"], mem_limit_mb=p["mem_limit_mb"],
            cpp_compiler=cfg.cpp_compiler,
        )
        case_summary = "\n".join(
            f"用例{c.idx}: {c.verdict} ({c.time_ms}ms)"
            + (f"；期望 {c.expected!r} 实际 {c.stdout!r}" if c.verdict == "WA" else "")
            + (f"；stderr: {c.stderr[:200]}" if c.verdict in ("RE", "MLE") else "")
            for c in res.cases
        )
        sid = db.record_submission(
            req.problem_id, req.language, req.code, res.verdict,
            {"cases": [c.__dict__ for c in res.cases],
             "compile_error": res.compile_error},
            res.max_time_ms,
        )
        return {
            "submission_id": sid, "verdict": res.verdict,
            "max_time_ms": res.max_time_ms,
            "compile_error": res.compile_error,
            "cases": [c.__dict__ for c in res.cases],
            "case_summary": case_summary,
        }

    @app.post("/api/review/{sid}")
    def review(sid: int):
        sub = db.get_submission(sid)
        if not sub:
            raise HTTPException(404, "提交不存在")
        if sub.get("review"):
            return {"review": sub["review"]}
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        p = db.get_problem(sub["problem_id"])
        from ..review import review_code

        cs = sub["detail"].get("cases", [])
        case_summary = "\n".join(f"用例{c.get('idx')}: {c.get('verdict')}" for c in cs[:12])
        try:
            r = review_code(llm, p, sub["code"], sub["language"], sub["verdict"], case_summary)
        except Exception as e:
            raise HTTPException(502, f"点评失败: {e}")
        db.set_review(sid, r)
        return {"review": r}

    # ---------- 八股 ----------

    @app.get("/api/cards/next")
    def cards_next(tags: str = "", n: int = 5, difficulty: int = -1,
                   only_learned: bool = True):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
        cards = db.pick_cards(tags=tag_list, n=n, only_learned=only_learned,
                              difficulty=difficulty if difficulty > 0 else None)
        if not cards and only_learned:
            # 已学池为空：回退到全部卡（提示前端），避免无题可抽
            cards = db.pick_cards(tags=tag_list, n=n,
                                  difficulty=difficulty if difficulty > 0 else None)
            return {"cards": [c0(c) for c in cards], "fallback": True}
        return {"cards": [c0(c) for c in cards], "fallback": False}

    def c0(c):  # 抽题不下发答案，防偷看
        return {k: c[k] for k in ("id", "question", "topic_tags", "difficulty",
                                  "source_ref", "learned")}

    @app.get("/api/cards/learn")
    def cards_learn(tags: str = "", n: int = 10, include_learned: bool = False):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
        cards = db.pick_learn_cards(tags=tag_list, n=n,
                                    only_unlearned=not include_learned)
        return {"cards": cards}  # 学习模式直接给全部字段（含要点与讲解缓存）

    @app.post("/api/cards/{cid}/learn")
    def card_mark_learned(cid: str, body: dict):
        learned = bool(body.get("learned", True))
        if not db.mark_learned(cid, learned):
            raise HTTPException(404, "题卡不存在")
        return {"ok": True, "learned": learned}

    @app.get("/api/cards/progress")
    def cards_progress():
        return db.learn_progress()

    @app.get("/api/cards/{cid}/explain")
    def card_explain(cid: str):
        card = db.get_card(cid)
        if not card:
            raise HTTPException(404, "题卡不存在")
        if card.get("explanation"):  # 缓存直接返回
            return {"explanation": json.loads(card["explanation"]), "cached": True}
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..quiz import explain_card

        try:
            out = explain_card(llm, card, with_reasoning=True)
            result = out["json"]
        except Exception as e:
            raise HTTPException(502, f"讲解生成失败: {e}")
        db.set_explanation(cid, json.dumps(result, ensure_ascii=False))
        return {"explanation": result, "cached": False, "reasoning": out["reasoning"]}

    @app.get("/api/cards/{cid}")
    def card_detail(cid: str):
        c = db.get_card(cid)
        if not c:
            raise HTTPException(404, "题卡不存在")
        return c

    @app.post("/api/quiz/grade")
    def quiz_grade(req: GradeReq):
        card = db.get_card(req.card_id)
        if not card:
            raise HTTPException(404, "题卡不存在")
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..quiz import grade_answer

        try:
            out = grade_answer(llm, card, req.answer, style=req.style, with_reasoning=True)
            result = out["json"]
        except Exception as e:
            raise HTTPException(502, f"打分失败: {e}")
        db.record_attempt(card["id"], card["question"], req.answer,
                          result.get("score", 0), result, mode="web")
        result["reference"] = card["answer_points"]  # 打完分再给参考要点
        result["reasoning"] = out["reasoning"]
        return result

    @app.post("/api/quiz/followup")
    def quiz_followup(req: FollowupReq):
        card = db.get_card(req.card_id)
        if not card:
            raise HTTPException(404, "题卡不存在")
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..quiz import grade_followup

        try:
            out = grade_followup(llm, card, req.question, req.answer,
                                 req.context_answer, style=req.style, with_reasoning=True)
            result = out["json"]
        except Exception as e:
            raise HTTPException(502, f"追问评分失败: {e}")
        db.record_attempt(card["id"], req.question, req.answer,
                          result.get("score", 0), result, mode="follow_up")
        result["reasoning"] = out["reasoning"]
        return result

    # ---------- AI 判题（工具增强的结构化判定报告，SSE） ----------

    @app.post("/api/ai_judge/{pid}")
    async def ai_judge(pid: str, body: dict):
        problem = db.get_problem(pid)
        if not problem:
            raise HTTPException(404, "题目不存在")
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import (AI_JUDGE_SYSTEM, SandboxTools, ai_judge_report,
                            build_problem_context, chat_step)

        code = body.get("code") or ""
        language = body.get("language", "python")
        last_verdict, last_detail = None, None
        sid = body.get("last_submission_id")
        if sid:
            sub = db.get_submission(int(sid))
            if sub:
                last_verdict = sub["verdict"]
                cs = sub["detail"].get("cases", [])
                last_detail = "\n".join(
                    f"用例{c.get('idx')}: {c.get('verdict')}" for c in cs[:12])

        context = build_problem_context(problem, code, language, last_verdict, last_detail)
        history = [
            {"role": "system", "content": AI_JUDGE_SYSTEM + "\n\n" + context},
            {"role": "user", "content": "请判定我这份代码（先用工具验证，再出报告）。"},
        ]
        tools = SandboxTools(
            get_problem=lambda p: db.get_problem(p),
            load_cases=lambda p: _load_cases(db, p),
            cpp_compiler=cfg.cpp_compiler,
        )

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue()

            def on_event(kind: str, data: dict) -> None:
                q.put((kind, data))

            def run():
                try:
                    result = chat_step(llm, tools, history, on_event=on_event)
                    report = ai_judge_report(result.reply)
                    if report:
                        q.put(("report", {"report": report}))
                    else:
                        q.put(("report_raw", {"text": result.reply}))
                except Exception as e:
                    q.put(("error", {"message": str(e)}))
                finally:
                    q.put(("done", {}))

            task = loop.run_in_executor(None, run)
            while True:
                kind, data = await loop.run_in_executor(None, q.get)
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"
                if kind in ("done", "error"):
                    break
            await task

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------- AI 讲题教练（沙箱工具循环 + SSE 流式） ----------

    @app.post("/api/chat/problem/{pid}")
    async def chat_problem(pid: str, req: ChatReq):
        problem = db.get_problem(pid)
        if not problem:
            raise HTTPException(404, "题目不存在")
        llm = _llm(cfg)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import COACH_SYSTEM, SandboxTools, build_problem_context, chat_step

        last_verdict, last_detail = None, None
        if req.last_submission_id:
            sub = db.get_submission(req.last_submission_id)
            if sub:
                last_verdict = sub["verdict"]
                cs = sub["detail"].get("cases", [])
                last_detail = "\n".join(
                    f"用例{c.get('idx')}: {c.get('verdict')}" for c in cs[:12])

        context = build_problem_context(
            problem, req.code or "", req.language, last_verdict, last_detail)
        history = [{"role": "system", "content": COACH_SYSTEM + "\n\n" + context}]
        history += [m for m in req.messages if m.get("role") in ("user", "assistant")
                    and m.get("content")]

        tools = SandboxTools(
            get_problem=lambda pid_: db.get_problem(pid_),
            load_cases=lambda pid_: _load_cases(db, pid_),
            cpp_compiler=cfg.cpp_compiler,
        )

        import asyncio

        loop = asyncio.get_event_loop()

        async def gen():
            import queue

            q: queue.Queue = queue.Queue()

            def on_event(kind: str, data: dict) -> None:
                q.put((kind, data))

            def run():
                try:
                    chat_step(llm, tools, history, on_event=on_event)
                except Exception as e:
                    q.put(("error", {"message": str(e)}))
                finally:
                    q.put(("done", {}))

            task = loop.run_in_executor(None, run)
            while True:
                kind, data = await loop.run_in_executor(None, q.get)
                payload = json.dumps({"event": kind, **data}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                if kind in ("done", "error"):
                    break
            await task

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.exception_handler(503)
    async def _(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc.detail)})

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


_LLM_HINT = ("LLM 未配置：请在 data/config.yaml 填写 api_key（或设置 PREPDOJO_API_KEY），"
             "重启后可用 AI 点评与八股打分。判题功能不受影响。")


def _load_cases(db: DB, pid: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT input, expected_output FROM test_cases WHERE problem_id=? ORDER BY idx",
        (pid,),
    ).fetchall()
    return [{"input": r["input"], "output": r["expected_output"]} for r in rows]
