"""学习模式 / 错题本 / 讲解缓存测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from prepdojo.config import Config  # noqa: E402
from prepdojo.db import DB  # noqa: E402
from prepdojo.seed_loader import load_seed_dir  # noqa: E402
from prepdojo.web.server import create_app  # noqa: E402

SEEDS = Path(__file__).resolve().parent.parent / "seeds" / "coding"


def seed_cards(db):
    sid = db.upsert_source("/t.pdf", "s", "t")
    ids = []
    for i, (q, learned) in enumerate([
        ("什么是 KV cache？", 0), ("什么是 PagedAttention？", 1),
        ("LoRA 的原理？", 0), ("ZeRO 三阶段？", 1),
    ]):
        cid = db.insert_card(q, [f"要点{j}" for j in range(3)], ["追问"],
                             ["推理加速"] if i % 2 == 0 else ["分布式训练"],
                             2, sid, f"t.pdf|{q[:10]}")
        if learned:
            db.mark_learned(cid, True)
        ids.append(cid)
    return ids


def make_db(tmp_path):
    db = DB(tmp_path / "lrn.db")
    load_seed_dir(db, SEEDS)
    seed_cards(db)
    return db


def test_migration_adds_columns(tmp_path):
    """旧库（无 learned 列）打开后自动迁移。"""
    import sqlite3

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE cards (id TEXT PRIMARY KEY, question TEXT NOT NULL, "
                 "answer_points TEXT NOT NULL, follow_ups TEXT NOT NULL, "
                 "topic_tags TEXT NOT NULL, difficulty INTEGER NOT NULL DEFAULT 2, "
                 "source_id INTEGER, source_ref TEXT, created_at TEXT NOT NULL)")
    conn.commit(); conn.close()
    db = DB(p)  # 打开即迁移
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(cards)")}
    assert {"learned", "learned_at", "explanation"} <= cols


def test_mark_learned_and_progress(tmp_path):
    db = make_db(tmp_path)
    prog = db.learn_progress()
    assert prog["total"] == 4 and prog["learned"] == 2
    unlearned = [c for c in db.pick_learn_cards(n=10) if not c["learned"]]
    assert len(unlearned) == 2  # 默认只抽未学


def test_quiz_only_learned(tmp_path):
    db = make_db(tmp_path)
    cards = db.pick_cards(n=10, only_learned=True)
    assert cards and all(c["learned"] for c in cards)
    all_cards = db.pick_cards(n=10, only_learned=False)
    assert len(all_cards) == 4


def test_wrong_problem_book(tmp_path):
    db = make_db(tmp_path)
    # cp-001 AC 过，cp-003 只交了 WA，其余没交
    db.record_submission("cp-001", "python", "ok", "AC", {}, 10)
    db.record_submission("cp-003", "python", "bad", "WA", {}, 10)
    db.record_submission("cp-003", "python", "bad2", "WA", {}, 10)
    smap = db.problem_status_map()
    assert smap["cp-001"]["ever_ac"] is True
    assert smap["cp-003"]["ever_ac"] is False and smap["cp-003"]["attempts"] == 2
    assert db.wrong_problem_ids() == ["cp-003"]  # AC 即移出错题本


def test_learn_and_explain_endpoints(tmp_path):
    db = make_db(tmp_path)
    cfg = Config(api_key="", db_path=tmp_path / "lrn.db")
    c = TestClient(create_app(cfg, db))
    # 学习抽卡（含答案要点——学习模式可见）
    r = c.get("/api/cards/learn?n=10")
    cards = r.json()["cards"]
    assert all(not x["learned"] for x in cards) and "answer_points" in cards[0]
    # 标记已学
    cid = cards[0]["id"]
    assert c.post(f"/api/cards/{cid}/learn", json={"learned": True}).json()["learned"]
    assert c.get("/api/cards/progress").json()["learned"] == 3
    # 测验抽题默认仅已学，且不下发答案要点
    q = c.get("/api/cards/next?n=5").json()
    assert all("answer_points" not in x for x in q["cards"])
    assert all(x["learned"] for x in q["cards"])
    # 讲解未配置 LLM → 503
    assert c.get(f"/api/cards/{cid}/explain").status_code == 503
    # 题目状态与错题本
    probs = c.get("/api/problems").json()["problems"]
    p3 = next(p for p in probs if p["id"] == "cp-003") if any(
        p.get("attempts") for p in probs) else None


def test_submit_updates_wrong_book(tmp_path):
    db = make_db(tmp_path)
    cfg = Config(api_key="", db_path=tmp_path / "lrn.db")
    c = TestClient(create_app(cfg, db))
    bad = "print(0)"
    c.post("/api/submit", json={"problem_id": "cp-003", "language": "python", "code": bad})
    assert c.get("/api/problems/wrong").json()["wrong"][0]["id"] == "cp-003"
    good = ("n=int(input());a=list(map(int,input().split()));cur=ans=a[0]\n"
            "for x in a[1:]:\n    cur=max(x,cur+x);ans=max(ans,cur)\nprint(ans)")
    c.post("/api/submit", json={"problem_id": "cp-003", "language": "python", "code": good})
    assert c.get("/api/problems/wrong").json()["wrong"] == []  # AC 自动移出


def test_problem_generate_endpoint_guard(tmp_path):
    from prepdojo.db import DB as DB2
    from prepdojo.seed_loader import load_seed_dir as _ls

    db2 = DB2(tmp_path / "gen.db")
    _ls(db2, SEEDS)
    cfg2 = Config(api_key="", db_path=tmp_path / "gen.db")
    c2 = TestClient(create_app(cfg2, db2))
    assert c2.post("/api/problems/generate", json={"brief": ""}).status_code == 400
    assert c2.post("/api/problems/generate",
                   json={"brief": "出一道题"}).status_code == 503


def test_problem_gen_slug():
    from prepdojo.problem_gen import _slug
    assert _slug("Two Sum II!") == "two-sum-ii"
    assert _slug("中文题") == "gen"
