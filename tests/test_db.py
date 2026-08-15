"""数据库与种子载入测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo.db import DB  # noqa: E402
from prepdojo.seed_loader import load_seed_dir  # noqa: E402

SEEDS = Path(__file__).resolve().parent.parent / "seeds" / "coding"


def make_db(tmp_path):
    return DB(tmp_path / "t.db")


def test_card_crud_and_pick(tmp_path):
    db = make_db(tmp_path)
    sid = db.upsert_source("/tmp/a.pdf", "sha1", "a")
    cid = db.insert_card("什么是 X？", ["要点1", "要点2"], ["追问1"], ["RAG"], 2, sid, "a.pdf|Q")
    c = db.get_card(cid)
    assert c["answer_points"] == ["要点1", "要点2"]
    picked = db.pick_cards(tags=["RAG"], n=5)
    assert len(picked) == 1
    assert db.pick_cards(tags=["不存在的标签"]) == []
    db.record_attempt(cid, "什么是 X？", "我的回答", 7.5, {"overall": "ok"}, mode="test")
    # 练过之后默认排除（3 天窗口）
    assert db.pick_cards() == []
    s = db.stats()
    assert s["cards"] == 1 and s["quiz_attempts"] == 1


def test_problem_upsert_and_submission(tmp_path):
    db = make_db(tmp_path)
    db.upsert_problem(
        {"id": "cp-999", "title": "测试题", "difficulty": "easy", "tags": ["数组"],
         "statement": "题面", "time_limit_ms": 3000, "mem_limit_mb": 256,
         "languages": ["python"]},
        [{"input": "1\n", "output": "1\n", "sample": True}],
    )
    p = db.get_problem("cp-999")
    assert p["n_cases"] == 1 and p["samples"][0]["input"] == "1\n"
    assert db.list_problems()[0]["n_cases"] == 1
    sid = db.record_submission("cp-999", "python", "print(1)", "AC",
                               {"cases": []}, 12)
    db.set_review(sid, {"summary": "不错"})
    sub = db.get_submission(sid)
    assert sub["verdict"] == "AC" and sub["review"]["summary"] == "不错"


def test_seed_loading(tmp_path):
    db = make_db(tmp_path)
    n = load_seed_dir(db, SEEDS)
    assert n == 20
    p = db.get_problem("cp-001")
    assert p and p["n_cases"] >= 5
    assert p["samples"], "第一用例应标记为样例"


def test_write_persists_across_processes(tmp_path):
    """回归测试：写操作必须真实落盘（独立连接可见），防止 commit 丢失再次发生。"""
    import sqlite3

    from prepdojo.seed_loader import load_seed_dir

    db_path = tmp_path / "persist.db"
    db = DB(db_path)
    load_seed_dir(db, SEEDS)
    db.close()  # 模拟进程退出
    fresh = sqlite3.connect(db_path)  # 独立连接验证
    n = fresh.execute("SELECT COUNT(*) FROM coding_problems").fetchone()[0]
    assert n == 20, f"数据未持久化：仅 {n} 行"


def test_source_sha_incremental_lookup(tmp_path):
    """回归测试：同一文件二次接入应命中 sha 跳过（此前列名 bug 会 IndexError）。"""
    db = make_db(tmp_path)
    assert db.source_sha("/not/exist.pdf") is None  # 空表路径
    db.upsert_source("/a/b.pdf", "abc123", "b")
    assert db.source_sha("/a/b.pdf") == "abc123"  # 命中路径（曾在此崩溃）
    db.upsert_source("/a/b.pdf", "new456", "b")
    assert db.source_sha("/a/b.pdf") == "new456"
