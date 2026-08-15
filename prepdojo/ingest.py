"""知识接入流水线：用户目录 → 抽取 → 分块 → LLM 结构化题卡 → SQLite。

增量：按文件 sha256 跳过已处理文件。
无 API key 时可用 --dry-run 只看抽取/分块统计。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .db import DB
from .extract import ExtractError, QABlock, chunk_qa, extract_text, iter_source_files
from .llm import LLMClient

CARD_SYSTEM_PROMPT = """你是一位在一线大厂工作多年的资深技术面试官，负责把八股资料加工成面试练习题卡。
要求：
1. question：把原始问题改写得更清晰、更像面试官的提问口吻；如果原材料没有明确问题，则根据内容自拟一个高质量问题。
2. answer_points：3-8 条答案要点，每条一句话，覆盖原材料的关键信息；宁可具体不要空泛。
3. follow_ups：2-3 个深度追问，模拟面试官继续深挖（考察原理、对比、工程实践）。
4. topic_tags：1-4 个技术标签（中文，如：RAG、分布式训练、推理加速、LoRA、CUDA、操作系统）。
5. difficulty：1-3 整数（1=概念背诵，2=需要理解，3=需要深度原理/工程经验）。
只依据给定材料，不要编造材料中没有的内容；材料信息不足时 answer_points 相应减少。"""

CARD_USER_TEMPLATE = """【来源】{source_name}

【原始材料】
{raw}

请输出如下 JSON：
{{"question": "...", "answer_points": ["...", ...], "follow_ups": ["...", ...],
  "topic_tags": ["...", ...], "difficulty": 2}}"""


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def structure_block(llm: LLMClient, block: QABlock) -> dict:
    user = CARD_USER_TEMPLATE.format(
        source_name=block.source_name or "未命名材料",
        raw=block.raw,
    )
    obj = llm.chat_json(CARD_SYSTEM_PROMPT, user, max_tokens=1500)
    question = (obj.get("question") or block.question or "").strip()
    points = [str(x).strip() for x in obj.get("answer_points", []) if str(x).strip()]
    follow_ups = [str(x).strip() for x in obj.get("follow_ups", []) if str(x).strip()]
    tags = [str(x).strip() for x in obj.get("topic_tags", []) if str(x).strip()]
    difficulty = obj.get("difficulty", 2)
    try:
        difficulty = max(1, min(3, int(difficulty)))
    except (TypeError, ValueError):
        difficulty = 2
    if not question or not points:
        raise ValueError("题卡缺少 question 或 answer_points")
    return {
        "question": question,
        "answer_points": points,
        "follow_ups": follow_ups,
        "topic_tags": tags or ["未分类"],
        "difficulty": difficulty,
    }


def ingest_dir(
    root: Path,
    db: DB,
    cfg: Config,
    llm: Optional[LLMClient],
    limit_files: Optional[int] = None,
    limit_blocks: Optional[int] = None,
    dry_run: bool = False,
    sleep_s: float = 0.2,
) -> dict:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"目录不存在: {root}")

    files = iter_source_files(root)
    if limit_files:
        files = files[:limit_files]

    stats = {"files_total": len(files), "files_skipped": 0, "files_failed": 0,
             "files_done": 0, "blocks_found": 0, "cards_added": 0, "cards_failed": 0}

    for fp in files:
        rel = str(fp.relative_to(root))
        try:
            sha = file_sha256(fp)
            if db.source_sha(str(fp)) == sha:
                stats["files_skipped"] += 1
                continue
            text = extract_text(fp)
            blocks = chunk_qa(text, source_name=fp.stem)
        except ExtractError as e:
            print(f"  [跳过] {rel}: {e}")
            stats["files_failed"] += 1
            continue

        stats["blocks_found"] += len(blocks)
        if dry_run:
            stats["files_done"] += 1
            print(f"  [dry-run] {rel}: {len(blocks)} 个 Q&A 块"
                  + (f"，示例: {blocks[0].question[:40]}" if blocks else ""))
            continue

        if llm is None:
            raise RuntimeError("ingest 需要 LLM（未配置 API key 时只能 --dry-run）")

        source_id = db.upsert_source(str(fp), sha, fp.stem)
        n_ok = 0
        for i, block in enumerate(blocks):
            if limit_blocks and n_ok >= limit_blocks:
                break
            try:
                card = structure_block(llm, block)
                db.insert_card(
                    question=card["question"],
                    answer_points=card["answer_points"],
                    follow_ups=card["follow_ups"],
                    topic_tags=card["topic_tags"],
                    difficulty=card["difficulty"],
                    source_id=source_id,
                    source_ref=f"{rel} | {block.locator}",
                )
                n_ok += 1
                stats["cards_added"] += 1
            except Exception as e:
                stats["cards_failed"] += 1
                print(f"  [卡失败] {rel} 块{i}: {e}")
            if sleep_s:
                time.sleep(sleep_s)

        db.update_source_count(source_id)
        stats["files_done"] += 1
        print(f"  [完成] {rel}: +{n_ok} 卡（累计 {stats['cards_added']}）")

    return stats
