"""代码题 AI 点评：LLM 只做点评，永不判对错（对错以测试用例为准）。"""

from __future__ import annotations

from typing import Any

from .llm import LLMClient

REVIEW_SYSTEM = """你是资深的算法面试教练。用户刚提交了一道编程题的代码，判题系统已经给出客观判定。
你的职责是点评，不是重新判定对错。请输出 JSON：
{
 "summary": "一句话总体评价（中文）",
 "complexity": {"time": "用户实现的时间复杂度，如 O(n log n)；看不出来写 未知", "space": "同上"},
 "good_points": ["代码中做得好的地方，1-3 条，具体"],
 "issues": ["问题或风险，1-4 条：正确性隐患、边界条件、复杂度可优化处、可读性"],
 "interview_tips": ["如果这是面试现场，面试官可能会追问什么，1-3 条"],
 "improved_hint": "如果用户想改进，给一个方向性提示（不要直接给完整代码），若已足够好则写 无"
}
要求：中文、具体、不空泛；直接指出行级问题；不要输出代码块标记以外的内容。"""


def review_code(
    llm: LLMClient,
    problem: dict[str, Any],
    code: str,
    language: str,
    verdict: str,
    case_summary: str,
) -> dict[str, Any]:
    user = f"""【题目】{problem['title']}
难度：{problem['difficulty']}；标签：{', '.join(problem['tags'])}
题面（节选）：
{problem['statement'][:1500]}

【判题结果】{verdict}
{case_summary}

【用户代码（{language}）】
```{language}
{code[:6000]}
```

请按 system 要求输出 JSON 点评。"""
    return llm.chat_json(REVIEW_SYSTEM, user, max_tokens=1500)
