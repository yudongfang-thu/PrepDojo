"""八股陪练：抽题 → 用户作答 → LLM 打分（对照知识卡要点）→ 追问。"""

from __future__ import annotations

from typing import Any, Optional

from .db import DB
from .llm import LLMClient

GRADE_SYSTEM = """你是严格但公允的技术面试官，正在考察候选人的一道八股题。
你会得到：题目、参考答案要点（来自候选人自己的知识库）、候选人的作答。
请输出 JSON：
{
 "score": 0-10 的分数（保留一位小数），
 "per_point": [{"point": "参考要点", "covered": true/false, "comment": "候选人是否覆盖、覆盖质量"}],
 "missed": ["候选人遗漏或答错的关键点"],
 "extra_good": ["候选人答出但参考要点之外的加分内容"],
 "overall": "两三句中文总评：先肯定，再指出最大短板",
 "follow_up": "一个自然的深度追问（面试官口吻，考察更深原理或工程实践）"
}
评分标准：10=全面准确且有自己的理解；7-8=覆盖大部分要点；4-6=只覆盖少数要点或有明显错误；
0-3=基本未答或答非所问。宁可偏严不要放水——这是练习，不是客套。"""

FOLLOWUP_SYSTEM = """你是技术面试官，正在就候选人上一题的追问听取回答。
请输出 JSON：
{
 "score": 0-10，
 "overall": "两三句中文点评",
 "reference_answer": "这个追问的参考答案要点（2-4 条合成一段）"
}"""


def grade_answer(llm: LLMClient, card: dict[str, Any], answer: str) -> dict[str, Any]:
    points = "\n".join(f"- {p}" for p in card["answer_points"])
    user = f"""【题目】{card['question']}

【参考答案要点（来自候选人知识库，供你对照）】
{points}

【候选人作答】
{answer[:4000]}

请按 system 要求输出 JSON。"""
    result = llm.chat_json(GRADE_SYSTEM, user, max_tokens=2000)
    try:
        result["score"] = round(float(result.get("score", 0)), 1)
    except (TypeError, ValueError):
        result["score"] = 0.0
    return result


def grade_followup(
    llm: LLMClient, card: dict[str, Any], followup_q: str, answer: str,
    context_answer: Optional[str] = None,
) -> dict[str, Any]:
    points = "\n".join(f"- {p}" for p in card["answer_points"])
    user = f"""【原题】{card['question']}

【参考背景要点】
{points}

【追问】{followup_q}

{f"【候选人上一轮作答（节选）】{context_answer[:1500]}" if context_answer else ""}

【候选人对追问的回答】
{answer[:4000]}

请按 system 要求输出 JSON。"""
    result = llm.chat_json(FOLLOWUP_SYSTEM, user, max_tokens=1200)
    try:
        result["score"] = round(float(result.get("score", 0)), 1)
    except (TypeError, ValueError):
        result["score"] = 0.0
    return result
