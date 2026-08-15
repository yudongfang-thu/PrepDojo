"""AI 讲题教练：LLM + 判题沙箱工具循环（agentic）。

设计：
- 沙箱暴露为两个 tool（function calling）：run_code / run_problem_case。
- 循环上限保护成本；工具结果以事实回喂（AI 看到的是沙箱真实输出）。
- 讲题上下文：题面 + 用户当前代码 + 最近判题结果，由调用方注入 system。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .llm import LLMClient

MAX_TOOL_ROUNDS = 8

COACH_SYSTEM = """你是一位经验丰富的算法面试教练，正在一对一辅导一位准备秋招的考生。
你有一个本地判题沙箱作为工具：可以运行代码、对题目用例判题。**善用它**：
- 怀疑考生代码有边界 bug 时，构造一个能暴露问题的输入去跑，用事实说话；
- 讲解思路时，可以现场运行一段示例代码演示行为；
- 考生贴代码求 debug 时，先用工具复现，再指出问题行。

你始终能看到考生编辑器里的**最新代码**与**最近一次判题结果**（见上下文）。

准则：
1. 沙箱与测试用例的结果是唯一事实；你的判断与它冲突时以它为准。
2. 不直接给完整答案代码，除非考生明确说"我要看答案"——引导为主。
3. **多解法对比与知识延伸**：讨论解法时，主动对比不同解法的时间/空间复杂度；
   如果存在明显更优解法（如两数之和从暴力 O(n²) 到哈希表 O(n)），要点出它、
   给出思路提示，并顺势讲解其背后的数据结构/算法知识点（哈希表原理、trade-off、
   典型应用场景）——把每道题变成一个知识锚点。
