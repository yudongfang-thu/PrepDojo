"""AI 出题：描述 → 题面 + 参考解 + 测试输入 → 沙箱实跑参考解生成期望输出。

事实兜底：期望输出永远来自参考解的真实运行（与 seeds 生成机制一致）；
参考解跑挂则把错误喂回 AI 修复，最多 fix_rounds 轮；全部用例通过才入库。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from .judge import MAX_SUBMISSION_WALL_S, judge_submission
from .llm import LLMCancelled, LLMClient

GEN_SYSTEM = """（思考从简：不要反复推敲，直接构造题目并输出，把 token 留给最终 JSON。）
你是资深算法面试官，为本地判题训练场生成一道可自动判定的编程题。
硬性要求：
1. IO 风格：标准输入读、标准输出写（不要函数签名式）。
2. statement 用中文，包含：题目描述、输入格式、输出格式、数据范围，至少一个样例。
3. 答案必须唯一可判定（无多解歧义；如有多个合法输出请定义 tie-break，如"最靠左的"）。
4. reference_python：完整可运行的 Python3 参考解（stdin 读、stdout 写），不依赖第三方库。
5. test_inputs：6-10 个完整 stdin 字符串，覆盖：基础样例、最小规模边界、单元素、特殊/极端值、（可能的话）最大规模压测。
6. time_limit_ms：给 5000，除非你明确预期需要更长。
输出 JSON：
{"title": "中文题名", "difficulty": "easy|medium|hard", "tags": ["数组","..."],
 "statement": "完整题面（含输入/输出格式与样例）",
 "time_limit_ms": 5000,
 "reference_python": "完整代码",
 "test_inputs": ["完整stdin文本", ...]}"""

FIX_SYSTEM = """（思考从简，尽快输出修复后的完整 JSON。）
你生成的编程题参考解在判题沙箱中运行失败。以下是失败信息，请修复参考解
（或在不改变题意的前提下修正测试输入），重新输出完整 JSON（schema 同前）。"""


def _slug(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:20].lower() or "gen"


_GEN_FIELDS = {
    "title", "difficulty", "tags", "statement", "time_limit_ms",
    "reference_python", "test_inputs",
}


def _schema_errors(obj: Any) -> list[str]:
    """严格校验模型输出，避免隐式转换把畸形 JSON 带入沙箱或数据库。"""
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON object"]
    errors: list[str] = []
    missing = sorted(_GEN_FIELDS - set(obj))
    extra = sorted(set(obj) - _GEN_FIELDS)
    if missing:
        errors.append("缺少字段: " + ", ".join(missing))
    if extra:
        errors.append("不允许额外字段: " + ", ".join(extra))

    for key, max_len in (("title", 100), ("statement", 50_000),
                         ("reference_python", 100_000)):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} 必须是非空字符串")
        elif len(value) > max_len:
            errors.append(f"{key} 长度不能超过 {max_len}")

    difficulty = obj.get("difficulty")
    if not isinstance(difficulty, str) or difficulty not in {"easy", "medium", "hard"}:
        errors.append("difficulty 必须是 easy、medium 或 hard")

    tags = obj.get("tags")
    if not isinstance(tags, list) or not (1 <= len(tags) <= 10):
        errors.append("tags 必须是包含 1-10 项的字符串数组")
    elif any(not isinstance(tag, str) or not tag.strip() or len(tag) > 30 for tag in tags):
        errors.append("tags 每项必须是长度 1-30 的非空字符串")

    time_limit = obj.get("time_limit_ms")
    if type(time_limit) is not int or not (100 <= time_limit <= 60_000):
        errors.append("time_limit_ms 必须是 100-60000 的整数")

    inputs = obj.get("test_inputs")
    if not isinstance(inputs, list) or not (6 <= len(inputs) <= 10):
        errors.append("test_inputs 必须是包含 6-10 项的字符串数组")
    elif any(not isinstance(item, str) or len(item) > 1_000_000 for item in inputs):
        errors.append("test_inputs 每项必须是长度不超过 1000000 的字符串")
    return errors


def _run_reference(code: str, inputs: list[str], time_limit_ms: int,
                   docker_image: str = "", cancel_check=None) -> list[dict]:
    """逐用例运行参考解（不短路），返回每用例 stdout/stderr/rc/耗时。"""
    import tempfile
    from pathlib import Path

    from .judge import _run_once

    deadline = time.monotonic() + MAX_SUBMISSION_WALL_S
    with tempfile.TemporaryDirectory(prefix="prepdojo-gen-") as td:
        entry = Path(td) / "main.py"
        entry.write_text(code, encoding="utf-8")
        cmd = ["python3", str(entry)]
        results = []
        for s in inputs:
            if cancel_check and cancel_check():
                raise LLMCancelled("出题验证已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append({"stdout": "", "stderr": "整题验证总时间已超时",
                                "rc": -1, "ms": int(MAX_SUBMISSION_WALL_S * 1000),
                                "timed_out": True})
                break
            out, err, rc, ms, timed_out = _run_once(
                cmd, s, min(time_limit_ms / 1000, remaining), 512, cwd=td,
                docker_image=docker_image, outer_timeout_s=remaining)
            results.append({"stdout": out, "stderr": err, "rc": rc,
                            "ms": ms, "timed_out": timed_out})
            if rc != 0 or timed_out:
                break
        return results


def generate_problem(
    llm: LLMClient,
    brief: str,
    cpp_compiler: str = "clang++",
    on_event: Optional[Callable[[str, dict], None]] = None,
    fix_rounds: int = 2,
    docker_image: str = "",
    cancel_check=None,
    reference_runner=None,
) -> dict[str, Any]:
    """brief：用户的题目描述或出题要求。返回入库后的题目信息。

    on_event：thinking_delta / content_delta / verify_start / verify_case /
    verify_fix / done / error
    """
    import time as _time

    def emit(kind: str, **data) -> None:
        if on_event:
            on_event(kind, data)

    user_msg = f"""【出题需求】
{brief}

