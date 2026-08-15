"""AI 讲题教练与面试官风格测试（不依赖真实 LLM）。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from prepdojo.chat import SandboxTools, build_problem_context  # noqa: E402
from prepdojo.config import Config  # noqa: E402
from prepdojo.db import DB  # noqa: E402
from prepdojo.quiz import grade_system  # noqa: E402
from prepdojo.seed_loader import load_seed_dir  # noqa: E402
from prepdojo.web.server import create_app  # noqa: E402

SEEDS = Path(__file__).resolve().parent.parent / "seeds" / "coding"


def make_tools(tmp_path):
    db = DB(tmp_path / "chat.db")
    load_seed_dir(db, SEEDS)
    return SandboxTools(
        get_problem=lambda pid: db.get_problem(pid),
        load_cases=lambda pid: [
            {"input": r["input"], "output": r["expected_output"]}
            for r in db.conn.execute(
                "SELECT input, expected_output FROM test_cases WHERE problem_id=? ORDER BY idx",
                (pid,)).fetchall()
        ],
    ), db


def test_tool_run_code():
    tools, _ = make_tools(Path("/tmp/prepdojo-test-tools"))
    out = json.loads(tools.run_code(
        "n=int(input());print(n*2)", "python", stdin="21"))
    assert out["stdout"].strip() == "42" and out["exit"] == "ok"


def test_tool_run_code_error():
    tools, _ = make_tools(Path("/tmp/prepdojo-test-tools"))
    out = json.loads(tools.run_code("1/0", "python", stdin=""))
    assert out["exit"] != "ok" and "ZeroDivision" in out["stderr"]


def test_tool_run_problem_case_ac_and_wa():
    tools, _ = make_tools(Path("/tmp/prepdojo-test-tools"))
    good = ("n=int(input());a=list(map(int,input().split()));"
            "cur=ans=a[0]\nfor x in a[1:]:\n    cur=max(x,cur+x);ans=max(ans,cur)\nprint(ans)")
    r = json.loads(tools.run_problem_case("cp-003", good, "python"))
    assert r["verdict"] == "AC" and all(c["verdict"] == "AC" for c in r["cases"])
    bad = "n=int(input());print(-99999)"
    r2 = json.loads(tools.run_problem_case("cp-003", bad, "python"))
    assert r2["verdict"] == "WA"
    wa_case = next(c for c in r2["cases"] if c["verdict"] == "WA")
    assert "expected" in wa_case and "actual" in wa_case  # 事实对比信息齐全


def test_tool_dispatch_unknown():
    tools, _ = make_tools(Path("/tmp/prepdojo-test-tools"))
    out, summ = tools.dispatch("nope", {})
    assert "error" in out


def test_build_problem_context():
    tools, db = make_tools(Path("/tmp/prepdojo-test-tools"))
    p = db.get_problem("cp-001")
    ctx = build_problem_context(p, "print(1)", "python", "WA", "用例0: WA")
    assert "cp-001" in ctx and "print(1)" in ctx and "WA" in ctx


def test_grader_styles():
    s_std = grade_system("standard")
    s_strict = grade_system("strict")
    s_press = grade_system("pressure")
    assert "资深技术面试官" in s_std
    assert "高标准" in s_strict and "放水" in s_strict
    assert "压力面" in s_press and "质疑" in s_press


def test_chat_endpoint_needs_llm(tmp_path):
    db = DB(tmp_path / "web2.db")
    load_seed_dir(db, SEEDS)
    cfg = Config(api_key="", db_path=tmp_path / "web2.db")
    c = TestClient(create_app(cfg, db))
    r = c.post("/api/chat/problem/cp-001", json={
        "messages": [{"role": "user", "content": "讲讲思路"}], "code": "", "language": "python"})
    assert r.status_code == 503
    assert c.post("/api/chat/problem/cp-xxx", json={
        "messages": [{"role": "user", "content": "hi"}]}).status_code == 404
