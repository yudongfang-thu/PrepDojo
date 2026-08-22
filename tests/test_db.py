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


def test_llm_usage_is_atomic_under_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = make_db(tmp_path)
    with ThreadPoolExecutor(max_workers=24) as pool:
        values = list(pool.map(lambda _: db.bump_llm_usage("alice"), range(500)))
    assert sorted(values) == list(range(1, 501))
    assert db.llm_usage_today("alice") == 500


def test_quota_check_and_increment_are_one_transaction(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = make_db(tmp_path)
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(
            lambda _: db.consume_llm_quota("alice", user_limit=25, global_limit=100),
            range(100)))
    assert sum(1 for result in results if result["ok"]) == 25
    assert db.llm_usage_today("alice") == 25


def test_deleted_username_is_tombstoned_and_password_reset_revokes_sessions(tmp_path):
    db = make_db(tmp_path)
    assert not db.create_user("local", "password123")
    assert db.create_user("alice", "password123")
    token = db.create_session("alice")
    assert db.session_user(token)["username"] == "alice"

    assert db.set_user_password("alice", "new-password123")
    assert db.session_user(token) is None
    assert db.verify_login("alice", "password123") is None
    assert db.verify_login("alice", "new-password123") is not None

    token2 = db.create_session("alice")
    assert db.delete_user("alice")
    assert db.get_user("alice") is None and db.session_user(token2) is None
    assert not db.create_user("alice", "another-password")


def test_problem_replacement_rolls_back_on_invalid_case(tmp_path):
    import pytest

    db = make_db(tmp_path)
    original = {"id": "cp-atomic", "title": "原题", "difficulty": "easy",
                "tags": ["事务"], "statement": "原题面", "languages": ["python"]}
    db.upsert_problem(original, [{"input": "1\n", "output": "1\n", "sample": True}])
    changed = {**original, "title": "不应落库"}
    with pytest.raises(ValueError, match="input/output"):
        db.upsert_problem(changed, [
            {"input": "2\n", "output": "2\n"},
            {"input": "3\n"},
        ])
    problem = db.get_problem("cp-atomic")
    assert problem["title"] == "原题"
    assert problem["n_cases"] == 1 and problem["samples"][0]["input"] == "1\n"
    with pytest.raises(ValueError, match="数量必须为 1-200"):
        db.upsert_problem(original, [])


def test_source_card_replacement_is_atomic(tmp_path):
    import pytest

    db = make_db(tmp_path)
    cards = [{"question": "旧问题", "answer_points": ["旧要点"],
              "follow_ups": [], "topic_tags": ["旧"], "difficulty": 2,
              "source_ref": "doc|1"}]
    old_id = db.replace_source_cards("/doc.md", "sha-old", "doc", cards)[0]
    with pytest.raises(ValueError, match="question"):
        db.replace_source_cards("/doc.md", "sha-new", "doc", [
            {**cards[0], "question": "临时问题"},
            {"answer_points": ["缺少问题"], "follow_ups": [],
             "topic_tags": ["坏"], "difficulty": 2, "source_ref": "doc|2"},
        ])
    assert db.source_sha("/doc.md") == "sha-old"
    assert db.get_card(old_id)["question"] == "旧问题"


def test_database_file_is_private(tmp_path):
    import stat

    db = make_db(tmp_path)
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600


def test_problem_schema_and_revision_isolate_old_results(tmp_path):
    import pytest

    db = make_db(tmp_path)
    problem = {"id": "cp-rev", "title": "版本题", "difficulty": "easy",
               "tags": ["旧标签"], "statement": "输出输入", "languages": ["python"]}
    cases = [{"input": "1\n", "output": "1\n", "sample": True}]
    db.upsert_problem(problem, cases)
    first_revision = db.get_problem("cp-rev")["revision"]
    sid = db.record_submission("cp-rev", "python", "print(input())", "AC", {}, 1)
    assert db.problem_status_map()["cp-rev"]["ever_ac"] is True

    # 展示元数据不改变判定版本。
    db.upsert_problem({**problem, "tags": ["新标签"], "title": "新标题"}, cases)
    assert db.get_problem("cp-rev")["revision"] == first_revision
    assert db.problem_status_map()["cp-rev"]["ever_ac"] is True

    # 题面/用例变化后，旧 AC 仍可审计但不计入当前版本。
    db.upsert_problem(
        {**problem, "statement": "输出输入的两倍"},
        [{"input": "1\n", "output": "2\n", "sample": True}],
    )
    current = db.get_problem("cp-rev")
    assert current["revision"] != first_revision
    assert db.list_problems()[0]["attempts"] == 0
    assert db.last_submission_code("cp-rev") is None
    assert db.get_submission(sid)["problem_revision"] == first_revision

    with pytest.raises(ValueError, match="languages"):
        db.upsert_problem({**problem, "languages": ["javascript"]}, cases)
    with pytest.raises(ValueError, match="input/output"):
        db.upsert_problem(problem, [{"input": 123, "output": "x"}])


def test_source_sync_preserves_stable_card_progress_and_explanation(tmp_path):
    db = make_db(tmp_path)
    original = [{"question": "原问题", "answer_points": ["原答案"],
                 "follow_ups": [], "topic_tags": ["测试"], "difficulty": 2,
                 "source_ref": "doc.md | 第1段"}]
    card_id = db.replace_source_cards("/doc.md", "sha-1", "doc", original)[0]
    assert db.mark_learned(card_id, True, user_id="alice")
    db.set_explanation(card_id, "缓存讲解")
    before = db.get_card(card_id, "alice")
    learned_at = before["learned_at"]
    old_revision = before["content_revision"]
    assert learned_at

    # 文件 SHA 变化但结构化内容相同：ID、进度、缓存均保留。
    new_id = db.replace_source_cards("/doc.md", "sha-2", "doc", original)[0]
    card = db.get_card(new_id, "alice")
    assert new_id == card_id
    assert card["learned"] is True and card["learned_at"] == learned_at
    assert card["explanation"] == "缓存讲解"

    # 同一定位的问答内容改变：学习进度保留，但旧讲解缓存失效。
    updated = [{**original[0], "question": "修正错别字"}]
    assert db.replace_source_cards("/doc.md", "sha-3", "doc", updated)[0] == card_id
    changed = db.get_card(card_id, "alice")
    assert changed["learned"] is True and changed["learned_at"] == learned_at
    assert changed["explanation"] is None
    assert not db.set_explanation(
        card_id, "过期并发讲解", expected_revision=old_revision)
    assert db.get_card(card_id, "alice")["explanation"] is None

    # 答案要点实质变化代表知识版本变化，旧“已学”状态不再沿用。
    changed_answer = [{**updated[0], "answer_points": ["全新答案"]}]
    db.replace_source_cards("/doc.md", "sha-4", "doc", changed_answer)
    assert db.get_card(card_id, "alice")["learned"] is False


def test_quiz_recent_window_compares_timestamps_not_iso_text(tmp_path):
    db = make_db(tmp_path)
    sid = db.upsert_source("/time.md", "sha", "time")
    cid = db.insert_card("时间题", ["答案"], [], ["测试"], 2, sid, "time|1")
    attempt = db.record_attempt(cid, "时间题", "答", 1, {})
    db.execute(
        "UPDATE quiz_attempts SET asked_at=date('now', '-3 days') || "
        "'T00:00:00+00:00' WHERE id=?", (attempt,))
    assert [card["id"] for card in db.pick_cards(exclude_seen_days=3)] == [cid]
