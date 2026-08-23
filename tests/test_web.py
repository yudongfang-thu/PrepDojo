"""Web API 测试（TestClient，不依赖 LLM）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from prepdojo.config import Config  # noqa: E402
from prepdojo.db import DB  # noqa: E402
from prepdojo.seed_loader import load_seed_dir  # noqa: E402
from prepdojo.web.server import create_app  # noqa: E402

SEEDS = Path(__file__).resolve().parent.parent / "seeds" / "coding"


def make_client(tmp_path):
    db = DB(tmp_path / "web.db")
    load_seed_dir(db, SEEDS)
    cfg = Config(api_key="", db_path=tmp_path / "web.db")
    return TestClient(create_app(cfg, db)), db


def test_health_and_stats(tmp_path):
    c, _ = make_client(tmp_path)
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json()["llm_ready"] is False
    assert c.get("/api/stats").json()["problems"] == 20


def test_problems_and_detail(tmp_path):
    c, _ = make_client(tmp_path)
    ps = c.get("/api/problems").json()["problems"]
    assert len(ps) == 20
    assert [p["interview_priority"] for p in ps] == sorted(
        p["interview_priority"] for p in ps)
    d = c.get("/api/problems/cp-003").json()
    assert "statement" in d and d["n_cases"] >= 5
    assert d["leetcode_id"] == 53 and d["interview_priority"] == 2
    assert c.get("/api/problems/cp-xxx").status_code == 404


def test_submit_ac_and_wa(tmp_path):
    c, _ = make_client(tmp_path)
    good = "n=int(input());a=list(map(int,input().split()));cur=ans=a[0]\nfor x in a[1:]:\n    cur=max(x,cur+x);ans=max(ans,cur)\nprint(ans)"
    r = c.post("/api/submit", json={"problem_id": "cp-003", "language": "python", "code": good})
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "AC"
    bad = "n=int(input());print(0)"
    r2 = c.post("/api/submit", json={"problem_id": "cp-003", "language": "python", "code": bad})
    assert r2.status_code == 200 and r2.json()["verdict"] == "WA"
    # LLM 未配置时点评应明确报 503
    sid = r.json()["submission_id"]
    assert c.post(f"/api/review/{sid}").status_code == 503


def test_quiz_needs_llm(tmp_path):
    c, db = make_client(tmp_path)
    sid = db.upsert_source("/t.pdf", "s", "t")
    cid = db.insert_card("什么是 KV cache？", ["要点"], ["追问"], ["推理加速"], 2, sid, "x")
    cards = c.get("/api/cards/next").json()["cards"]
    assert len(cards) == 1 and "answer_points" not in cards[0]  # 不下发答案防偷看
    r = c.post("/api/quiz/grade", json={"card_id": cid, "answer": "缓存键值"})
    assert r.status_code == 503
