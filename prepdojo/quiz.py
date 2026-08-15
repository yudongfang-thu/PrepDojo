"""八股陪练：抽题 → 用户作答 → LLM 打分（对照知识卡要点）→ 追问。"""

from __future__ import annotations

from typing import Any, Optional

from .db import DB
from .llm import LLMClient

INTERVIEWER_PERSONA = {
    "standard": "你的人设：一位在一线大厂工作多年的资深技术面试官，专业平和、就事论事，考察扎实。",
    "strict": "你的人设：一位以高标准著称的一线大厂资深技术面试官，措辞直接，指出问题毫不留情，绝不为安慰而放水。",
    "pressure": "你的人设：一位压力面风格的一线大厂资深面试官，会对含糊表述提出质疑和挑战，模拟真实压力面的追问强度。",
}

GRADE_SYSTEM_TEMPLATE = """{persona}
考察方式：先看候选人对核心概念的掌握是否准确，再看理解深度与工程落地能力；用追问逼近知识边界。

你正在考察候选人的一道八股题。你会得到：题目、参考答案要点（来自候选人自己的知识库）、候选人的作答。
请输出 JSON：
{{
 "score": 0-10 的分数（保留一位小数），
 "per_point": [{{"point": "参考要点", "covered": true/false, "comment": "候选人是否覆盖、覆盖质量"}}],
 "missed": ["候选人遗漏或答错的关键点"],
 "extra_good": ["候选人答出但参考要点之外的加分内容"],
 "overall": "两三句中文总评：先肯定，再指出最大短板",
 "follow_up": "一个自然的深度追问（面试官口吻，考察更深原理或工程实践）"
}}
评分标准：10=全面准确且有自己的理解；7-8=覆盖大部分要点；4-6=只覆盖少数要点或有明显错误；
0-3=基本未答或答非所问。宁可偏严不要放水——这是练习，不是客套。"""


def grade_system(style: str = "standard") -> str:
    return GRADE_SYSTEM_TEMPLATE.format(
        persona=INTERVIEWER_PERSONA.get(style, INTERVIEWER_PERSONA["standard"]))

FOLLOWUP_SYSTEM_TEMPLATE = """{persona}
你正在就候选人上一题的追问听取回答。请输出 JSON：
{{
 "score": 0-10，
 "overall": "两三句中文点评",
 "reference_answer": "这个追问的参考答案要点（2-4 条合成一段）"
}}"""


def followup_system(style: str = "standard") -> str:
    return FOLLOWUP_SYSTEM_TEMPLATE.format(
        persona=INTERVIEWER_PERSONA.get(style, INTERVIEWER_PERSONA["standard"]))


EXPLAIN_SYSTEM = """你是一位擅长把复杂概念讲简单的技术讲师，面向准备秋招的学生讲解八股知识点。
基于给定的问题与参考要点，输出 JSON：
{
 "core": "一句话说清这个知识点的核心（不超过 40 字）",
 "expanded": "展开讲解，300-500 字：是什么、为什么这样设计、常见误区；用中文，条理清晰",
 "analogy": "一个贴切的生活/工程类比，帮助记忆",
 "mnemonic": "一句记忆锚点：把要点串成朗朗上口的一句话或口诀",
 "related": ["1-3 个相关联的知识点名称，便于联想复习"]
}
只依据给定材料展开，不编造；材料信息不足的部分从略。"""


def explain_card(llm: LLMClient, card: dict[str, Any],
                 with_reasoning: bool = False):
    points = "\n".join(f"- {p}" for p in card["answer_points"])
    user = f"""【问题】{card['question']}

【参考要点】
{points}

请按 system 要求输出 JSON 讲解。"""
    out = llm.stream_json(EXPLAIN_SYSTEM, user, max_tokens=2000)
    if with_reasoning:
        return {"json": out["json"], "reasoning": out["reasoning"]}
    return out["json"]


def grade_answer(llm: LLMClient, card: dict[str, Any], answer: str,
                 style: str = "standard", with_reasoning: bool = False):
    points = "\n".join(f"- {p}" for p in card["answer_points"])
    user = f"""【题目】{card['question']}

【参考答案要点（来自候选人知识库，供你对照）】
{points}

【候选人作答】
{answer[:4000]}

请按 system 要求输出 JSON。"""
    out = llm.stream_json(grade_system(style), user, max_tokens=2000)
    result = out["json"]
    if with_reasoning:
        return {"json": result, "reasoning": out["reasoning"]}
    try:
        result["score"] = round(float(result.get("score", 0)), 1)
    except (TypeError, ValueError):
        result["score"] = 0.0
    return result


def grade_followup(
    llm: LLMClient, card: dict[str, Any], followup_q: str, answer: str,
    context_answer: Optional[str] = None, style: str = "standard",
    with_reasoning: bool = False,
):
    points = "\n".join(f"- {p}" for p in card["answer_points"])
    user = f"""【原题】{card['question']}

【参考背景要点】
{points}

【追问】{followup_q}

{f"【候选人上一轮作答（节选）】{context_answer[:1500]}" if context_answer else ""}

【候选人对追问的回答】
{answer[:4000]}

请按 system 要求输出 JSON。"""
    out = llm.stream_json(followup_system(style), user, max_tokens=1200)
    result = out["json"]
    if with_reasoning:
        return {"json": result, "reasoning": out["reasoning"]}
    try:
        result["score"] = round(float(result.get("score", 0)), 1)
    except (TypeError, ValueError):
        result["score"] = 0.0
    return result
