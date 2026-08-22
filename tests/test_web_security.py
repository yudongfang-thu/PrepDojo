"""Web 安全边界：隐藏用例、基础设施错误、AI 权威判定与响应头。"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yaml
from fastapi.testclient import TestClient

from prepdojo import config as config_module
from prepdojo.config import Config
from prepdojo.db import DB
from prepdojo.judge import CaseResult, JudgeResult
from prepdojo.web.server import create_app


def _problem(db: DB, pid: str = "cp-sec") -> None:
    db.upsert_problem(
        {"id": pid, "title": "安全测试", "difficulty": "easy", "tags": ["测试"],
         "statement": "回显输入", "languages": ["python"]},
        [
            {"input": "public\n", "output": "public\n", "sample": True},
            {"input": "SECRET_HIDDEN_INPUT\n", "output": "SECRET_EXPECTED\n"},
        ],
    )


def test_submit_redacts_hidden_case_from_response_and_database(tmp_path):
    db = DB(tmp_path / "web.db")
    _problem(db)
    client = TestClient(create_app(Config(db_path=db.path), db))
    response = client.post("/api/submit", json={
        "problem_id": "cp-sec", "language": "python", "code": "print(input())"})
    assert response.status_code == 200 and response.json()["verdict"] == "WA"
    payload = response.json()
    assert payload["cases"][0]["stdout"].strip() == "public"
    hidden = payload["cases"][1]
    assert set(hidden) == {"idx", "verdict", "time_ms", "timed_out"}
    assert "SECRET_" not in json.dumps(payload, ensure_ascii=False)

    stored = db.get_submission(payload["submission_id"])
    assert "SECRET_" not in json.dumps(stored["detail"], ensure_ascii=False)


def test_judge_infrastructure_error_is_503_and_not_recorded(tmp_path, monkeypatch):
    import prepdojo.web.server as server_mod
    from prepdojo.judge import JudgeInfrastructureError

    db = DB(tmp_path / "web.db")
    _problem(db)
    client = TestClient(create_app(Config(db_path=db.path), db))
    monkeypatch.setattr(
        server_mod, "judge_submission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            JudgeInfrastructureError("docker daemon down")))
    response = client.post("/api/submit", json={
        "problem_id": "cp-sec", "language": "python", "code": "print(1)"})
    assert response.status_code == 503
    assert response.json()["detail"] == "判题服务暂不可用，请联系管理员"
    assert "daemon" not in response.text
    assert db.stats()["submissions"] == 0


def test_ai_report_cannot_override_authoritative_sandbox(tmp_path, monkeypatch):
    import prepdojo.web.server as server_mod

    class LyingLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        def stream_chat(self, *_args, **_kwargs):
            report = {
                "sandbox_verdict": "AC",
                "complexity": {"time": "O(1)", "space": "O(1)"},
                "summary": "模型错误地认为通过",
            }
            yield {"type": "done", "content": json.dumps(report, ensure_ascii=False),
                   "reasoning": "", "tool_calls": []}

    monkeypatch.setattr(server_mod, "LLMClient", LyingLLM)
    db = DB(tmp_path / "web.db")
    _problem(db, "cp-ai-authority")
    cfg = Config(api_key="sk-test", db_path=db.path)
    client = TestClient(create_app(cfg, db))
    response = client.post("/api/ai_judge/cp-ai-authority", json={
        "language": "python", "code": "print('wrong')"})
    assert response.status_code == 200
    events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
              if line.startswith("data: ")]
    report = next(event["report"] for event in events if event["event"] == "report")
    assert report["sandbox_verdict"] == "WA"
    assert report["authoritative_sandbox"]["verdict"] == "WA"
    assert "覆盖" in report["authority_note"]
    row = db.execute(
        "SELECT verdict, detail FROM ai_judgements ORDER BY id DESC LIMIT 1").fetchone()
    assert row["verdict"] == "WA" and json.loads(row["detail"])["authoritative"] is True


def test_admin_user_list_never_contains_api_key_and_local_key_save_is_rejected(tmp_path):
    db = DB(tmp_path / "web.db")
    assert db.create_user("root", "password123", is_admin=True)
    assert db.create_user("alice", "password123")
    db.set_user_api_key("alice", "sk-personal-secret")
    cfg = Config(db_path=db.path)
    client = TestClient(create_app(cfg, db, multiuser=True))
    assert client.post("/api/auth/login", json={
        "username": "root", "password": "password123"}).status_code == 200
    users = client.get("/api/admin/users").json()["users"]
    alice = next(user for user in users if user["username"] == "alice")
    assert alice["has_api_key"] is True and "api_key" not in alice
    assert "sk-personal-secret" not in json.dumps(users)

    local = TestClient(create_app(Config(db_path=db.path), db, multiuser=False))
    assert local.post("/api/me/llm", json={"api_key": "sk-fake"}).status_code == 400


def test_security_headers_body_limit_and_secure_cookie(tmp_path):
    db = DB(tmp_path / "web.db")
    assert db.create_user("root", "password123", is_admin=True)
    cfg = Config(db_path=db.path, secure_cookie=True)
    client = TestClient(create_app(cfg, db, multiuser=True))
    login = client.post("/api/auth/login", json={
        "username": "root", "password": "password123"})
    assert login.status_code == 200 and "Secure" in login.headers["set-cookie"]
    assert login.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]
    assert login.headers["cache-control"] == "no-store"

    oversized = client.post(
        "/api/auth/login", content=b"x" * (512 * 1024 + 1),
        headers={"content-type": "application/json"})
    assert oversized.status_code == 413

    def chunked_body():
        for _ in range(6):
            yield b"x" * (100 * 1024)

    chunked = client.post(
        "/api/auth/login", content=chunked_body(),
        headers={"content-type": "application/json", "transfer-encoding": "chunked"})
    assert chunked.status_code == 413
    assert chunked.headers.get("connection") == "close"


def test_cross_site_mutation_is_rejected(tmp_path):
    db = DB(tmp_path / "web.db")
    assert db.create_user("root", "password123", is_admin=True)
    client = TestClient(create_app(Config(db_path=db.path), db, multiuser=True))
    response = client.post(
        "/api/auth/login",
        json={"username": "root", "password": "password123"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_ipv6_host_header_is_supported(tmp_path):
    db = DB(tmp_path / "web.db")
    client = TestClient(create_app(Config(db_path=db.path), db))
    response = client.get("/api/me", headers={"Host": "[::1]:8686"})
    assert response.status_code == 200
    assert client.get("/api/me", headers={"Host": "[::1]:bad"}).status_code == 400
    assert client.get("/api/me", headers={"Host": "localhost:99999"}).status_code == 400


def test_public_health_does_not_expose_backend_details(tmp_path, monkeypatch):
    import prepdojo.web.server as server_mod

    monkeypatch.setattr(server_mod, "judge_backend_status", lambda _image: {
        "ready": False, "image": "private/image:secret",
        "daemon_version": "99.1", "error": "sensitive docker stderr",
    })
    db = DB(tmp_path / "web.db")
    client = TestClient(create_app(
        Config(db_path=db.path, judge_docker_image="private/image:secret"),
        db, multiuser=True))
    response = client.get("/api/health")
    assert response.status_code == 503
    raw = response.text
    assert "private" not in raw and "99.1" not in raw and "stderr" not in raw
    assert client.get("/openapi.json").status_code == 404


def test_chat_aggregate_length_is_bounded(tmp_path):
    db = DB(tmp_path / "web.db")
    _problem(db)
    client = TestClient(create_app(Config(db_path=db.path), db))
    response = client.post("/api/chat/problem/cp-sec", json={
        "messages": [
            {"role": "user", "content": "x" * 11_000},
            {"role": "assistant", "content": "y" * 11_000},
            {"role": "user", "content": "z" * 11_000},
            {"role": "assistant", "content": "w" * 8_000},
        ],
    })
    assert response.status_code == 400


def test_blank_admin_paths_and_generation_brief_are_rejected(tmp_path):
    db = DB(tmp_path / "web.db")
    client = TestClient(create_app(Config(db_path=db.path), db))
    assert client.post(
        "/api/problems/import_json", json={"path": "   "}).status_code == 400
    assert client.post(
        "/api/ingest/start", json={"path": "\t"}).status_code == 400
    assert client.post(
        "/api/problems/generate", json={"brief": "   "}).status_code == 400


def test_llm_slot_is_released_when_quota_database_raises(tmp_path, monkeypatch):
    import prepdojo.web.server as server_mod

    class CallbackLLM:
        def __init__(self, *_args, before_request=None, after_request=None, **_kwargs):
            self.before_request = before_request
            self.after_request = after_request

        def chat(self, *_args, **_kwargs):
            self.before_request()
            try:
                return "通"
            finally:
                self.after_request()

    monkeypatch.setattr(server_mod, "LLMClient", CallbackLLM)
    db = DB(tmp_path / "web.db")
    original = db.consume_llm_quota
    monkeypatch.setattr(
        db, "consume_llm_quota",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    client = TestClient(create_app(Config(api_key="sk-test", db_path=db.path), db))
    for _ in range(4):
        response = client.post("/api/llm/test")
        assert response.status_code == 200 and response.json()["ok"] is False

    monkeypatch.setattr(db, "consume_llm_quota", original)
    recovered = client.post("/api/llm/test")
    assert recovered.status_code == 200 and recovered.json()["ok"] is True


def test_judge_per_user_concurrency_is_bounded(tmp_path, monkeypatch):
    import prepdojo.web.server as server_mod

    active = maximum = 0
    lock = threading.Lock()

    def slow_judge(*_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.15)
            return JudgeResult("AC", [CaseResult(0, "AC", 1)])
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(server_mod, "judge_submission", slow_judge)
    db = DB(tmp_path / "web.db")
    _problem(db)
    cfg = Config(
        db_path=db.path, judge_concurrency_per_user=1,
        judge_concurrency_global=2)
    app = create_app(cfg, db)

    def submit_once(_):
        with TestClient(app) as client:
            return client.post("/api/submit", json={
                "problem_id": "cp-sec", "language": "python", "code": "print(1)"}).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(submit_once, range(12)))
    assert maximum == 1
    assert 200 in statuses and 429 in statuses


def test_llm_config_save_preserves_environment_precedence_without_leaking_key(
        tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  api_key: disk-key\n  base_url: https://disk.example/v1\n"
        "  model: disk-model\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    monkeypatch.setenv("PREPDOJO_API_KEY", "env-secret-key")
    monkeypatch.setenv("PREPDOJO_MODEL", "env-model")
    cfg = config_module.load_config(path)
    db = DB(tmp_path / "web.db")
    client = TestClient(create_app(cfg, db))
    response = client.post("/api/llm/config", json={
        "api_key": "", "base_url": "https://new.example/v1", "model": "disk-new"})
    assert response.status_code == 200
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert stored["llm"]["api_key"] == "disk-key"
    assert "env-secret-key" not in path.read_text(encoding="utf-8")
    assert stored["llm"]["model"] == "disk-new"
    assert cfg.api_key == "env-secret-key" and cfg.model == "env-model"


def test_llm_per_user_concurrency_is_bounded(tmp_path, monkeypatch):
    import prepdojo.web.server as server_mod

    active = maximum = 0
    lock = threading.Lock()

    class SlowLLM:
        def __init__(self, *_args, before_request=None, after_request=None, **_kwargs):
            self.before_request = before_request
            self.after_request = after_request

        def chat(self, *_args, **_kwargs):
            nonlocal active, maximum
            self.before_request()
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.15)
                return "通"
            finally:
                with lock:
                    active -= 1
                self.after_request()

    monkeypatch.setattr(server_mod, "LLMClient", SlowLLM)
    db = DB(tmp_path / "web.db")
    app = create_app(Config(
        api_key="sk-test", db_path=db.path,
        llm_concurrency_per_user=1, llm_concurrency_global=2), db)

    def call_once(_):
        with TestClient(app) as client:
            return client.post("/api/llm/test").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(call_once, range(8)))
    assert maximum == 1
    assert 200 in statuses and 503 in statuses


def test_forged_session_cookie_does_not_write_database(tmp_path):
    db = DB(tmp_path / "web.db")
    client = TestClient(create_app(Config(db_path=db.path), db, multiuser=True))
    before = db.conn.total_changes
    client.cookies.set("prepdojo_session", "A" * 43)
    assert client.get("/api/me").status_code == 401
    assert db.conn.total_changes == before


def test_rate_limiter_identity_map_is_bounded():
    from fastapi import HTTPException
    from prepdojo.web.server import MAX_RATE_LIMIT_IDENTITIES, _RateLimiter

    limiter = _RateLimiter()
    rejected = 0
    for index in range(MAX_RATE_LIMIT_IDENTITIES + 10):
        try:
            limiter.check("login", f"198.51.{index // 256}.{index % 256}", 1, 300)
        except HTTPException:
            rejected += 1
    assert len(limiter._hits) == MAX_RATE_LIMIT_IDENTITIES
    assert rejected == 10


def test_fs_browse_result_is_bounded(tmp_path):
    root = tmp_path / "many-directories"
    root.mkdir()
    for index in range(1002):
        (root / f"dir-{index:04d}").mkdir()

    db = DB(tmp_path / "web.db")
    client = TestClient(create_app(Config(db_path=db.path), db))
    response = client.get("/api/fs/browse", params={"path": str(root)})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["dirs"]) == 1000
    assert payload["truncated"] is True
