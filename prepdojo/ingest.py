"""知识接入流水线：用户目录 → 抽取 → 分块 → LLM 结构化题卡 → SQLite。

增量：按文件 sha256 跳过已处理文件。
无 API key 时可用 --dry-run 只看抽取/分块统计。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .db import DB
from .extract import (MAX_SOURCE_FILE_BYTES, ExtractError, QABlock, chunk_qa,
                      extract_text, iter_source_files)
from .llm import LLMBusy, LLMCancelled, LLMClient, LLMQuotaExceeded

CARD_SYSTEM_PROMPT = """（思考从简，尽快输出最终 JSON。）
你是一位在一线大厂工作多年的资深技术面试官，负责把八股资料加工成面试练习题卡。
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
    if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
        raise ExtractError(f"文件超过 {MAX_SOURCE_FILE_BYTES >> 20}MB 上限")
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            total += len(chunk)
            if total > MAX_SOURCE_FILE_BYTES:
                raise ExtractError(f"文件超过 {MAX_SOURCE_FILE_BYTES >> 20}MB 上限")
            h.update(chunk)
    return h.hexdigest()


def structure_block(llm: LLMClient, block: QABlock,
                    on_delta=None) -> dict:
    user = CARD_USER_TEMPLATE.format(
        source_name=block.source_name or "未命名材料",
        raw=block.raw,
    )
    out = llm.stream_json(CARD_SYSTEM_PROMPT, user, max_tokens=4000,
                          on_delta=on_delta)
    obj = out["json"]
    if not isinstance(obj, dict):
        raise ValueError("题卡 JSON 顶层必须是对象")

    question = obj.get("question")
    if not isinstance(question, str) or not question.strip() or len(question.strip()) > 2000:
        raise ValueError("题卡 question 必须是长度 1-2000 的字符串")
    question = question.strip()

    def string_list(key: str, minimum: int, maximum: int,
                    item_max: int) -> list[str]:
        values = obj.get(key)
        if not isinstance(values, list) or not minimum <= len(values) <= maximum:
            raise ValueError(f"题卡 {key} 必须是包含 {minimum}-{maximum} 项的字符串数组")
        if any(not isinstance(value, str) or not value.strip()
               or len(value.strip()) > item_max for value in values):
            raise ValueError(f"题卡 {key} 每项必须是长度 1-{item_max} 的字符串")
        return [value.strip() for value in values]

    points = string_list("answer_points", 1, 8, 2000)
    follow_ups = string_list("follow_ups", 0, 3, 1000)
    tags = string_list("topic_tags", 1, 4, 100)
    difficulty = obj.get("difficulty")
    if type(difficulty) is not int or not 1 <= difficulty <= 3:
        raise ValueError("题卡 difficulty 必须是 1-3 的整数")
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
    on_event=None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """on_event(kind, data)：file_start / file_skip / file_done / file_failed /
    card_done / delta（AI thinking 与输出增量）。"""
    root = Path(root).expanduser().resolve()
    if not root.exists():
        raise NotADirectoryError(f"路径不存在: {root}")

    def emit(kind: str, **data) -> None:
        if on_event:
            on_event(kind, data)

    # 支持单文件或目录
    files = [root] if root.is_file() else iter_source_files(root)
    if limit_files:
        files = files[:limit_files]

    stats = {"files_total": len(files), "files_skipped": 0, "files_failed": 0,
             "files_done": 0, "blocks_found": 0, "cards_added": 0, "cards_failed": 0}

    for fp in files:
        if cancel_check and cancel_check():
            raise LLMCancelled("知识接入已取消")
        rel = fp.name if root.is_file() else str(fp.relative_to(root))
        try:
            sha = file_sha256(fp)
            if db.source_sha(str(fp)) == sha:
                stats["files_skipped"] += 1
                emit("file_skip", file=rel)
                continue
            text = extract_text(fp)
            blocks = chunk_qa(text, source_name=fp.stem)
        except (ExtractError, OSError) as e:
            stats["files_failed"] += 1
            emit("file_failed", file=rel, error=str(e))
            continue

        stats["blocks_found"] += len(blocks)
        if dry_run:
            stats["files_done"] += 1
            emit("file_done", file=rel, cards=0, dry_run=True, blocks=len(blocks))
            continue

        if not blocks:
            stats["files_failed"] += 1
            emit("file_failed", file=rel, error="未抽取到可结构化的材料块，已保留旧版本")
            continue

        if llm is None:
            raise RuntimeError("ingest 需要 LLM（未配置 API key 时只能 dry-run）")

        emit("file_start", file=rel, blocks=len(blocks))
        selected = blocks[:limit_blocks] if limit_blocks and limit_blocks > 0 else blocks
        staged: list[dict] = []
        failed = 0
        for i, block in enumerate(selected):
            if cancel_check and cancel_check():
                raise LLMCancelled("知识接入已取消")
            try:
                def on_delta(dtype, text, _i=i):
                    emit("delta", delta_kind=dtype, text=text)

                card = structure_block(llm, block, on_delta=on_delta)
                card["source_ref"] = f"{rel} | {block.locator}"
                staged.append(card)
            except (LLMCancelled, LLMQuotaExceeded, LLMBusy):
                raise
            except Exception as e:
                failed += 1
                stats["cards_failed"] += 1
                emit("card_failed", file=rel, block=i, error=str(e)[:200])
            if sleep_s:
                deadline = time.monotonic() + sleep_s
                while time.monotonic() < deadline:
                    if cancel_check and cancel_check():
                        raise LLMCancelled("知识接入已取消")
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        # 只有本批全部结构化成功才原子替换旧题卡；任一块失败时不写 SHA，
        # 下次仍会重试，也不会留下半批新卡或删除上一个可用版本。
        if failed:
            stats["files_failed"] += 1
            emit("file_failed", file=rel,
                 error=f"{failed} 个材料块结构化失败，已保留旧版本")
            continue

        # --limit-blocks 是调试/试跑选项：不提交不完整批次，避免临时预览
        # 删除该来源其余卡片及全体用户学习状态。
        complete = len(selected) == len(blocks)
        if not complete:
            stats["files_done"] += 1
            emit("file_done", file=rel, cards=0, preview_cards=len(staged),
                 partial=True,
                 progress=(stats["files_done"] + stats["files_skipped"]
                           + stats["files_failed"]) / max(stats["files_total"], 1))
            continue
        db.replace_source_cards(str(fp), sha, fp.stem, staged)
        n_ok = len(staged)
        stats["cards_added"] += n_ok
        for card in staged:
            emit("card_done", question=card["question"][:80], tags=card["topic_tags"])
        stats["files_done"] += 1
        emit("file_done", file=rel, cards=n_ok,
             partial=False,
             progress=(stats["files_done"] + stats["files_skipped"] + stats["files_failed"])
             / max(stats["files_total"], 1))

    return stats
