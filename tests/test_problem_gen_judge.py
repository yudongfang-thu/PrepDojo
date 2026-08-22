"""AI 出题与判题沙箱衔接的回归测试。"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo import judge, problem_gen  # noqa: E402
from prepdojo.llm import LLMCancelled  # noqa: E402
from prepdojo.problem_gen import (  # noqa: E402
    _run_reference, _schema_errors, generate_problem,
)


def _valid_problem() -> dict:
    return {
        "title": "回显整数",
        "difficulty": "easy",
        "tags": ["输入输出"],
        "statement": "读入一个整数并原样输出。",
        "time_limit_ms": 1000,
        "reference_python": "print(int(input()))",
        "test_inputs": [f"{i}\n" for i in range(6)],
    }


def test_generated_problem_schema_is_strict():
    obj = _valid_problem()
    obj["difficulty"] = "简单"
    obj["test_inputs"] = ["1\n"]
    obj["unexpected"] = True
    errors = "；".join(_schema_errors(obj))
    assert "difficulty" in errors
    assert "6-10" in errors
    assert "额外字段" in errors


def test_generate_problem_repairs_invalid_schema():
    class FakeLLM:
        def __init__(self):
            self.responses = [
                {"json": {"reference_python": "print(1)", "test_inputs": []}},
                {"json": _valid_problem()},
            ]
            self.users = []

        def stream_json(self, system, user, max_tokens, on_delta):
            self.users.append(user)
            return self.responses.pop(0)

    llm = FakeLLM()
    result = generate_problem(llm, "出一道回显题", fix_rounds=1)
    assert len(llm.users) == 2
    assert "不符合 schema" in llm.users[1]
    assert len(result["cases"]) == 6


def test_reference_runner_passes_its_tempdir_as_docker_cwd(monkeypatch):
    calls = []

    def fake_run(cmd, stdin_text, wall_timeout_s, mem_limit_mb, cwd=None,
                 docker_image="", outer_timeout_s=None):
        calls.append((cmd, cwd, docker_image))
        return stdin_text, "", 0, 1, False

    monkeypatch.setattr(judge, "_run_once", fake_run)
    results = _run_reference("print(input())", ["1\n", "2\n"], 1000, "judge-image")
    assert len(results) == 2
    assert all(cwd and Path(cmd[1]).parent == Path(cwd) for cmd, cwd, _ in calls)
    assert all(image == "judge-image" for _, _, image in calls)


def test_reference_verification_has_total_budget(monkeypatch):
    calls = 0

    def slow_success(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return "ok", "", 0, 20, False

    monkeypatch.setattr(problem_gen, "MAX_SUBMISSION_WALL_S", 0.01)
    monkeypatch.setattr(judge, "_run_once", slow_success)
    results = _run_reference("print('ok')", ["1\n", "2\n"], 1000)
    assert calls == 1 and results[-1]["timed_out"] is True


def test_generation_honors_cancellation_before_cost():
    class NeverCalled:
        def stream_json(self, *_args, **_kwargs):
            raise AssertionError("取消后不应调用 LLM")

    with pytest.raises(LLMCancelled):
        generate_problem(NeverCalled(), "出题", cancel_check=lambda: True)