4. 回答用中文，简洁直接；代码块用 ``` 标注语言。
5. 每次最多连续调用 3 次工具就该给出结论，不要无止境试错。"""

AI_JUDGE_SYSTEM = """你是资深算法面试官，对考生刚提交的代码做深度判定。**必须分两步**：

【第一步：先用工具验证（强制）】
- 立即调用 run_problem_case 对全部测试用例运行考生代码，亲眼看到沙箱结果；
- 若结果为 AC 但你怀疑边界（空输入、极端值、单元素等），再调用 run_code 构造边界输入验证；
- 若结果为 WA/TLE/RE，可构造小输入定位问题。

【第二步：基于工具事实输出报告】
只输出一个 JSON 对象（不要任何额外文字）：
{
 "sandbox_verdict": "从工具结果读到的判定（AC/WA/TLE/MLE/RE/CE，必须与沙箱一致）",
 "complexity": {"time": "考生实现的时间复杂度", "space": "空间复杂度"},
 "boundary_analysis": "边界正确性分析；你构造了什么输入验证、观察到什么（如未构造写 未）",
 "better_solution": {
   "exists": true/false,
   "name": "更优解法名称（如 哈希表），无则空",
   "complexity": "其复杂度",
   "why_better": "为什么更优（一句话）",
   "hint": "思路提示（不给完整代码）"
 },
 "related_knowledge": ["更优解法背后的知识点讲解，1-3 条，每条一个知识点：原理/trade-off/典型场景"],
 "interview_tips": ["面试官视角的点评，1-2 条"],
 "summary": "一句话总评"
}
注意：sandbox_verdict 只能来自工具返回的事实；分析结论与工具结果冲突时，以工具为准并修正你的分析。"""

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "在本地沙箱运行一段代码（Python3 或 C++17），返回 stdout、stderr、退出码、耗时。用于构造任意输入验证行为、演示示例。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "完整源代码（stdin/stdout 风格）"},
                    "language": {"type": "string", "enum": ["python", "cpp"]},
                    "stdin": {"type": "string", "description": "标准输入，可为空字符串"},
                },
                "required": ["code", "language"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_problem_case",
            "description": "把代码提交到指定题目的判题沙箱，对全部测试用例运行，返回每用例的判定（AC/WA/TLE/RE）、期望输出与实际输出。用于判题、对比官方答案。",
            "parameters": {
                "type": "object",
                "properties": {
                    "problem_id": {"type": "string", "description": "题目编号，如 cp-003"},
                    "code": {"type": "string"},
                    "language": {"type": "string", "enum": ["python", "cpp"]},
                },
                "required": ["problem_id", "code", "language"],
            },
        },
    },
]


@dataclass
class ToolTrace:
    name: str
    args: dict
    result_summary: str


@dataclass
class ChatResult:
    reply: str
    tool_trace: list[ToolTrace] = field(default_factory=list)
    stopped_reason: str = "done"  # done / max_rounds


class SandboxTools:
    """把判题沙箱包装为 LLM 可调用的工具集（注入题库依赖）。"""

    def __init__(self, get_problem: Callable[[str], Optional[dict]],
                 load_cases: Callable[[str], list[dict]],
                 cpp_compiler: str = "clang++"):
        from .judge import judge_submission  # 延迟导入避免环

        self._judge = judge_submission
        self._get_problem = get_problem
        self._load_cases = load_cases
        self._cpp_compiler = cpp_compiler

    def run_code(self, code: str, language: str, stdin: str = "") -> str:
        res = self._judge(code, language, [{"input": stdin, "output": ""}],
                          cpp_compiler=self._cpp_compiler)
        c = res.cases[0] if res.cases else None
        return json.dumps({
            "exit": "ok" if res.verdict in ("AC", "WA") else res.verdict,
            "stdout": (c.stdout if c else "")[:4000],
            "stderr": (c.stderr if c else "")[:1500],
            "time_ms": c.time_ms if c else 0,
            "note": "RE/TLE/MLE 时 stdout 可能不完整",
        }, ensure_ascii=False)

    def run_problem_case(self, problem_id: str, code: str, language: str) -> str:
        p = self._get_problem(problem_id)
        if not p:
            return json.dumps({"error": f"题目不存在: {problem_id}"}, ensure_ascii=False)
        cases = self._load_cases(problem_id)
        res = self._judge(code, language, cases,
                          time_limit_ms=p["time_limit_ms"],
                          mem_limit_mb=p["mem_limit_mb"],
                          cpp_compiler=self._cpp_compiler)
        payload = {
            "verdict": res.verdict,
            "max_time_ms": res.max_time_ms,
            "cases": [
                {"idx": c.idx, "verdict": c.verdict, "time_ms": c.time_ms,
                 **({"expected": c.expected[:500], "actual": c.stdout[:500]}
                    if c.verdict == "WA" else {}),
                 **({"stderr": c.stderr[:400]} if c.verdict in ("RE", "MLE") else {})}
                for c in res.cases
            ],
        }
        if res.compile_error:
            payload["compile_error"] = res.compile_error[:2000]
        return json.dumps(payload, ensure_ascii=False)

    def dispatch(self, name: str, args: dict) -> tuple[str, str]:
        """返回 (工具结果文本, 人类可读摘要)。"""
        try:
            if name == "run_code":
                out = self.run_code(args["code"], args.get("language", "python"),
                                    args.get("stdin", ""))
                summ = f"运行 {args.get('language', 'python')} 代码（{len(args['code'])} 字符）"
                return out, summ
            if name == "run_problem_case":
                out = self.run_problem_case(args["problem_id"], args["code"],
                                            args.get("language", "python"))
                import json as _j

                verdict = _j.loads(out).get("verdict", "?")
                summ = f"对 {args['problem_id']} 全用例判题：{verdict}"
                return out, summ
            return json.dumps({"error": f"未知工具 {name}"}), f"未知工具 {name}"
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False), f"工具执行出错: {e}"


def chat_step(
    llm: LLMClient,
    tools: SandboxTools,
    history: list[dict],
    on_event: Optional[Callable[[str, dict], None]] = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> ChatResult:
    """跑一轮对话：LLM ↔ 工具循环，直到产出自然语言回复。

    history: [{"role": "system"|"user"|"assistant"|"tool", "content": ...}]
    on_event 事件：
      tool_start / tool_done / reply（完整回复）
      thinking_delta / content_delta（AI 思考与输出增量流）
    """
    messages = [dict(m) for m in history]
    trace: list[ToolTrace] = []

    def emit(kind: str, **data) -> None:
        if on_event:
            on_event(kind, data)

    for round_i in range(max_rounds):
        content, reasoning = "", ""
        calls: list[dict] = []
        try:
            for ev in llm.stream_chat(
                "", "", max_tokens=3000, tools=TOOLS_SPEC,
                _messages_override=messages,
            ):
                if ev["type"] == "done":
                    content, reasoning = ev["content"], ev["reasoning"]
                    calls = ev.get("tool_calls") or []
                elif ev["type"] == "reasoning_delta":
                    emit("thinking_delta", text=ev["text"])
                elif ev["type"] == "content_delta":
                    emit("content_delta", text=ev["text"])
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}") from e

        if not calls:
            reply = content or ""
            emit("reply", text=reply)
            return ChatResult(reply=reply, tool_trace=trace)

        messages.append({"role": "assistant",
                         "content": content or "",
                         "tool_calls": calls})
        for call in calls:
            fn = call["function"]
            try:
                args = json.loads(fn["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            emit("tool_start", name=fn["name"], args=args)
            result, summary = tools.dispatch(fn["name"], args)
            trace.append(ToolTrace(fn["name"], args, summary))
            emit("tool_done", name=fn["name"], summary=summary)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": result})

    messages.append({"role": "user", "content": "（工具调用轮数已达上限，请基于已有信息直接给出结论。）"})
    content, _ = "", ""
    for ev in llm.stream_chat("", "", max_tokens=2500, _messages_override=messages):
        if ev["type"] == "done":
            content = ev["content"]
        elif ev["type"] == "content_delta":
            emit("content_delta", text=ev["text"])
    emit("reply", text=content)
    return ChatResult(reply=content, tool_trace=trace, stopped_reason="max_rounds")


def build_problem_context(problem: dict, code: str, language: str,
                          last_verdict: Optional[str] = None,
                          last_detail: Optional[str] = None) -> str:
    parts = [
        f"【当前题目】{problem['id']} {problem['title']}（{problem['difficulty']}）",
        f"题面：\n{problem['statement']}",
    ]
    if problem.get("samples"):
        s = problem["samples"][0]
        parts.append(f"样例：输入 {s['input']!r} 输出 {s['output']!r}")
    if code and code.strip():
        parts.append(f"【考生当前代码（{language}，编辑器实时快照）】\n```\n{code[:4000]}\n```")
    if last_verdict:
        parts.append(f"【最近一次判题结果】{last_verdict}\n{last_detail or ''}")
    return "\n\n".join(parts)


def ai_judge_report(reply_text: str) -> Optional[dict]:
    """从 AI 判题的最终回复中解析 JSON 报告；失败返回 None。"""
    from .llm import _find_json_object

    import json as _json

    cand = _find_json_object(reply_text)
    if cand:
        try:
            obj = _json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except _json.JSONDecodeError:
            pass
    return None
