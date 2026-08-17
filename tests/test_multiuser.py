"""多用户模式测试：认证、数据隔离、IDOR、admin 门禁、配额、旧库迁移。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from prepdojo.config import Config  # noqa: E402
from prepdojo.db import DB  # noqa: E402
from prepdojo.seed_loader import load_seed_dir  # noqa: E402
from prepdojo.web.server import create_app  # noqa: E402

SEEDS = Path(__file__).resolve().parent.parent / "seeds" / "coding"


def make_app(tmp_path, *, multiuser=True, **cfg_kw):
    db = DB(tmp_path / "m.db")
    load_seed_dir(db, SEEDS)
    cfg = Config(db_path=tmp_path / "m.db", **cfg_kw)
    app = create_app(cfg, db, multiuser=multiuser)
    return TestClient(app), db


def add_user(db, name, pw="pass1234", admin=False):
    assert db.create_user(name, pw, admin)
    return db.get_user(name)


def login(client, name, pw="pass1234"):
    r = client.post("/api/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 认证 ----------

def test_unauthenticated_gets_401(tmp_path):
    c, _ = make_app(tmp_path)
    assert c.get("/api/problems").status_code == 401
    assert c.get("/api/stats").status_code == 401
    # health 不需要登录（前端徽标用）
    assert c.get("/api/health").status_code == 200


def test_login_logout_flow(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")
    assert c.post("/api/auth/login",
                  json={"username": "alice", "password": "wrong!"}).status_code == 401
    me = login(c, "alice")
    assert me["username"] == "alice" and me["is_admin"] is False
    r = c.get("/api/me")
    assert r.status_code == 200 and r.json()["username"] == "alice"
    c.post("/api/auth/logout")
    assert c.get("/api/problems").status_code == 401


def test_local_mode_no_auth(tmp_path):
    c, _ = make_app(tmp_path, multiuser=False)
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["multiuser"] is False and r.json()["username"] == "local"
    assert c.get("/api/problems").status_code == 200


# ---------- 数据隔离 ----------

def test_submission_and_wrong_book_isolation(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")
    add_user(db, "bob")
    db.upsert_problem(
        {"id": "cp-iso", "title": "回显", "difficulty": "easy", "tags": ["测试"],
         "statement": "读一行原样输出", "languages": ["python"]},
        [{"input": "hello\n", "output": "hello\n", "sample": True}])

    login(c, "alice")
    r = c.post("/api/submit", json={"problem_id": "cp-iso", "language": "python",
                                    "code": "print('wrong')"})
    assert r.status_code == 200 and r.json()["verdict"] == "WA"
    alice_sid = r.json()["submission_id"]
    # Alice 的题列表：未 AC；错题本含该题
    assert any(p["id"] == "cp-iso" and not p["ever_ac"] for p in
               c.get("/api/problems").json()["problems"])
    assert any(p["id"] == "cp-iso" for p in c.get("/api/problems/wrong").json()["wrong"])

    login(c, "bob")
    r = c.post("/api/submit", json={"problem_id": "cp-iso", "language": "python",
                                    "code": "print(input())"})
    assert r.json()["verdict"] == "AC"
    # Bob 视角：已 AC、错题本为空
    assert any(p["id"] == "cp-iso" and p["ever_ac"] for p in
               c.get("/api/problems").json()["problems"])
    assert not any(p["id"] == "cp-iso" for p in c.get("/api/problems/wrong").json()["wrong"])
    # IDOR：bob 看不到 alice 的提交
    assert c.get("/api/stats").json()["submissions"] == 1  # 只有 bob 自己的


def test_idor_cannot_review_others_submission(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")
    add_user(db, "bob")
    db.upsert_problem(
        {"id": "cp-iso2", "title": "回显", "difficulty": "easy", "tags": ["测试"],
         "statement": "读一行原样输出", "languages": ["python"]},
        [{"input": "x\n", "output": "x\n"}])
    login(c, "alice")
    sid = c.post("/api/submit", json={"problem_id": "cp-iso2", "language": "python",
                                      "code": "print('w')"}).json()["submission_id"]
    login(c, "bob")
    assert c.post(f"/api/review/{sid}").status_code == 404  # 非本人提交视为不存在
    # AI 判题上下文也不喂别人的提交（带 last_submission_id 只影响上下文，不越权返回数据）
    login(c, "alice")
    assert db.get_submission(sid, user_id="bob") is None
    assert db.get_submission(sid, user_id="alice") is not None


def test_learn_state_and_quiz_window_per_user(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")
    add_user(db, "bob")
    sid = db.upsert_source("/tmp/x.pdf", "sha", "x")
    cid = db.insert_card("什么是 A？", ["要点"], ["追问"], ["RAG"], 2, sid, "x|Q")

    # alice 标记已学；bob 视角仍未学
    login(c, "alice")
    assert c.post(f"/api/cards/{cid}/learn", json={"learned": True}).status_code == 200
    assert c.get(f"/api/cards/{cid}").json()["learned"] is True
    login(c, "bob")
    assert c.get(f"/api/cards/{cid}").json()["learned"] is False

    # alice 练过 → 3 天窗口内不再抽到；bob 不受影响
    db.record_attempt(cid, "q", "a", 5.0, {}, user_id="alice")
    assert db.pick_cards(user_id="alice") == []
    assert len(db.pick_cards(user_id="bob")) == 1

    # 学习进度按人
    pa = db.learn_progress("alice")
    pb = db.learn_progress("bob")
    assert pa["learned"] == 1 and pb["learned"] == 0


def test_stats_per_user(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")
    add_user(db, "bob")
    db.record_submission("cp-001", "python", "x", "WA", {}, 10, user_id="alice")
    login(c, "bob")
    s = c.get("/api/stats").json()
    assert s["submissions"] == 0 and s["problems"] == 20  # 个人计数为 0，共享题库可见


# ---------- admin 门禁 ----------

def test_admin_gates(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")          # 普通成员
    add_user(db, "root", admin=True)

    login(c, "alice")
    for method, path, kw in [
        ("get", "/api/llm/config", {}),
        ("post", "/api/llm/config", {"json": {"base_url": "http://x", "model": "m"}}),
        ("get", "/api/llm/models", {}),
        ("get", "/api/fs/browse", {}),
        ("post", "/api/ingest/start", {"json": {"path": "/tmp"}}),
        ("post", "/api/problems/generate", {"json": {"brief": "x"}}),
        ("post", "/api/problems/adapt", {"json": {"path": "/tmp"}}),
        ("post", "/api/problems/import_json", {"json": {"path": "/tmp"}}),
        ("delete", "/api/sources/1", {}),
        ("delete", "/api/problems/cp-001", {}),
        ("get", "/api/admin/users", {}),
    ]:
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 403, f"{method} {path} 应为 403，实际 {r.status_code}"

    login(c, "root")
    assert c.get("/api/llm/config").status_code == 200
    assert c.get("/api/fs/browse").status_code == 200
    assert c.get("/api/admin/users").status_code == 200


def test_admin_user_management(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "root", admin=True)
    login(c, "root")
    r = c.post("/api/admin/users", json={"username": "newbie", "password": "abcd1234"})
    assert r.status_code == 200
    assert c.post("/api/admin/users",
                  json={"username": "newbie", "password": "abcd1234"}).status_code == 400
    assert c.post("/api/admin/users/newbie/passwd",
                  json={"password": "wxyz5678"}).status_code == 200
    login(c, "newbie", "wxyz5678")
    assert c.delete("/api/admin/users/newbie").status_code == 403  # 非 admin
    login(c, "root")
    assert c.delete("/api/admin/users/root").status_code == 400   # 不能删自己
    assert c.delete("/api/admin/users/newbie").status_code == 200
    assert db.get_user("newbie") is None


def test_personal_api_key_storage(tmp_path):
    c, db = make_app(tmp_path)
    add_user(db, "alice")
    login(c, "alice")
    assert c.get("/api/me/llm").json()["using_own_key"] is False
    c.post("/api/me/llm", json={"api_key": "sk-my-own-key-123"})
    assert c.get("/api/me/llm").json()["using_own_key"] is True
    assert db.get_user("alice")["api_key"] == "sk-my-own-key-123"
    # 清空 → 回退服务器 key
    c.post("/api/me/llm", json={"api_key": ""})
    assert c.get("/api/me/llm").json()["using_own_key"] is False


# ---------- 用量配额 ----------

def test_daily_llm_quota_429(tmp_path):
    # base_url 指向必然连接失败的本地端口：前两次调用走配额（LLM 构造成功、
    # 实际请求立即失败返回 502），第三次超限直接 429
    c, db = make_app(tmp_path, api_key="sk-test-quota",
                     base_url="http://127.0.0.1:9", daily_limit_per_user=2)
    add_user(db, "alice")
    login(c, "alice")
    sid = db.upsert_source("/tmp/q.pdf", "sha", "q")
    cid = db.insert_card("什么是 B？", ["要点"], ["追问"], ["RAG"], 2, sid, "q|Q")
    codes = [c.get(f"/api/cards/{cid}/explain").status_code for _ in range(3)]
    assert codes[0] == 502 and codes[1] == 502  # 前两次：配额放行，LLM 网络失败
    assert codes[2] == 429                       # 第三次：超限
    assert db.llm_usage_today("alice") == 2


# ---------- 旧库迁移 ----------

def test_migrate_old_singleuser_db(tmp_path):
    """旧版单用户库（无 user_id 列、learned 在 cards 上）升级后数据归属 local。"""
    import sqlite3

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
    CREATE TABLE cards (id TEXT PRIMARY KEY, question TEXT, answer_points TEXT,
      follow_ups TEXT, topic_tags TEXT, difficulty INTEGER, source_id INTEGER,
      source_ref TEXT, created_at TEXT,
      learned INTEGER NOT NULL DEFAULT 0, learned_at TEXT, explanation TEXT);
    CREATE TABLE submissions (id INTEGER PRIMARY KEY AUTOINCREMENT,
      problem_id TEXT, language TEXT, code TEXT, verdict TEXT, detail TEXT,
      runtime_ms INTEGER, reviewed INTEGER DEFAULT 0, review TEXT,
      submitted_at TEXT);
    INSERT INTO cards VALUES ('kc-old', 'q', '[]', '[]', '["旧"]', 2, NULL, NULL,
      '2026-01-01', 1, '2026-01-01', NULL);
    INSERT INTO submissions (problem_id, language, code, verdict, submitted_at)
      VALUES ('cp-001', 'python', 'x', 'WA', '2026-01-01');
    """)
    raw.commit()
    raw.close()

    db = DB(path)
    # 旧提交归属 local；旧 learned 状态迁入 card_learn_state
    assert db.get_submission(1, user_id="local") is not None
    assert db.get_submission(1, user_id="alice") is None
    assert db.pick_cards(user_id="local", tags=["旧"])[0]["learned"] is True
    assert db.pick_cards(user_id="alice", tags=["旧"])[0]["learned"] is False
    assert db.stats("local")["submissions"] == 1
    # 新用户体系就绪
    assert db.create_user("root", "pass1234", True)
    assert db.verify_login("root", "pass1234")["is_admin"] is True
    assert db.verify_login("root", "bad") is None


