"""PrepDojo Web UI：FastAPI + 无构建静态前端。

单机模式：localhost 单用户，无鉴权（所有请求视为 local 用户）。
多用户模式（server-beta，multiuser=True）：登录 + 会话 Cookie，
个人数据（提交/练习/学习进度）按用户隔离，知识库与题库共享，
危险端点（知识库管理/全局配置）仅管理员可用。

LLM 未配置时：判题完全可用；AI 点评 / 八股打分返回明确提示。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..auth import LOCAL_USER, SESSION_COOKIE, SESSION_DAYS
from ..config import Config, is_placeholder_key
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


def create_app(cfg: Config, db: DB, multiuser: bool = False) -> FastAPI:
    app = FastAPI(title="PrepDojo", docs_url=None, redoc_url=None)

    # ---------- 认证 ----------

    def require_user(request: Request) -> dict:
        """所有 /api 端点的用户解析：单机模式恒为 local；多用户模式查会话。"""
        if not multiuser:
            return LOCAL_USER
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            user = db.session_user(token)
            if user:
                return user
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    def require_admin(user: dict = Depends(require_user)) -> dict:
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    def _user_llm(user: dict) -> Optional[LLMClient]:
        """构造 LLM 客户端：个人 key 优先于服务器共享 key；含每日用量上限。"""
        uid = user["username"]
        if cfg.daily_limit_per_user > 0 and \
                db.llm_usage_today(uid) >= cfg.daily_limit_per_user:
            raise HTTPException(429, f"今日 AI 调用已达上限（{cfg.daily_limit_per_user} 次），明天再来")
        key = user.get("api_key") or cfg.api_key
        if is_placeholder_key(key):
            return None
        try:
            client = LLMClient(cfg.base_url, key, cfg.model, cfg.timeout, cfg.temperature)
            db.bump_llm_usage(uid)
            return client
        except LLMNotConfigured:
            return None

    @app.post("/api/auth/login")
    def login(body: dict):
        if not multiuser:
            raise HTTPException(400, "单机模式无需登录")
        user = db.verify_login(body.get("username") or "", body.get("password") or "")
        if not user:
            raise HTTPException(401, "用户名或密码错误")
        token = db.create_session(user["username"], SESSION_DAYS)
        resp = JSONResponse({"ok": True, "username": user["username"],
                             "is_admin": user["is_admin"]})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_DAYS * 86400, path="/")
        return resp

    @app.get("/api/auth/registration_mode")
    def registration_mode():
        """前端据此渲染注册表单（无需登录）。"""
        if not multiuser:
            return {"mode": "off", "multiuser": False}
        return {"mode": cfg.registration if cfg.registration in ("off", "code", "open") else "off",
                "multiuser": True}

    @app.post("/api/auth/register")
    def register(body: dict):
        """自助注册（按 registration 模式校验），成功即自动登录。"""
        if not multiuser:
            raise HTTPException(400, "单机模式无需注册")
        mode = cfg.registration if cfg.registration in ("off", "code", "open") else "off"
        if mode == "off":
            raise HTTPException(403, "当前未开放自助注册，请联系管理员创建账号")
        if mode == "code":
            code = (body.get("code") or "").strip()
            if not cfg.registration_code or code != cfg.registration_code:
                raise HTTPException(403, "邀请码错误，请向管理员索取")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if len(username) < 2 or len(username) > 20:
            raise HTTPException(400, "用户名长度需在 2-20 字符之间")
        if len(password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        if not db.create_user(username, password, is_admin=False):
            raise HTTPException(409, "用户名已存在或非法（不能包含空格、引号或斜杠）")
        token = db.create_session(username, SESSION_DAYS)
        resp = JSONResponse({"ok": True, "username": username, "is_admin": False})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_DAYS * 86400, path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            db.delete_session(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me(user: dict = Depends(require_user)):
        return {"username": user["username"], "is_admin": bool(user.get("is_admin")),
                "multiuser": multiuser,
                "llm": {"server_ready": cfg.llm_ready,
                        "using_own_key": bool(user.get("api_key"))}}

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        """HTML/JS/CSS 每次必须 revalidate，杜绝浏览器拿旧版前端。"""
        resp = await call_next(request)
        ct = resp.headers.get("content-type", "")
        if any(t in ct for t in ("text/html", "javascript", "text/css")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.get("/api/health")
    def health():
        return {"ok": True, "llm_ready": cfg.llm_ready,
                "model": cfg.model if cfg.llm_ready else None,
                "multiuser": multiuser}

    # ---------- 设置：个人 API key（多用户模式） ----------

    @app.get("/api/me/llm")
    def me_llm(user: dict = Depends(require_user)):
        return {"using_own_key": bool(user.get("api_key")),
                "server_configured": cfg.llm_ready,
                "daily_limit": cfg.daily_limit_per_user}

    @app.post("/api/me/llm")
    def me_llm_save(body: dict, user: dict = Depends(require_user)):
        key = (body.get("api_key") or "").strip()
        clean = None if is_placeholder_key(key) else key
        db.set_user_api_key(user["username"], clean)
        return {"ok": True, "using_own_key": clean is not None}

    # ---------- 设置：LLM 全局配置（仅管理员） ----------

    @app.get("/api/llm/config")
    def llm_config(admin: dict = Depends(require_admin)):
        masked = ""
        if cfg.api_key:
            k = cfg.api_key
            masked = k[:5] + "***" + k[-4:] if len(k) > 12 else "***"
        return {"base_url": cfg.base_url, "model": cfg.model,
                "api_key_masked": masked, "configured": cfg.llm_ready}

    @app.post("/api/llm/config")
    def llm_config_save(body: dict, admin: dict = Depends(require_admin)):
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

    @app.post("/api/llm/test")
    def llm_test(user: dict = Depends(require_user)):
        """发一条最简消息验证 API 连通性，返回成功或明确错误。"""
        llm = _user_llm(user)
        if llm is None:
            return {"ok": False, "detail": "未配置 API key，请在下方填写 key 并保存"}
        try:
            answer = llm.chat("", "回复一个字：通", max_tokens=500)
            return {"ok": True, "detail": "连接成功，模型可正常响应", "model": cfg.model}
        except Exception as e:
            msg = str(e)
            if "401" in msg or "invalid" in msg.lower() or "Authentication" in msg:
                return {"ok": False, "detail": "API key 无效（401 认证失败），请检查 key"}
            if "timeout" in msg.lower():
                return {"ok": False, "detail": "连接超时，请检查 base_url 是否可达"}
            return {"ok": False, "detail": msg[:200]}

    @app.get("/api/llm/models")
    def llm_models(admin: dict = Depends(require_admin)):
        """扫描 base_url 下可用模型列表（管理员：请求携带服务器共享 key）。"""
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

    # ---------- 用户管理（仅管理员） ----------

    @app.get("/api/admin/users")
    def admin_users(admin: dict = Depends(require_admin)):
        users = db.list_users()
        for u in users:
            u["llm_today"] = db.llm_usage_today(u["username"])
        return {"users": users}

    @app.post("/api/admin/users")
    def admin_add_user(body: dict, admin: dict = Depends(require_admin)):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if len(password) < 4:
            raise HTTPException(400, "密码至少 4 位")
        if not db.create_user(username, password, bool(body.get("is_admin"))):
            raise HTTPException(400, "用户名已存在或非法（勿含空格/引号/斜杠）")
        return {"ok": True}

    @app.post("/api/admin/users/{username}/passwd")
    def admin_passwd(username: str, body: dict, admin: dict = Depends(require_admin)):
        password = body.get("password") or ""
        if len(password) < 4:
            raise HTTPException(400, "密码至少 4 位")
        if not db.set_user_password(username, password):
            raise HTTPException(404, "用户不存在")
        return {"ok": True}

    @app.delete("/api/admin/users/{username}")
    def admin_del_user(username: str, admin: dict = Depends(require_admin)):
        if username == admin["username"]:
            raise HTTPException(400, "不能删除当前登录的管理员")
        if not db.delete_user(username):
            raise HTTPException(404, "用户不存在")
        return {"ok": True}

    # ---------- 设置：知识库管理（仅管理员：涉及服务器路径与 LLM 成本） ----------

    @app.get("/api/fs/browse")
    def fs_browse(path: str = "~", admin: dict = Depends(require_admin)):
        """服务器目录浏览（管理员工具）。返回子目录与可导入文件统计。"""
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
    def list_sources(user: dict = Depends(require_user)):
        rows = db.execute(
            "SELECT id, path, title, n_cards, ingested_at FROM sources ORDER BY id DESC"
        ).fetchall()
        return {"sources": [
            {"id": r["id"], "title": r["title"] or Path(r["path"]).name,
             "path": r["path"], "n_cards": r["n_cards"],
             "ingested_at": r["ingested_at"]} for r in rows]}

    @app.delete("/api/sources/{sid}")
    def delete_source(sid: int, admin: dict = Depends(require_admin)):
        db.execute("DELETE FROM cards WHERE source_id=?", (sid,))
        db.execute("DELETE FROM sources WHERE id=?", (sid,))
        return {"ok": True}

    @app.post("/api/ingest/start")
    async def ingest_start(body: dict, admin: dict = Depends(require_admin)):
        """SSE：流式导入知识目录（AI thinking / 输出 / 进度全事件）。"""
        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "缺少 path")
        llm = _user_llm(admin)
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

    @app.post("/api/problems/generate")
    async def problems_generate(body: dict, admin: dict = Depends(require_admin)):
        """AI 出题：生成 → 沙箱实跑参考解生成期望 → 自洽才入库（SSE）。"""
        brief = (body.get("brief") or "").strip()
        if not brief:
            raise HTTPException(400, "请填写出题需求或题目描述")
        llm = _user_llm(admin)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..problem_gen import generate_problem

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue()

            def on_event(kind: str, data: dict) -> None:
                q.put((kind, data))

            def run():
                try:
                    out = generate_problem(llm, brief,
                                           cpp_compiler=cfg.cpp_compiler,
                                           on_event=on_event,
                                           docker_image=cfg.judge_docker_image)
                    db.upsert_problem(out["problem"], out["cases"])
                    q.put(("saved", {"problem_id": out["problem"]["id"],
                                     "title": out["problem"]["title"],
                                     "difficulty": out["problem"]["difficulty"],
                                     "tags": out["problem"]["tags"],
                                     "n_cases": len(out["cases"])}))
                except Exception as e:
                    q.put(("error", {"message": str(e)[:400]}))
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

    # ---------- Coding 题导入：JSON 直接导入 / AI 适配目录（仅管理员） ----------

    @app.post("/api/problems/import_json")
    async def problems_import_json(body: dict, admin: dict = Depends(require_admin)):
        """从目录导入符合 schema 的 JSON 题目文件（无需 AI，含用例直接入库）。"""
        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "缺少 path")
        root = Path(path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"目录不存在: {root}")

        import asyncio
        import json as _json
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue()

            def run():
                try:
                    files = sorted(root.glob("*.json"))
                    q.put(("total", {"n": len(files)}))
                    ok = fail = 0
                    for fp in files:
                        try:
                            obj = _json.loads(fp.read_text(encoding="utf-8"))
                            cases = [{"input": c["input"], "output": c["output"],
                                      "sample": c.get("sample", False)}
                                     for c in obj["test_cases"]]
                            db.upsert_problem(obj, cases)
                            ok += 1
                            q.put(("imported", {"file": fp.name,
                                                "id": obj.get("id", "?"),
                                                "title": obj.get("title", "")}))
                        except Exception as e:
                            fail += 1
                            q.put(("failed", {"file": fp.name, "error": str(e)[:200]}))
                    q.put(("all_done", {"ok": ok, "fail": fail}))
                except Exception as e:
                    q.put(("error", {"message": str(e)[:300]}))
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

    @app.post("/api/problems/adapt")
    async def problems_adapt(body: dict, admin: dict = Depends(require_admin)):
        """AI 适配导入：目录中每个 .md/.txt 视为一道题的描述，
        AI 生成参考解+用例并沙箱自洽验证后入库。"""
        path = (body.get("path") or "").strip()
        limit = int(body.get("limit") or 0)
        if not path:
            raise HTTPException(400, "缺少 path")
        root = Path(path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"目录不存在: {root}")
        llm = _user_llm(admin)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..problem_gen import generate_problem

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue()

            def on_event(kind: str, data: dict) -> None:
                q.put((kind, data))

            def run():
                try:
                    files = sorted(list(root.glob("*.md")) + list(root.glob("*.txt")))
                    if limit > 0:
                        files = files[:limit]
                    q.put(("total", {"n": len(files)}))
                    ok = fail = 0
                    for fp in files:
                        q.put(("file_start", {"file": fp.name}))
                        brief = fp.read_text(encoding="utf-8", errors="ignore")[:4000]
                        brief = ("根据以下题目描述生成判题题（保留题意，规范输入输出格式，"
                                 f"设计参考解与用例）：\n\n{brief}")
                        try:
                            out = generate_problem(
                                llm, brief, cpp_compiler=cfg.cpp_compiler,
                                on_event=on_event,
                                docker_image=cfg.judge_docker_image)
                            db.upsert_problem(out["problem"], out["cases"])
                            ok += 1
                            q.put(("saved", {"file": fp.name,
                                             "problem_id": out["problem"]["id"],
                                             "title": out["problem"]["title"],
                                             "n_cases": len(out["cases"])}))
                        except Exception as e:
                            fail += 1
                            q.put(("failed", {"file": fp.name,
                                              "error": str(e)[:250]}))
                    q.put(("all_done", {"ok": ok, "fail": fail}))
                except Exception as e:
                    q.put(("error", {"message": str(e)[:300]}))
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
    def stats(user: dict = Depends(require_user)):
        return db.stats(user["username"])

    @app.get("/api/tags")
    def tags(user: dict = Depends(require_user)):
        return {"tags": db.all_tags()}

    # ---------- 代码题 ----------

    @app.get("/api/problems")
    def problems(user: dict = Depends(require_user)):
        return {"problems": db.list_problems(user["username"])}

    @app.delete("/api/problems/{pid}")
    def delete_problem(pid: str, admin: dict = Depends(require_admin)):
        db.execute("DELETE FROM test_cases WHERE problem_id=?", (pid,))
        db.execute("DELETE FROM coding_problems WHERE id=?", (pid,))
        return {"ok": True}

    @app.get("/api/problems/wrong")
    def wrong_problems(user: dict = Depends(require_user)):
        """错题本：提交过但从未 AC 的题（AC 即自动移出）。"""
        ids = db.wrong_problem_ids(user["username"])
        allp = {p["id"]: p for p in db.list_problems(user["username"])}
        return {"wrong": [allp[i] for i in ids if i in allp]}

    @app.get("/api/submissions/last/{pid}")
    def last_submission_code(pid: str, language: str = "",
                             user: dict = Depends(require_user)):
        """打开题目时恢复上次提交的代码（草稿丢失后回退用）。
        language 为空时取最近一次提交（不限语言），同时返回语言字段。"""
        r = db.last_submission_code(pid, language, user_id=user["username"])
        return {"code": r["code"] if r else None, "language": r["language"] if r else None}

    @app.get("/api/problems/{pid}")
    def problem_detail(pid: str, user: dict = Depends(require_user)):
        p = db.get_problem(pid)
        if not p:
            raise HTTPException(404, "题目不存在")
        return p

    @app.post("/api/submit")
    def submit(req: SubmitReq, user: dict = Depends(require_user)):
        p = db.get_problem(req.problem_id)
        if not p:
            raise HTTPException(404, "题目不存在")
        if req.language not in p["languages"]:
            raise HTTPException(400, f"该题不支持 {req.language}")

        cases = _load_cases(db, req.problem_id)
        res = judge_submission(
            req.code, req.language, cases,
            time_limit_ms=p["time_limit_ms"], mem_limit_mb=p["mem_limit_mb"],
            cpp_compiler=cfg.cpp_compiler, docker_image=cfg.judge_docker_image,
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
            res.max_time_ms, user_id=user["username"],
        )
        return {
            "submission_id": sid, "verdict": res.verdict,
            "max_time_ms": res.max_time_ms,
            "compile_error": res.compile_error,
            "cases": [c.__dict__ for c in res.cases],
            "case_summary": case_summary,
        }

    @app.post("/api/review/{sid}")
    def review(sid: int, user: dict = Depends(require_user)):
        sub = db.get_submission(sid, user_id=user["username"])
        if not sub:
            raise HTTPException(404, "提交不存在")
        if sub.get("review"):
            return {"review": sub["review"]}
        llm = _user_llm(user)
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
        db.set_review(sid, r, user_id=user["username"])
        return {"review": r}

    # ---------- 八股 ----------

    @app.get("/api/cards/next")
    def cards_next(tags: str = "", n: int = 5, difficulty: int = -1,
                   only_learned: bool = True, user: dict = Depends(require_user)):
        uid = user["username"]
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
        cards = db.pick_cards(user_id=uid, tags=tag_list, n=n, only_learned=only_learned,
                              difficulty=difficulty if difficulty > 0 else None)
        if not cards and only_learned:
            # 已学池为空：回退到全部卡（提示前端），避免无题可抽
            cards = db.pick_cards(user_id=uid, tags=tag_list, n=n,
                                  difficulty=difficulty if difficulty > 0 else None)
            return {"cards": [c0(c) for c in cards], "fallback": True}
        return {"cards": [c0(c) for c in cards], "fallback": False}

    def c0(c):  # 抽题不下发答案，防偷看
        return {k: c[k] for k in ("id", "question", "topic_tags", "difficulty",
                                  "source_ref", "learned")}

    @app.get("/api/cards/learn")
    def cards_learn(tags: str = "", n: int = 10, include_learned: bool = False,
                    user: dict = Depends(require_user)):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
        cards = db.pick_learn_cards(user_id=user["username"], tags=tag_list, n=n,
                                    only_unlearned=not include_learned)
        return {"cards": cards}  # 学习模式直接给全部字段（含要点与讲解缓存）

    @app.post("/api/cards/{cid}/learn")
    def card_mark_learned(cid: str, body: dict, user: dict = Depends(require_user)):
        learned = bool(body.get("learned", True))
        if not db.mark_learned(cid, learned, user_id=user["username"]):
            raise HTTPException(404, "题卡不存在")
        return {"ok": True, "learned": learned}

    @app.get("/api/cards/progress")
    def cards_progress(user: dict = Depends(require_user)):
        return db.learn_progress(user["username"])

    @app.get("/api/cards/{cid}/explain")
    def card_explain(cid: str, user: dict = Depends(require_user)):
        card = db.get_card(cid)
        if not card:
            raise HTTPException(404, "题卡不存在")
        if card.get("explanation"):  # 缓存直接返回
            return {"explanation": json.loads(card["explanation"]), "cached": True}
        llm = _user_llm(user)
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
    def card_detail(cid: str, user: dict = Depends(require_user)):
        c = db.get_card(cid, user_id=user["username"])
        if not c:
            raise HTTPException(404, "题卡不存在")
        return c

    @app.post("/api/quiz/grade")
    def quiz_grade(req: GradeReq, user: dict = Depends(require_user)):
        card = db.get_card(req.card_id)
        if not card:
            raise HTTPException(404, "题卡不存在")
        llm = _user_llm(user)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..quiz import grade_answer

        try:
            out = grade_answer(llm, card, req.answer, style=req.style, with_reasoning=True)
            result = out["json"]
        except Exception as e:
            raise HTTPException(502, f"打分失败: {e}")
        db.record_attempt(card["id"], card["question"], req.answer,
                          result.get("score", 0), result, mode="web",
                          user_id=user["username"])
        result["reference"] = card["answer_points"]  # 打完分再给参考要点
        result["reasoning"] = out["reasoning"]
        return result

    @app.post("/api/quiz/followup")
    def quiz_followup(req: FollowupReq, user: dict = Depends(require_user)):
        card = db.get_card(req.card_id)
        if not card:
            raise HTTPException(404, "题卡不存在")
        llm = _user_llm(user)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..quiz import grade_followup

        try:
            out = grade_followup(llm, card, req.question, req.answer,
                                 req.context_answer, style=req.style, with_reasoning=True)
            result = out["json"]
        except Exception as e:
            raise HTTPException(502, f"追问评分失败: {e}")
        db.record_attempt(req.card_id, req.question, req.answer,
                          result.get("score", 0), result, mode="follow_up",
                          user_id=user["username"])
        result["reasoning"] = out["reasoning"]
        return result

    # ---------- AI 修复代码（直接给修复后代码，不引导） ----------

    @app.post("/api/fix/{pid}")
    async def fix_code_endpoint(pid: str, body: dict, user: dict = Depends(require_user)):
        problem = db.get_problem(pid)
        if not problem:
            raise HTTPException(404, "题目不存在")
        llm = _user_llm(user)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import fix_code

        code = body.get("code") or ""
        language = body.get("language", "python")
        verdict = body.get("verdict", "")
        detail = body.get("detail", "")

        import asyncio, queue as _q
        loop = asyncio.get_event_loop()

        async def gen():
            q = _q.Queue()

            def on_event(kind, text):
                q.put((kind, text))

            def run():
                try:
                    reply = fix_code(llm, problem, code, language, verdict, detail, on_event=on_event)
                    q.put(("done", reply))
                except Exception as e:
                    q.put(("error", str(e)[:300]))

            task = loop.run_in_executor(None, run)
            while True:
                item = await loop.run_in_executor(None, q.get)
                if item[0] == "done":
                    yield "data: " + json.dumps({"event": "reply", "code": item[1]}, ensure_ascii=False) + "\n\n"
                    break
                elif item[0] == "error":
                    yield "data: " + json.dumps({"event": "error", "message": item[1]}, ensure_ascii=False) + "\n\n"
                    break
                else:
                    yield "data: " + json.dumps({"event": item[0], "text": item[1]}, ensure_ascii=False) + "\n\n"
            await task

        from fastapi.responses import StreamingResponse
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------- AI 判题（工具增强的结构化判定报告，SSE） ----------

    @app.post("/api/ai_judge/{pid}")
    async def ai_judge(pid: str, body: dict, user: dict = Depends(require_user)):
        problem = db.get_problem(pid)
        if not problem:
            raise HTTPException(404, "题目不存在")
        llm = _user_llm(user)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import (AI_JUDGE_SYSTEM, SandboxTools, ai_judge_report,
                            build_problem_context, chat_step)

        code = body.get("code") or ""
        language = body.get("language", "python")
        last_verdict, last_detail = None, None
        sid = body.get("last_submission_id")
        if sid:
            sub = db.get_submission(int(sid), user_id=user["username"])
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
            docker_image=cfg.judge_docker_image,
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
                    # AI 判题留档到独立表（不进 submissions：避免污染错题本与统计）
                    verdict = (report or {}).get("sandbox_verdict", "NA")
                    db.record_ai_judgement(
                        pid, language, code, verdict,
                        {"tool_trace": [t.summary for t in result.tool_trace],
                         "report": report}, user_id=user["username"])
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
    async def chat_problem(pid: str, req: ChatReq, user: dict = Depends(require_user)):
        problem = db.get_problem(pid)
        if not problem:
            raise HTTPException(404, "题目不存在")
        llm = _user_llm(user)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import COACH_SYSTEM, SandboxTools, build_problem_context, chat_step

        last_verdict, last_detail = None, None
        if req.last_submission_id:
            sub = db.get_submission(req.last_submission_id, user_id=user["username"])
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
            docker_image=cfg.judge_docker_image,
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
    rows = db.execute(
        "SELECT input, expected_output FROM test_cases WHERE problem_id=? ORDER BY idx",
        (pid,),
    ).fetchall()
    return [{"input": r["input"], "output": r["expected_output"]} for r in rows]
