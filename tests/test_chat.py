"""AI 讲题教练与面试官风格测试（不依赖真实 LLM）。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from prepdojo.chat import SandboxTools, build_problem_context  # noqa: E402
from prepdojo.config import Config  # noqa: E402
from prepdojo.db import DB  # noqa: E402
from prepdojo.quiz import grade_answer, grade_followup, grade_system  # noqa: E402
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
    # 工具只能看到判定事实，不能借回显代码探测隐藏输入/期望输出。
    assert "expected" not in wa_case and "actual" not in wa_case


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


def test_grader_normalizes_score_even_with_reasoning():
    class FakeLLM:
        def stream_json(self, *_args, **_kwargs):
            return {"json": {"score": 99, "per_point": "bad", "missed": "bad",
                             "overall": {"unexpected": True}},
                    "reasoning": "thinking"}

    card = {"question": "q", "answer_points": ["p"]}
    graded = grade_answer(FakeLLM(), card, "a", with_reasoning=True)
    assert graded["json"]["score"] == 10.0
    assert graded["json"]["per_point"] == [] and graded["json"]["missed"] == []
    assert graded["reasoning"] == "thinking"

    followup = grade_followup(
        FakeLLM(), card, "why", "a", with_reasoning=True)
    assert followup["json"]["score"] == 10.0


def test_grader_rejects_nonfinite_scores_and_wrong_field_types():
    from prepdojo.quiz import _normalize_grade

    result = _normalize_grade({
        "score": float("nan"),
        "per_point": [{"point": {"bad": True}, "covered": "false",
                       "comment": ["bad"]}],
        "missed": [{"bad": True}, "有效遗漏"],
        "overall": {"bad": True},
    })
    assert result["score"] == 0.0
    assert result["per_point"] == [{"point": "", "covered": False, "comment": ""}]
    assert result["missed"] == ["有效遗漏"]
    assert result["overall"] == ""


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


def test_ai_judge_prompt_and_report_parse():
    from prepdojo.chat import AI_JUDGE_SYSTEM, ai_judge_report
    assert "run_problem_case" in AI_JUDGE_SYSTEM
    assert "sandbox_verdict" in AI_JUDGE_SYSTEM
    assert "related_knowledge" in AI_JUDGE_SYSTEM
    # 报告解析：带壳 / 纯 JSON / 垃圾
    good = '```json\n{"sandbox_verdict": "AC", "complexity": {"time": "O(n)"}}\n```'
    assert ai_judge_report(good)["sandbox_verdict"] == "AC"
    assert ai_judge_report('前言 {"sandbox_verdict": "WA"} 后记')["sandbox_verdict"] == "WA"
    assert ai_judge_report("not json at all") is None


def test_ai_judge_endpoint_guard(tmp_path):
    db = DB(tmp_path / "aj.db")
    load_seed_dir(db, SEEDS)
    cfg = Config(api_key="", db_path=tmp_path / "aj.db")
    c = TestClient(create_app(cfg, db))
    assert c.post("/api/ai_judge/cp-001",
                  json={"code": "print(1)", "language": "python"}).status_code == 503
    assert c.post("/api/ai_judge/cp-xxx",
                  json={"code": "print(1)", "language": "python"}).status_code == 404