def _reg_client(tmp_path, registration="code", code="LAB2026"):
    """构造指定注册模式的登录客户端。"""
    import importlib

    import prepdojo.config as cfgmod
    importlib.reload(cfgmod)
    from prepdojo.db import DB as DB2
    from prepdojo.web.server import create_app as _create

    db = DB2(tmp_path / "reg.db")
    db.create_user("admin", "adminpass", is_admin=True)
    cfg = cfgmod.Config(api_key="", db_path=tmp_path / "reg.db")
    cfg.multiuser = True
    cfg.registration = registration
    cfg.registration_code = code
    return TestClient(_create(cfg, db, multiuser=True))


def test_registration_mode_endpoint(tmp_path):
    c = _reg_client(tmp_path, registration="code")
    d = c.get("/api/auth/registration_mode").json()
    assert d == {"mode": "code", "multiuser": True}


def test_register_with_code(tmp_path):
    c = _reg_client(tmp_path, registration="code", code="LAB2026")
    # 错误邀请码
    r = c.post("/api/auth/register",
               json={"username": "alice", "password": "123456", "code": "WRONG"})
    assert r.status_code == 403
    # 正确邀请码 → 自动登录（cookie 生效）
    r2 = c.post("/api/auth/register",
                json={"username": "alice", "password": "123456", "code": "LAB2026"})
    assert r2.status_code == 200 and r2.json()["is_admin"] is False
    me = c.get("/api/me")
    assert me.status_code == 200 and me.json()["username"] == "alice"


def test_register_open_and_off(tmp_path):
    c = _reg_client(tmp_path, registration="open")
    assert c.post("/api/auth/register",
                  json={"username": "bob", "password": "123456"}).status_code == 200
    c2 = _reg_client(tmp_path, registration="off")
    assert c2.post("/api/auth/register",
                   json={"username": "carol", "password": "123456"}).status_code == 403


def test_register_validation(tmp_path):
    c = _reg_client(tmp_path, registration="open")
    # 用户名过短 / 密码过短 / 重复注册
    assert c.post("/api/auth/register",
                  json={"username": "a", "password": "123456"}).status_code == 400
    assert c.post("/api/auth/register",
                  json={"username": "dave", "password": "123"}).status_code == 400
    assert c.post("/api/auth/register",
                  json={"username": "dave", "password": "123456"}).status_code == 200
    assert c.post("/api/auth/register",
                  json={"username": "dave", "password": "123456"}).status_code == 409
