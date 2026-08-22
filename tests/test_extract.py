"""抽取与分块测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo.extract import (  # noqa: E402
    MAX_QA_BLOCKS,
    MAX_SOURCE_FILE_BYTES,
    ExtractError,
    chunk_qa,
    extract_text,
)

SAMPLE = """大模型基础面试
来自：某八股资料

1. 什么是大语言模型？
大语言模型是基于 Transformer 架构的大规模神经网络，通过海量文本预训练获得语言能力。
其核心是自回归生成。

2. prefix Decoder 和 causal Decoder 的区别是什么？
区别在于 attention mask 的设计不同：
第一种 prefix Decoder 只对前缀部分做双向注意力；
第二种 causal Decoder 严格单向。

二、RAG 的三个关键痛点是什么？
检索质量、生成幻觉、评估困难。分别对应不同的解决方案。
"""


def test_chunk_qa_numbered():
    blocks = chunk_qa(SAMPLE, source_name="sample.pdf")
    qs = [b.question for b in blocks]
    assert any("大语言模型" in q for q in qs)
    assert any("prefix Decoder" in q for q in qs)
    assert any("RAG" in q for q in qs)
    for b in blocks:
        assert b.answer, f"答案为空: {b.question}"
        assert "来自" not in b.answer[:10]  # 页眉不进答案开头


def test_chunk_qa_fallback_window():
    text = "这是一段没有任何问号的连续长文本。" * 200  # 无 Q 边界
    blocks = chunk_qa(text)
    assert len(blocks) >= 1
    assert blocks[0].question == ""


def test_extract_md(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# 标题\n\n1. 什么是注意力？\n答案内容。", encoding="utf-8")
    text = extract_text(f)
    assert "注意力" in text


def test_extract_real_pdf_if_exists():
    pdf_dir = Path.home() / "Documents/秋招/Basic AI knowledge"
    pdfs = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.is_dir() else []
    if not pdfs:  # 环境无该目录则跳过
        return
    text = extract_text(pdfs[0])
    assert len(text) > 100
    blocks = chunk_qa(text, source_name=pdfs[0].stem)
    assert blocks, "真实 PDF 应能分出 Q&A 块"


def test_extract_rejects_oversized_file_before_reading(tmp_path):
    path = tmp_path / "huge.txt"
    with path.open("wb") as stream:
        stream.truncate(MAX_SOURCE_FILE_BYTES + 1)
    with pytest.raises(ExtractError, match="文件超过"):
        extract_text(path)


def test_chunk_count_is_bounded():
    text = "\n".join(
        f"Q: 问题{i}？\n答案{i}" for i in range(MAX_QA_BLOCKS + 1))
    with pytest.raises(ExtractError, match="材料块超过"):
        chunk_qa(text)
