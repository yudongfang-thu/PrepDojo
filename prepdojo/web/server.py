"""PrepDojo Web UI：FastAPI + 无构建静态前端（localhost 单用户）。

LLM 未配置时：判题完全可用；AI 点评 / 八股打分返回明确提示。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

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


class FollowupReq(BaseModel):
    card_id: str
    question: str
    answer: str
    context_answer: Optional[str] = None


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
    def cards_next(tags: str = "", n: int = 5, difficulty: int = -1):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
        cards = db.pick_cards(tags=tag_list, n=n,
                              difficulty=difficulty if difficulty > 0 else None)
        # 打分后才下发 answer_points 之外的信息；这里先不带答案，防偷看
        return {"cards": [
            {k: c[k] for k in ("id", "question", "topic_tags", "difficulty", "source_ref")}
            for c in cards
        ]}

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
            result = grade_answer(llm, card, req.answer)
        except Exception as e:
            raise HTTPException(502, f"打分失败: {e}")
        db.record_attempt(card["id"], card["question"], req.answer,
                          result.get("score", 0), result, mode="web")
        result["reference"] = card["answer_points"]  # 打完分再给参考要点
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
            result = grade_followup(llm, card, req.question, req.answer, req.context_answer)
        except Exception as e:
            raise HTTPException(502, f"追问评分失败: {e}")
        db.record_attempt(card["id"], req.question, req.answer,
                          result.get("score", 0), result, mode="follow_up")
        return result

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