请按 system 要求生成题目 JSON。"""
    obj: Optional[dict] = None

    for attempt in range(fix_rounds + 1):
        if cancel_check and cancel_check():
            raise LLMCancelled("出题任务已取消")
        if attempt == 0:
            out = llm.stream_json(GEN_SYSTEM, user_msg, max_tokens=30000,
                                  on_delta=lambda t, x: emit(
                                      "thinking_delta" if t == "reasoning_delta" else "content_delta",
                                      text=x))
        else:
            out = llm.stream_json(FIX_SYSTEM, user_msg, max_tokens=30000,
                                  on_delta=lambda t, x: emit(
                                      "thinking_delta" if t == "reasoning_delta" else "content_delta",
                                      text=x))
        obj = out.get("json") if isinstance(out, dict) else None
        schema_errors = _schema_errors(obj)
        if schema_errors:
            detail = "；".join(schema_errors)
            emit("verify_fix", errors=detail[:400])
            user_msg = f"""【出题需求】
{brief}

【上次生成的 JSON 不符合 schema】
{detail}

请修复后输出完整 JSON。"""
            continue

        emit("verify_start", attempt=attempt, n_cases=len(obj["test_inputs"]))
        inputs = [s if s.endswith("\n") else s + "\n"
                  for s in obj["test_inputs"]]
        tl = int(obj.get("time_limit_ms", 5000))
        runner = reference_runner or _run_reference
        results = runner(
            obj["reference_python"], inputs, tl, docker_image=docker_image,
            cancel_check=cancel_check)
        bad = [(i, r) for i, r in enumerate(results)
               if r["rc"] != 0 or r["timed_out"]]
        if not bad:  # 全部跑通：期望输出 = 参考解实际输出
            detail = " | ".join(f"#{i} {r['ms']}ms" for i, r in enumerate(results))
            emit("verify_case", ok=True, detail=detail[:500])
            return _finalize(obj, inputs, [r["stdout"] for r in results], emit)
        err_detail = "\n".join(
            f"用例#{i} stdin={inputs[i][:120]!r} exit={r['rc']} "
            f"timed_out={r['timed_out']} stderr={r['stderr'][:200]}"
            for i, r in bad)
        emit("verify_fix", errors=err_detail[:400])
        user_msg = f"""【出题需求】
{brief}

【上次生成的内容有误，判题沙箱反馈】
{err_detail}

请修复后输出完整 JSON。"""
        _time.sleep(0.2)

    raise RuntimeError(f"AI 出题经 {fix_rounds + 1} 轮仍未通过沙箱自洽验证，请换一个描述再试")


def _finalize(obj: dict, inputs: list[str], expected: list[str],
              emit: Callable) -> dict[str, Any]:
    import uuid

    pid = "cpg-" + uuid.uuid4().hex[:8]
    tags = obj.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    problem = {
        "id": pid,
        "title": obj.get("title", "AI 生成题"),
        "difficulty": obj.get("difficulty", "medium"),
        "tags": tags or ["AI生成"],
        "statement": obj.get("statement", ""),
        "time_limit_ms": int(obj.get("time_limit_ms", 5000)),
        "mem_limit_mb": 512,
        "languages": ["python", "cpp"],
        "created_by": "ai",
    }
    case_list = [
        {"input": s, "output": (exp or "").rstrip("\n"), "sample": i == 0}
        for i, (s, exp) in enumerate(zip(inputs, expected))
    ]
    emit("done", problem_id=pid, title=problem["title"],
         difficulty=problem["difficulty"], tags=problem["tags"],
         n_cases=len(case_list))
    return {"problem": problem, "cases": case_list}
