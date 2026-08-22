"""PrepDojo Web UI：FastAPI + 无构建静态前端。

单机模式：localhost 单用户，无鉴权（所有请求视为 local 用户）。
多用户模式（server-beta，multiuser=True）：登录 + 会话 Cookie，
个人数据（提交/练习/学习进度）按用户隔离，知识库与题库共享，
危险端点（知识库管理/全局配置）仅管理员可用。

LLM 未配置时：判题完全可用；AI 点评 / 八股打分返回明确提示。
"""

from __future__ import annotations

import asyncio
import heapq
import ipaddress
import itertools
import json
import logging
import queue
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..auth import LOCAL_USER, SESSION_COOKIE, SESSION_DAYS
from ..config import (Config, ConfigError, is_placeholder_key, load_config,
                      update_llm_config)
from ..db import DB
from ..judge import (JudgeInfrastructureError, configure_docker_concurrency,
                     judge_backend_status, judge_submission)
from ..llm import (LLMBusy, LLMCancelled, LLMClient, LLMNotConfigured,
                   LLMQuotaExceeded, _strict_json_loads)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 512 * 1024
MAX_CODE_CHARS = 100_000
MAX_AI_CODE_CHARS = 12_000
MAX_CHAT_MESSAGES = 30
MAX_CHAT_TOTAL_CHARS = 40_000
MAX_RATE_LIMIT_IDENTITIES = 10_000
_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LOG = logging.getLogger(__name__)


class _BodyLimitMiddleware:
    """预读并限制 API 请求体，覆盖 chunked/无 Content-Length 的请求。"""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                declared = int(raw_length)
                if declared < 0:
                    raise ValueError
                if declared > self.max_bytes:
                    response = JSONResponse({"detail": "请求体过大"}, status_code=413)
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse({"detail": "Content-Length 非法"}, status_code=400)
                await response(scope, receive, send)
                return

        body = bytearray()
        received = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_bytes:
                response = JSONResponse(
                    {"detail": "请求体过大"}, status_code=413,
                    headers={"Connection": "close"})
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body),
                        "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


class _JudgeCapacityExceeded(RuntimeError):
    def __init__(self, scope: str):
        self.scope = scope
        super().__init__("个人判题并发已满" if scope == "user" else "全站判题并发已满")


class _JudgeLimiter:
    """共享判题并发闸门；速率限制之外阻止瞬时 Docker 资源风暴。"""

    def __init__(self, global_limit: int, per_user_limit: int):
        self._slots = threading.BoundedSemaphore(global_limit)
        self._per_user_limit = per_user_limit
        self._active: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def run(self, user_id: str, func, *args, **kwargs):
        with self._lock:
            if self._active[user_id] >= self._per_user_limit:
                raise _JudgeCapacityExceeded("user")
            if not self._slots.acquire(blocking=False):
                raise _JudgeCapacityExceeded("global")
            self._active[user_id] += 1
        try:
            return func(*args, **kwargs)
        finally:
            with self._lock:
                self._active[user_id] -= 1
                if self._active[user_id] == 0:
                    del self._active[user_id]
            self._slots.release()


class _ReqModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginReq(_ReqModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterReq(LoginReq):
    code: str = Field(default="", max_length=256)


class ApiKeyReq(_ReqModel):
    api_key: str = Field(default="", max_length=4096)


class LLMConfigReq(ApiKeyReq):
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)


class AdminUserReq(LoginReq):
    is_admin: bool = False


class PasswordReq(_ReqModel):
    password: str = Field(min_length=1, max_length=256)


class PathReq(_ReqModel):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def path_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("path 不能为空")
        return value


class GenerateReq(_ReqModel):
    brief: str = Field(min_length=1, max_length=20_000)

    @field_validator("brief")
    @classmethod
    def brief_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("brief 不能为空")
        return value


class AdaptReq(PathReq):
    limit: int = Field(default=0, ge=0, le=1000)


class LearnReq(_ReqModel):
    learned: bool = True


class FixReq(_ReqModel):
    code: str = Field(min_length=1, max_length=MAX_AI_CODE_CHARS)
    language: Literal["python", "cpp"] = "python"
    verdict: str = Field(default="", max_length=16)
    detail: str = Field(default="", max_length=4000)


class AIJudgeReq(_ReqModel):
    code: str = Field(min_length=1, max_length=MAX_AI_CODE_CHARS)
    language: Literal["python", "cpp"] = "python"
    last_submission_id: Optional[int] = None


class SubmitReq(_ReqModel):
    problem_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    language: Literal["python", "cpp"]
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)


class GradeReq(_ReqModel):
    card_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=20_000)
    style: Literal["standard", "strict", "pressure"] = "standard"

    @field_validator("answer")
    @classmethod
    def answer_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer 不能为空")
        return value


class FollowupReq(_ReqModel):
    card_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=20_000)
    context_answer: Optional[str] = Field(default=None, max_length=20_000)
    style: Literal["standard", "strict", "pressure"] = "standard"

    @field_validator("question", "answer")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("文本不能为空")
        return value


class ChatMessage(_ReqModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)

    @field_validator("content")
    @classmethod
    def content_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content 不能为空")
        return value


class ChatReq(_ReqModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_CHAT_MESSAGES)
    code: Optional[str] = Field(default=None, max_length=MAX_AI_CODE_CHARS)
    language: Literal["python", "cpp"] = "python"
    last_submission_id: Optional[int] = None

    @model_validator(mode="after")
    def total_context_is_bounded(self):
        total = sum(len(message.content) for message in self.messages)
        total += len(self.code or "")
        if total > MAX_CHAT_TOTAL_CHARS:
            raise ValueError(f"对话与代码总长度不能超过 {MAX_CHAT_TOTAL_CHARS} 字符")
        return self


