"""AI 出题：描述 → 题面 + 参考解 + 测试输入 → 沙箱实跑参考解生成期望输出。

事实兜底：期望输出永远来自参考解的真实运行（与 seeds 生成机制一致）；
参考解跑挂则把错误喂回 AI 修复，最多 fix_rounds 轮；全部用例通过才入库。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .judge import judge_submission
from .llm import LLMClient

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


def _run_reference(code: str, inputs: list[str], time_limit_ms: int,
                   docker_image: str = "") -> list[dict]:
    """逐用例运行参考解（不短路），返回每用例 stdout/stderr/rc/耗时。"""
    import tempfile
    from pathlib import Path

    from .judge import _run_once

    with tempfile.TemporaryDirectory(prefix="prepdojo-gen-") as td:
        entry = Path(td) / "main.py"
        entry.write_text(code, encoding="utf-8")
        cmd = ["python3", str(entry)]
        results = []
        for s in inputs:
            out, err, rc, ms, timed_out = _run_once(
                cmd, s, time_limit_ms / 1000, 512, docker_image=docker_image)
            results.append({"stdout": out, "stderr": err, "rc": rc,
                            "ms": ms, "timed_out": timed_out})
        return results


def generate_problem(
    llm: LLMClient,
    brief: str,
    cpp_compiler: str = "clang++",
    on_event: Optional[Callable[[str, dict], None]] = None,
    fix_rounds: int = 2,
    docker_image: str = "",
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
        obj = out["json"]
        if not (obj.get("reference_python") and obj.get("test_inputs")):
            user_msg += "\n\n（上次输出缺少 reference_python 或 test_inputs，请补全后输出完整 JSON。）"
            continue

        emit("verify_start", attempt=attempt, n_cases=len(obj["test_inputs"]))
        inputs = [s if s.endswith("\n") else s + "\n"
                  for s in obj["test_inputs"]]
        tl = int(obj.get("time_limit_ms", 5000))
        results = _run_reference(obj["reference_python"], inputs, tl, docker_image)
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