class _RateLimiter:
    """进程内滑动窗口限流；公网前置代理仍应配置连接/带宽限制。"""

    def __init__(self):
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._windows: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def _sweep(self, now: float) -> None:
        for key in list(self._hits):
            hits = self._hits[key]
            window = self._windows.get(key, 60)
            while hits and hits[0] <= now - window:
                hits.popleft()
            if not hits:
                del self._hits[key]
                self._windows.pop(key, None)
        self._last_sweep = now

    def check(self, bucket: str, identity: str, limit: int, window_s: int) -> None:
        now = time.monotonic()
        key = (bucket, identity)
        with self._lock:
            # 清理最多每分钟一次；达到容量后每个新来源都全表扫描会反过来
            # 成为可利用的 CPU 放大器。
            if now - self._last_sweep >= 60:
                self._sweep(now)
            if key not in self._hits and len(self._hits) >= MAX_RATE_LIMIT_IDENTITIES:
                raise HTTPException(
                    429, "请求来源过多，请稍后重试", headers={"Retry-After": "60"})
            hits = self._hits[key]
            self._windows[key] = window_s
            while hits and hits[0] <= now - window_s:
                hits.popleft()
            if len(hits) >= limit:
                retry = max(1, int(window_s - (now - hits[0])))
                raise HTTPException(
                    429, "请求过于频繁，请稍后重试", headers={"Retry-After": str(retry)})
            hits.append(now)


def _queue_put(q, item: tuple, *, critical: bool = False) -> None:
    """SSE 有界队列写入：增量可丢，终态必要时淘汰一个旧增量。"""
    import queue

    try:
        q.put_nowait(item)
        return
    except queue.Full:
        if not critical:
            return
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def create_app(cfg: Config, db: DB, multiuser: bool = False) -> FastAPI:
    app = FastAPI(
        title="PrepDojo", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_BodyLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)
    configure_docker_concurrency(cfg.judge_concurrency_global)
    rate_limiter = _RateLimiter()
    auth_slots = threading.BoundedSemaphore(4)
    llm_slots = threading.BoundedSemaphore(cfg.llm_concurrency_global)
    llm_active: dict[str, int] = defaultdict(int)
    llm_slot_lock = threading.Lock()
    judge_limiter = _JudgeLimiter(
        cfg.judge_concurrency_global, cfg.judge_concurrency_per_user)
    backend_cache: dict[str, Any] = {"at": 0.0, "value": None}
    backend_lock = threading.Lock()
    llm_config_lock = threading.Lock()

    def _client_ip(request: Request) -> str:
        raw = request.client.host if request.client else "unknown"
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return raw[:128]
        if ip.version == 6:
            network = ipaddress.ip_network(f"{ip}/64", strict=False)
            return f"{network.network_address}/64"
        return str(ip)

    def _host_allowed(raw_host: str) -> bool:
        """校验 Host，正确处理 RFC 规定的 ``[IPv6]:port`` 形式。"""
        raw = raw_host.strip().lower()
        if raw.startswith("["):
            end = raw.find("]")
            suffix = raw[end + 1:] if end >= 0 else ""
            if (end < 0 or (suffix and not re.fullmatch(r":\d{1,5}", suffix))
                    or (suffix and not 1 <= int(suffix[1:]) <= 65535)):
                return False
            host = raw[1:end]
        elif raw.count(":") == 1:
            name, maybe_port = raw.rsplit(":", 1)
            if not maybe_port.isdigit() or not 1 <= int(maybe_port) <= 65535:
                return False
            host = name
        else:
            host = raw
        host = host.rstrip(".")
        for pattern in cfg.allowed_hosts:
            allowed = pattern.lower().rstrip(".")
            if allowed == "*" or host == allowed:
                return True
            if allowed.startswith("*.") and host.endswith(allowed[1:]) \
                    and host != allowed[2:]:
                return True
        return False

    def _backend_status() -> dict[str, Any]:
        if not multiuser:
            return {"configured": True, "ready": True, "mode": "local_rlimit"}
        now = time.monotonic()
        with backend_lock:
            if backend_cache["value"] is None or now - backend_cache["at"] > 30:
                backend_cache["value"] = judge_backend_status(cfg.judge_docker_image)
                backend_cache["at"] = now
            return dict(backend_cache["value"])

    def _require_judge_backend() -> None:
        status = _backend_status()
        if multiuser and not status.get("ready"):
            detail = status.get("error") or "多用户模式必须配置可用的 Docker 判题镜像"
            _LOG.error("判题服务未就绪: %s", detail)
            raise HTTPException(503, "判题服务暂不可用，请联系管理员")

    def _limited_judge(user_id: str, *args, **kwargs):
        kwargs["time_limit_ms"] = min(
            int(kwargs.get("time_limit_ms", 5000)), cfg.judge_time_limit_ms)
        kwargs["mem_limit_mb"] = min(
            int(kwargs.get("mem_limit_mb", 512)), cfg.judge_mem_limit_mb)
        return judge_limiter.run(user_id, judge_submission, *args, **kwargs)

    def _http_judge(user_id: str, *args, **kwargs):
        try:
            return _limited_judge(user_id, *args, **kwargs)
        except _JudgeCapacityExceeded as exc:
            status = 429 if exc.scope == "user" else 503
            raise HTTPException(
                status, str(exc) + "，请稍后重试",
                headers={"Retry-After": "2"}) from exc

    def _limited_reference(user_id: str, *args, **kwargs):
        from ..problem_gen import _run_reference

        positional = list(args)
        if len(positional) >= 3:
            positional[2] = min(int(positional[2]), cfg.judge_time_limit_ms)
        else:
            kwargs["time_limit_ms"] = min(
                int(kwargs.get("time_limit_ms", 5000)), cfg.judge_time_limit_ms)
        return judge_limiter.run(
            user_id, _run_reference, *positional, **kwargs)

    async def _drain_queue(
        request: Request, q, task, cancel_event: threading.Event,
        terminal: set[str],
    ):
        """可感知断连的 SSE 队列消费器；断连后通知后台 LLM 尽快停止。"""
        loop = asyncio.get_running_loop()
        try:
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    break
                try:
                    item = await loop.run_in_executor(None, q.get, True, 1.0)
                except queue.Empty:
                    continue
                yield item
                if item[0] in terminal:
                    break
        finally:
            cancel_event.set()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except asyncio.TimeoutError:
                pass

    @app.middleware("http")
    async def security_headers_and_limits(request: Request, call_next):
        """限制 API 请求体、校验浏览器来源，并统一加固响应头。"""
        if not _host_allowed(request.headers.get("host", "")):
            return JSONResponse({"detail": "Host 不在允许列表"}, status_code=400)
        if request.url.path.startswith("/api/"):
            if multiuser and request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if origin:
                    origin_host = urlsplit(origin).netloc.lower()
                    request_host = request.headers.get("host", "").lower()
                    if origin_host != request_host:
                        return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif any(t in ct for t in ("text/html", "javascript", "text/css")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'")
        if cfg.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # ---------- 认证 ----------

    def require_user(request: Request) -> dict:
        """所有 /api 端点的用户解析：单机模式恒为 local；多用户模式查会话。"""
        if not multiuser:
            return LOCAL_USER
        token = request.cookies.get(SESSION_COOKIE)
        if token and _SESSION_TOKEN_RE.fullmatch(token):
            user = db.session_user(token)
            if user:
                return user
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    def require_admin(user: dict = Depends(require_user)) -> dict:
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    def _user_llm(
        user: dict, cancel_check: Optional[Any] = None,
    ) -> Optional[LLMClient]:
        """个人 key 优先；每次真实外发前原子扣额并占用全局并发槽。"""
        uid = user["username"]
        with llm_config_lock:
            key = user.get("api_key") or cfg.api_key
            base_url, model = cfg.base_url, cfg.model
            timeout, temperature = cfg.timeout, cfg.temperature
        if is_placeholder_key(key):
            return None

        def release_slot() -> None:
            with llm_slot_lock:
                llm_active[uid] -= 1
                if llm_active[uid] == 0:
                    del llm_active[uid]
            llm_slots.release()

        def before_request() -> None:
            with llm_slot_lock:
                if llm_active[uid] >= cfg.llm_concurrency_per_user:
                    raise LLMBusy("个人 AI 请求并发已满，请稍后重试")
                if not llm_slots.acquire(blocking=False):
                    raise LLMBusy("全站 AI 服务并发已满，请稍后重试")
                llm_active[uid] += 1
            try:
                quota = db.consume_llm_quota(
                    uid, cfg.daily_limit_per_user, cfg.daily_limit_global)
            except BaseException:
                release_slot()
                raise
            if not quota["ok"]:
                release_slot()
                scope = "全站" if quota["scope"] == "global" else "个人"
                raise LLMQuotaExceeded(
                    f"今日{scope} AI 调用已达上限（{quota['limit']} 次）",
                    scope=quota["scope"], limit=quota["limit"])

        def after_request() -> None:
            release_slot()

        try:
            return LLMClient(
                base_url, key, model, timeout, temperature,
                before_request=before_request, after_request=after_request,
                cancel_check=cancel_check)
        except LLMNotConfigured:
            return None

    @app.post("/api/auth/login")
    def login(body: LoginReq, request: Request):
        if not multiuser:
            raise HTTPException(400, "单机模式无需登录")
        rate_limiter.check("login", _client_ip(request), 10, 300)
        rate_limiter.check("login_global", "*", 120, 60)
        if not auth_slots.acquire(blocking=False):
            raise HTTPException(
                503, "登录服务繁忙，请稍后重试", headers={"Retry-After": "2"})
        try:
            authenticated = db.authenticate_and_create_session(
                body.username, body.password, SESSION_DAYS)
        finally:
            auth_slots.release()
        if not authenticated:
            raise HTTPException(401, "用户名或密码错误")
        user, token = authenticated
        resp = JSONResponse({"ok": True, "username": user["username"],
                             "is_admin": user["is_admin"]})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=cfg.secure_cookie,
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
    def register(body: RegisterReq, request: Request):
        """自助注册（按 registration 模式校验），成功即自动登录。"""
        if not multiuser:
            raise HTTPException(400, "单机模式无需注册")
        rate_limiter.check("register", _client_ip(request), 5, 3600)
        rate_limiter.check("register_global", "*", 100, 3600)
        mode = cfg.registration if cfg.registration in ("off", "code", "open") else "off"
        if mode == "off":
            raise HTTPException(403, "当前未开放自助注册，请联系管理员创建账号")
        if mode == "code":
            code = body.code.strip()
            if not cfg.registration_code or code != cfg.registration_code:
                raise HTTPException(403, "邀请码错误，请向管理员索取")
        username = body.username.strip()
        password = body.password
        if len(username) < 2 or len(username) > 20:
            raise HTTPException(400, "用户名长度需在 2-20 字符之间")
        if len(password) < 8:
            raise HTTPException(400, "密码至少 8 位")
        if not auth_slots.acquire(blocking=False):
            raise HTTPException(
                503, "注册服务繁忙，请稍后重试", headers={"Retry-After": "2"})
        try:
            created = db.create_user(username, password, is_admin=False)
            authenticated = (
                db.authenticate_and_create_session(username, password, SESSION_DAYS)
                if created else None)
        finally:
            auth_slots.release()
        if not created:
            raise HTTPException(409, "用户名已存在或非法（不能包含空格、引号或斜杠）")
        if not authenticated:
            raise HTTPException(409, "账号创建后凭据已被管理员变更，请重新登录")
        _, token = authenticated
        resp = JSONResponse({"ok": True, "username": username, "is_admin": False})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=cfg.secure_cookie,
                        max_age=SESSION_DAYS * 86400, path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        rate_limiter.check("logout", _client_ip(request), 30, 60)
        token = request.cookies.get(SESSION_COOKIE)
        if token and _SESSION_TOKEN_RE.fullmatch(token):
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

    @app.get("/api/health")
    def health():
        backend = _backend_status()
        ready = bool(backend.get("ready"))
        # 公开探针只给可用性，不泄漏镜像名、Docker 版本或 daemon stderr。
        payload = {"ok": ready, "llm_ready": cfg.llm_ready,
                   "judge": {"ready": ready,
                             "mode": "docker" if multiuser else "local"}}
        return JSONResponse(payload, status_code=200 if payload["ok"] else 503)

    # ---------- 设置：个人 API key（多用户模式） ----------

    @app.get("/api/me/llm")
    def me_llm(user: dict = Depends(require_user)):
        return {"using_own_key": bool(user.get("api_key")),
                "server_configured": cfg.llm_ready,
                "daily_limit": cfg.daily_limit_per_user}

    @app.post("/api/me/llm")
    def me_llm_save(body: ApiKeyReq, user: dict = Depends(require_user)):
        if not multiuser:
            raise HTTPException(400, "单机模式请使用全局 LLM 配置")
        key = body.api_key.strip()
        clean = None if is_placeholder_key(key) else key
        db.set_user_api_key(user["username"], clean)
        return {"ok": True, "using_own_key": clean is not None}

    # ---------- 设置：LLM 全局配置（仅管理员） ----------

    @app.get("/api/llm/config")
    def llm_config(admin: dict = Depends(require_admin)):
        with llm_config_lock:
            k, base_url, model = cfg.api_key, cfg.base_url, cfg.model
        masked = ""
        if k:
            masked = k[:5] + "***" + k[-4:] if len(k) > 12 else "***"
        return {"base_url": base_url, "model": model,
                "api_key_masked": masked,
                "configured": not is_placeholder_key(k)}

    @app.post("/api/llm/config")
    def llm_config_save(body: LLMConfigReq, admin: dict = Depends(require_admin)):
        with llm_config_lock:
            base_url = body.base_url.strip()
            model = body.model.strip()
            api_key = body.api_key.strip()  # Web 表单留空表示保留现有 key
            try:
                update_llm_config(
                    api_key=api_key or None, base_url=base_url, model=model,
                    temperature=cfg.temperature, timeout=cfg.timeout)
                effective = load_config()
            except ConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
            # 文件写入与热更新同属临界区，避免并发请求造成磁盘/内存不一致。
            cfg.api_key = effective.api_key
            cfg.base_url = effective.base_url
            cfg.model = effective.model
            cfg.temperature = effective.temperature
            cfg.timeout = effective.timeout
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
        except (LLMQuotaExceeded, LLMBusy):
            raise
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
        with llm_config_lock:
            key, base_url, current_model = cfg.api_key, cfg.base_url, cfg.model
        if not key:
            raise HTTPException(400, "请先填写 API key 并保存")
        try:
            with httpx.stream(
                    "GET", f"{base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {key}"}, timeout=30) as resp:
                if resp.status_code == 401:
                    raise HTTPException(401, "API key 无效")
                resp.raise_for_status()
                raw = bytearray()
                for chunk in resp.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1 << 20:
                        raise HTTPException(502, "模型列表响应超过 1MB 上限")
                payload = _strict_json_loads(raw)
            data = payload.get("data", []) if isinstance(payload, dict) else []
            ids = sorted(m.get("id", "") for m in data if isinstance(m, dict))
            return {"models": [i for i in ids if i], "current": current_model}
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
    def admin_add_user(body: AdminUserReq, admin: dict = Depends(require_admin)):
        username = body.username.strip()
        password = body.password
        if len(password) < 8:
            raise HTTPException(400, "密码至少 8 位")
        if not db.create_user(username, password, body.is_admin):
            raise HTTPException(400, "用户名已存在或非法（勿含空格/引号/斜杠）")
        return {"ok": True}

    @app.post("/api/admin/users/{username}/passwd")
    def admin_passwd(username: str, body: PasswordReq,
                     admin: dict = Depends(require_admin)):
        password = body.password
        if len(password) < 8:
            raise HTTPException(400, "密码至少 8 位")
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
    def fs_browse(path: str = Query("~", max_length=4096),
                  admin: dict = Depends(require_admin)):
        """服务器目录浏览（管理员工具）。返回子目录与可导入文件统计。"""
        from ..extract import SUPPORTED_EXT

        p = Path(path).expanduser()
        if not p.exists():
            raise HTTPException(404, f"路径不存在: {p}")
        if not p.is_dir():
            raise HTTPException(400, "请提供目录路径")
        dirs, importable = [], []
        try:
            children = heapq.nsmallest(
                1001, p.iterdir(), key=lambda item: str(item))
            truncated = len(children) > 1000
            for ch in children[:1000]:
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
                "importable_count": len(importable), "truncated": truncated}

    @app.get("/api/sources")
    def list_sources(admin: dict = Depends(require_admin)):
        rows = db.execute(
            "SELECT id, path, title, n_cards, ingested_at FROM sources ORDER BY id DESC"
        ).fetchall()
        return {"sources": [
            {"id": r["id"], "title": r["title"] or Path(r["path"]).name,
             "path": r["path"], "n_cards": r["n_cards"],
             "ingested_at": r["ingested_at"]} for r in rows]}

    @app.delete("/api/sources/{sid}")
    def delete_source(sid: int, admin: dict = Depends(require_admin)):
        if not db.delete_source(sid):
            raise HTTPException(404, "来源不存在")
        return {"ok": True}

    @app.post("/api/ingest/start")
    async def ingest_start(body: PathReq, request: Request,
                           admin: dict = Depends(require_admin)):
        """SSE：流式导入知识目录（AI thinking / 输出 / 进度全事件）。"""
        path = body.path.strip()
        cancel_event = threading.Event()
        llm = _user_llm(admin, cancel_event.is_set)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..ingest import ingest_dir

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue(maxsize=256)

            def on_event(kind: str, data: dict) -> None:
                _queue_put(q, (kind, data),
                           critical=kind not in ("thinking_delta", "content_delta"))

            def run():
                try:
                    stats = ingest_dir(Path(path), db, cfg, llm,
                                       on_event=on_event, sleep_s=0.05,
                                       cancel_check=cancel_event.is_set)
                    _queue_put(q, ("all_done", stats), critical=True)
                except Exception as e:
                    _queue_put(q, ("error", {"message": str(e)}), critical=True)
                finally:
                    _queue_put(q, ("_end", {}), critical=True)

            task = loop.run_in_executor(None, run)
            async for kind, data in _drain_queue(
                    request, q, task, cancel_event, {"_end"}):
                if kind == "_end":
                    break
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/problems/generate")
    async def problems_generate(body: GenerateReq, request: Request,
                                admin: dict = Depends(require_admin)):
        """AI 出题：生成 → 沙箱实跑参考解生成期望 → 自洽才入库（SSE）。"""
        _require_judge_backend()
        brief = body.brief.strip()
        cancel_event = threading.Event()
        llm = _user_llm(admin, cancel_event.is_set)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..problem_gen import generate_problem

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue(maxsize=256)

            def on_event(kind: str, data: dict) -> None:
                _queue_put(q, (kind, data),
                           critical=kind not in ("thinking_delta", "content_delta"))

            def run():
                try:
                    out = generate_problem(llm, brief,
                                           cpp_compiler=cfg.cpp_compiler,
                                           on_event=on_event,
                                           docker_image=cfg.judge_docker_image,
                                           cancel_check=cancel_event.is_set,
                                           reference_runner=lambda *a, **kw:
                                           _limited_reference(
                                               admin["username"], *a, **kw))
                    db.upsert_problem(out["problem"], out["cases"])
                    _queue_put(q, ("saved", {"problem_id": out["problem"]["id"],
                                              "title": out["problem"]["title"],
                                              "difficulty": out["problem"]["difficulty"],
                                              "tags": out["problem"]["tags"],
                                              "n_cases": len(out["cases"])}), critical=True)
                except JudgeInfrastructureError as e:
                    _queue_put(q, ("error", {
                        "message": f"判题基础设施不可用: {e}"}), critical=True)
                except Exception as e:
                    _queue_put(q, ("error", {"message": str(e)[:400]}), critical=True)
                finally:
                    _queue_put(q, ("_end", {}), critical=True)

            task = loop.run_in_executor(None, run)
            async for kind, data in _drain_queue(
                    request, q, task, cancel_event, {"_end"}):
                if kind == "_end":
                    break
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------- Coding 题导入：JSON 直接导入 / AI 适配目录（仅管理员） ----------

    @app.post("/api/problems/import_json")
    async def problems_import_json(body: PathReq, request: Request,
                                   admin: dict = Depends(require_admin)):
        """从目录导入符合 schema 的 JSON 题目文件（无需 AI，含用例直接入库）。"""
        path = body.path.strip()
        root = Path(path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"目录不存在: {root}")

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()
        cancel_event = threading.Event()

        async def gen():
            q: _q.Queue = _q.Queue(maxsize=256)

            def run():
                try:
                    files = heapq.nsmallest(
                        1000,
                        (item for item in root.glob("*.json") if item.is_file()),
                        key=lambda item: str(item))
                    _queue_put(q, ("total", {"n": len(files)}), critical=True)
                    ok = fail = 0
                    for fp in files:
                        if cancel_event.is_set():
                            break
                        try:
                            if fp.stat().st_size > 10 << 20:
                                raise ValueError("JSON 文件超过 10MB 上限")
                            obj = _strict_json_loads(fp.read_text(encoding="utf-8"))
                            cases = [{"input": c["input"], "output": c["output"],
                                      "sample": c.get("sample", False)}
                                     for c in obj["test_cases"]]
                            db.upsert_problem(obj, cases)
                            ok += 1
                            _queue_put(q, ("imported", {"file": fp.name,
                                                        "id": obj.get("id", "?"),
                                                        "title": obj.get("title", "")}),
                                       critical=True)
                        except Exception as e:
                            fail += 1
                            _queue_put(q, ("failed", {"file": fp.name,
                                                       "error": str(e)[:200]}),
                                       critical=True)
                    _queue_put(q, ("all_done", {"ok": ok, "fail": fail}), critical=True)
                except Exception as e:
                    _queue_put(q, ("error", {"message": str(e)[:300]}), critical=True)
                finally:
                    _queue_put(q, ("_end", {}), critical=True)

            task = loop.run_in_executor(None, run)
            async for kind, data in _drain_queue(
                    request, q, task, cancel_event, {"_end"}):
                if kind == "_end":
                    break
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/problems/adapt")
    async def problems_adapt(body: AdaptReq, request: Request,
                             admin: dict = Depends(require_admin)):
        """AI 适配导入：目录中每个 .md/.txt 视为一道题的描述，
        AI 生成参考解+用例并沙箱自洽验证后入库。"""
        _require_judge_backend()
        path = body.path.strip()
        limit = body.limit
        root = Path(path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"目录不存在: {root}")
        cancel_event = threading.Event()
        llm = _user_llm(admin, cancel_event.is_set)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..problem_gen import generate_problem

        import asyncio
        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue(maxsize=256)

            def on_event(kind: str, data: dict) -> None:
                _queue_put(q, (kind, data),
                           critical=kind not in ("thinking_delta", "content_delta"))

            def run():
                try:
                    files = heapq.nsmallest(
                        1000,
                        (item for item in itertools.chain(
                            root.glob("*.md"), root.glob("*.txt"))
                         if item.is_file()),
                        key=lambda item: str(item))
                    if limit > 0:
                        files = files[:limit]
                    else:
                        files = files[:1000]
                    _queue_put(q, ("total", {"n": len(files)}), critical=True)
                    ok = fail = 0
                    for fp in files:
                        if cancel_event.is_set():
                            break
                        _queue_put(q, ("file_start", {"file": fp.name}), critical=True)
                        try:
                            if fp.stat().st_size > 2 << 20:
                                raise ValueError("题目描述文件超过 2MB 上限")
                            with fp.open("r", encoding="utf-8", errors="ignore") as source:
                                brief = source.read(4001)
                            if len(brief) > 4000:
                                brief = brief[:4000]
                            brief = ("根据以下题目描述生成判题题（保留题意，规范输入输出格式，"
                                     f"设计参考解与用例）：\n\n{brief}")
                            out = generate_problem(
                                llm, brief, cpp_compiler=cfg.cpp_compiler,
                                on_event=on_event,
                                docker_image=cfg.judge_docker_image,
                                cancel_check=cancel_event.is_set,
                                reference_runner=lambda *a, **kw:
                                _limited_reference(
                                    admin["username"], *a, **kw))
                            db.upsert_problem(out["problem"], out["cases"])
                            ok += 1
                            _queue_put(q, ("saved", {"file": fp.name,
                                                      "problem_id": out["problem"]["id"],
                                                      "title": out["problem"]["title"],
                                                      "n_cases": len(out["cases"])}),
                                       critical=True)
                        except (LLMQuotaExceeded, LLMBusy, LLMCancelled):
                            raise
                        except JudgeInfrastructureError as e:
                            fail += 1
                            _queue_put(q, ("failed", {"file": fp.name,
                                                       "error": f"判题基础设施不可用: {e}"}),
                                       critical=True)
                        except Exception as e:
                            fail += 1
                            _queue_put(q, ("failed", {"file": fp.name,
                                                       "error": str(e)[:250]}),
                                       critical=True)
                    _queue_put(q, ("all_done", {"ok": ok, "fail": fail}), critical=True)
                except Exception as e:
                    _queue_put(q, ("error", {"message": str(e)[:300]}), critical=True)
                finally:
                    _queue_put(q, ("_end", {}), critical=True)

            task = loop.run_in_executor(None, run)
            async for kind, data in _drain_queue(
                    request, q, task, cancel_event, {"_end"}):
                if kind == "_end":
                    break
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"

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
        if not db.delete_problem(pid):
            raise HTTPException(404, "题目不存在")
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
    def submit(req: SubmitReq, request: Request,
               user: dict = Depends(require_user)):
        _require_judge_backend()
        rate_limiter.check("judge", user["username"] or _client_ip(request), 30, 60)
        snapshot = db.get_problem_snapshot(req.problem_id)
        if not snapshot:
            raise HTTPException(404, "题目不存在")
        p, cases = snapshot
        if not p.get("valid", True):
            raise HTTPException(
                409, "题目数据不合法，已隔离，请管理员重新导入: "
                + str(p.get("validation_error") or "未知错误")[:300])
        if req.language not in p["languages"]:
            raise HTTPException(400, f"该题不支持 {req.language}")

        if not cases:
            raise HTTPException(409, "题目没有测试用例，请联系管理员修复题库")
        res = _http_judge(
            user["username"],
            req.code, req.language, cases,
            time_limit_ms=p["time_limit_ms"], mem_limit_mb=p["mem_limit_mb"],
            cpp_compiler=cfg.cpp_compiler, docker_image=cfg.judge_docker_image,
        )
        public_cases = _public_case_results(res.cases, cases)
        case_summary = "\n".join(
            f"用例{c['idx']}: {c['verdict']} ({c['time_ms']}ms)" for c in public_cases)
        sid = db.record_submission(
            req.problem_id, req.language, req.code, res.verdict,
            {"cases": public_cases,
             "compile_error": res.compile_error},
            res.max_time_ms, user_id=user["username"],
            problem_revision=p["revision"],
        )
        return {
            "submission_id": sid, "verdict": res.verdict,
            "max_time_ms": res.max_time_ms,
            "compile_error": res.compile_error,
            "cases": public_cases,
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
        if not p or sub["problem_revision"] != p.get("revision"):
            raise HTTPException(409, "该提交属于旧题目版本，不能用当前题面生成点评")
        from ..review import review_code

        cs = sub["detail"].get("cases", [])
        case_summary = "\n".join(f"用例{c.get('idx')}: {c.get('verdict')}" for c in cs[:12])
        try:
            r = review_code(llm, p, sub["code"], sub["language"], sub["verdict"], case_summary)
        except (LLMQuotaExceeded, LLMBusy, LLMCancelled):
            raise
        except Exception as e:
            raise HTTPException(502, f"点评失败: {e}")
        db.set_review(sid, r, user_id=user["username"])
        return {"review": r}

    # ---------- 八股 ----------

    @app.get("/api/cards/next")
    def cards_next(tags: str = Query("", max_length=500),
                   n: int = Query(5, ge=1, le=100), difficulty: int = -1,
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
    def cards_learn(tags: str = Query("", max_length=500),
                    n: int = Query(10, ge=1, le=100), include_learned: bool = False,
                    user: dict = Depends(require_user)):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
        cards = db.pick_learn_cards(user_id=user["username"], tags=tag_list, n=n,
                                    only_unlearned=not include_learned)
        return {"cards": cards}  # 学习模式直接给全部字段（含要点与讲解缓存）

    @app.post("/api/cards/{cid}/learn")
    def card_mark_learned(cid: str, body: LearnReq,
                          user: dict = Depends(require_user)):
        learned = body.learned
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
            return {"explanation": _strict_json_loads(card["explanation"]), "cached": True}
        llm = _user_llm(user)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..quiz import explain_card

        try:
            out = explain_card(llm, card, with_reasoning=True)
            result = out["json"]
        except (LLMQuotaExceeded, LLMBusy, LLMCancelled):
            raise
        except Exception as e:
            raise HTTPException(502, f"讲解生成失败: {e}")
        if not db.set_explanation(
                cid, json.dumps(result, ensure_ascii=False),
                expected_revision=card["content_revision"]):
            raise HTTPException(409, "题卡在生成讲解期间已更新，请重试")
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
        except (LLMQuotaExceeded, LLMBusy, LLMCancelled):
            raise
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
        except (LLMQuotaExceeded, LLMBusy, LLMCancelled):
            raise
        except Exception as e:
            raise HTTPException(502, f"追问评分失败: {e}")
        db.record_attempt(req.card_id, req.question, req.answer,
                          result.get("score", 0), result, mode="follow_up",
                          user_id=user["username"])
        result["reasoning"] = out["reasoning"]
        return result

    # ---------- AI 修复代码（直接给修复后代码，不引导） ----------

    @app.post("/api/fix/{pid}")
    async def fix_code_endpoint(pid: str, body: FixReq, request: Request,
                                user: dict = Depends(require_user)):
        snapshot = db.get_problem_snapshot(pid)
        if not snapshot:
            raise HTTPException(404, "题目不存在")
        problem, cases = snapshot
        if not problem.get("valid", True):
            raise HTTPException(409, "题目数据不合法，已隔离，请管理员重新导入")
        rate_limiter.check("fix", user["username"], 10, 60)
        cancel_event = threading.Event()
        llm = _user_llm(user, cancel_event.is_set)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import fix_code

        code = body.code
        language = body.language
        verdict = body.verdict
        detail = body.detail

        import asyncio, queue as _q
        loop = asyncio.get_event_loop()

        async def gen():
            q = _q.Queue(maxsize=256)

            def on_event(kind, text):
                _queue_put(q, (kind, text),
                           critical=kind not in ("thinking_delta", "content_delta"))

            def run():
                try:
                    reply = fix_code(llm, problem, code, language, verdict, detail, on_event=on_event)
                    _queue_put(q, ("done", reply), critical=True)
                except Exception as e:
                    _queue_put(q, ("error", str(e)[:300]), critical=True)

            task = loop.run_in_executor(None, run)
            async for item in _drain_queue(
                    request, q, task, cancel_event, {"done", "error"}):
                if item[0] == "done":
                    yield "data: " + json.dumps({"event": "reply", "code": item[1]}, ensure_ascii=False) + "\n\n"
                    break
                elif item[0] == "error":
                    yield "data: " + json.dumps({"event": "error", "message": item[1]}, ensure_ascii=False) + "\n\n"
                    break
                else:
                    yield "data: " + json.dumps({"event": item[0], "text": item[1]}, ensure_ascii=False) + "\n\n"

        from fastapi.responses import StreamingResponse
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------- AI 判题（工具增强的结构化判定报告，SSE） ----------

    @app.post("/api/ai_judge/{pid}")
    async def ai_judge(pid: str, body: AIJudgeReq, request: Request,
                       user: dict = Depends(require_user)):
        _require_judge_backend()
        snapshot = db.get_problem_snapshot(pid)
        if not snapshot:
            raise HTTPException(404, "题目不存在")
        problem, cases = snapshot
        if not problem.get("valid", True):
            raise HTTPException(409, "题目数据不合法，已隔离，请管理员重新导入")
        if body.language not in problem["languages"]:
            raise HTTPException(400, f"该题不支持 {body.language}")
        rate_limiter.check("ai_judge", user["username"], 10, 60)
        cancel_event = threading.Event()
        llm = _user_llm(user, cancel_event.is_set)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import (AI_JUDGE_SYSTEM, SandboxTools, ai_judge_report,
                            build_problem_context, chat_step)

        code = body.code
        language = body.language
        if not cases:
            raise HTTPException(409, "题目没有测试用例，请联系管理员修复题库")

        # 权威判定在调用模型前由服务端对“这份代码 + 这批用例”直接执行；
        # 后续模型只负责解释，任何模型字段都不能覆盖这个结果。
        import asyncio

        authoritative = await asyncio.to_thread(
            _http_judge, user["username"], code, language, cases,
            time_limit_ms=problem["time_limit_ms"],
            mem_limit_mb=problem["mem_limit_mb"],
            cpp_compiler=cfg.cpp_compiler,
            docker_image=cfg.judge_docker_image)
        authoritative_cases = _public_case_results(authoritative.cases, cases)
        last_verdict = authoritative.verdict
        last_detail = "\n".join(
            f"用例{c['idx']}: {c['verdict']}" for c in authoritative_cases[:12])

        context = build_problem_context(problem, code, language, last_verdict, last_detail)
        history = [
            {"role": "system", "content": AI_JUDGE_SYSTEM + "\n\n" + context},
            {"role": "user", "content": "请判定我这份代码（先用工具验证，再出报告）。"},
        ]
        def tool_snapshot(requested_pid: str):
            return (problem, cases) if requested_pid == pid else db.get_problem_snapshot(requested_pid)

        tools = SandboxTools(
            get_problem=lambda p: db.get_problem(p),
            load_cases=lambda p: _load_cases(db, p),
            cpp_compiler=cfg.cpp_compiler,
            docker_image=cfg.judge_docker_image,
            judge=lambda *args, **kwargs: _limited_judge(
                user["username"], *args, **kwargs),
            problem_snapshot=tool_snapshot,
        )

        import queue as _q

        loop = asyncio.get_event_loop()

        async def gen():
            q: _q.Queue = _q.Queue(maxsize=256)

            def on_event(kind: str, data: dict) -> None:
                _queue_put(q, (kind, data),
                           critical=kind not in ("thinking_delta", "content_delta"))

            def run():
                try:
                    _queue_put(q, ("sandbox", {
                        "verdict": authoritative.verdict,
                        "max_time_ms": authoritative.max_time_ms,
                        "compile_error": authoritative.compile_error,
                        "cases": authoritative_cases,
                    }), critical=True)
                    result = chat_step(llm, tools, history, on_event=on_event)
                    report = ai_judge_report(result.reply)
                    if not report:
                        report = {
                            "complexity": {"time": "未能解析", "space": "未能解析"},
                            "boundary_analysis": "模型报告格式异常",
                            "better_solution": {"exists": False, "name": "",
                                                "complexity": "", "why_better": "",
                                                "hint": ""},
                            "related_knowledge": [], "interview_tips": [],
                            "summary": (result.reply or "模型未返回可解析报告")[:4000],
                        }
                    model_verdict = report.get("sandbox_verdict")
                    report["sandbox_verdict"] = authoritative.verdict
                    report["authoritative_sandbox"] = {
                        "verdict": authoritative.verdict,
                        "max_time_ms": authoritative.max_time_ms,
                        "cases": authoritative_cases,
                    }
                    if model_verdict and model_verdict != authoritative.verdict:
                        report["authority_note"] = (
                            f"模型原判定 {model_verdict} 已被权威沙箱结果 "
                            f"{authoritative.verdict} 覆盖")
                    _queue_put(q, ("report", {"report": report}), critical=True)
                    # AI 判题留档到独立表（不进 submissions：避免污染错题本与统计）
                    db.record_ai_judgement(
                        pid, language, code, authoritative.verdict,
                        {"tool_trace": [t.summary for t in result.tool_trace],
                         "report": report, "authoritative": True},
                        user_id=user["username"],
                        problem_revision=problem["revision"])
                except Exception as e:
                    _queue_put(q, ("error", {"message": str(e)}), critical=True)
                finally:
                    _queue_put(q, ("done", {}), critical=True)

            task = loop.run_in_executor(None, run)
            async for kind, data in _drain_queue(
                    request, q, task, cancel_event, {"done", "error"}):
                yield "data: " + json.dumps({"event": kind, **data},
                                            ensure_ascii=False) + "\n\n"

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------- AI 讲题教练（沙箱工具循环 + SSE 流式） ----------

    @app.post("/api/chat/problem/{pid}")
    async def chat_problem(pid: str, req: ChatReq, request: Request,
                           user: dict = Depends(require_user)):
        _require_judge_backend()
        snapshot = db.get_problem_snapshot(pid)
        if not snapshot:
            raise HTTPException(404, "题目不存在")
        problem, cases = snapshot
        if not problem.get("valid", True):
            raise HTTPException(409, "题目数据不合法，已隔离，请管理员重新导入")
        rate_limiter.check("chat", user["username"], 20, 60)
        cancel_event = threading.Event()
        llm = _user_llm(user, cancel_event.is_set)
        if llm is None:
            raise HTTPException(503, _LLM_HINT)
        from ..chat import COACH_SYSTEM, SandboxTools, build_problem_context, chat_step

        last_verdict, last_detail = None, None
        if req.last_submission_id:
            sub = db.get_submission(req.last_submission_id, user_id=user["username"])
            if sub and sub["problem_revision"] == problem.get("revision"):
                last_verdict = sub["verdict"]
                cs = sub["detail"].get("cases", [])
                last_detail = "\n".join(
                    f"用例{c.get('idx')}: {c.get('verdict')}" for c in cs[:12])

        context = build_problem_context(
            problem, req.code or "", req.language, last_verdict, last_detail)
        history = [{"role": "system", "content": COACH_SYSTEM + "\n\n" + context}]
        history += [m.model_dump() for m in req.messages]

        def tool_snapshot(requested_pid: str):
            return (problem, cases) if requested_pid == pid else db.get_problem_snapshot(requested_pid)

        tools = SandboxTools(
            get_problem=lambda pid_: db.get_problem(pid_),
            load_cases=lambda pid_: _load_cases(db, pid_),
            cpp_compiler=cfg.cpp_compiler,
            docker_image=cfg.judge_docker_image,
            judge=lambda *args, **kwargs: _limited_judge(
                user["username"], *args, **kwargs),
            problem_snapshot=tool_snapshot,
        )

        import asyncio

        loop = asyncio.get_event_loop()

        async def gen():
            import queue

            q: queue.Queue = queue.Queue(maxsize=256)

            def on_event(kind: str, data: dict) -> None:
                _queue_put(q, (kind, data),
                           critical=kind not in ("thinking_delta", "content_delta"))

            def run():
                try:
                    chat_step(llm, tools, history, on_event=on_event)
                except Exception as e:
                    _queue_put(q, ("error", {"message": str(e)}), critical=True)
                finally:
                    _queue_put(q, ("done", {}), critical=True)

            task = loop.run_in_executor(None, run)
            async for kind, data in _drain_queue(
                    request, q, task, cancel_event, {"done", "error"}):
                payload = json.dumps({"event": kind, **data}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        from fastapi.responses import StreamingResponse

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": "请求参数格式或长度不合法"})

    @app.exception_handler(LLMQuotaExceeded)
    async def llm_quota_error(request, exc):
        return JSONResponse(status_code=429, content={"detail": str(exc)},
                            headers={"Retry-After": "3600"})

    @app.exception_handler(LLMBusy)
    async def llm_busy_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)},
                            headers={"Retry-After": "5"})

    @app.exception_handler(LLMCancelled)
    async def llm_cancelled_error(request, exc):
        return JSONResponse(status_code=408, content={"detail": "请求已取消"})

    @app.exception_handler(JudgeInfrastructureError)
    async def judge_infrastructure_error(request, exc):
        _LOG.error("判题基础设施异常: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "判题服务暂不可用，请联系管理员"},
            headers={"Retry-After": "10"})

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


_LLM_HINT = ("LLM 未配置：请在 data/config.yaml 填写 api_key（或设置 PREPDOJO_API_KEY），"
             "重启后可用 AI 点评与八股打分。判题功能不受影响。")


def _load_cases(db: DB, pid: str) -> list[dict]:
    rows = db.execute(
        "SELECT input, expected_output, is_sample FROM test_cases "
        "WHERE problem_id=? ORDER BY idx",
        (pid,),
    ).fetchall()
    return [{"input": r["input"], "output": r["expected_output"],
             "sample": bool(r["is_sample"])} for r in rows]


def _public_case_results(results: list, cases: list[dict]) -> list[dict[str, Any]]:
    """只公开样例的输入输出；隐藏用例仅给判定与耗时，阻断回显探测。"""
    public: list[dict[str, Any]] = []
    for result in results:
        item: dict[str, Any] = {
            "idx": result.idx,
            "verdict": result.verdict,
            "time_ms": result.time_ms,
            "timed_out": bool(result.timed_out),
        }
        case = cases[result.idx] if 0 <= result.idx < len(cases) else {}
        if case.get("sample"):
            item.update({
                "input": str(case.get("input", ""))[:2000],
                "expected": result.expected[:2000],
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:1000],
            })
        public.append(item)
    return public
