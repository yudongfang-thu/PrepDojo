"""文档抽取与 Q&A 分块。

支持 PDF（文字型，pypdf）/ Markdown / 纯文本。
分块策略：启发式识别 Q&A 边界（编号问题行、以？结尾的短行），
剩余长文本滑窗兜底（question 为空，由 LLM 自拟问题）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXT = {".pdf", ".md", ".markdown", ".txt"}


class ExtractError(Exception):
    pass


@dataclass
class QABlock:
    question: str
    answer: str
    raw: str
    source_name: str = ""
    locator: str = ""  # 文件内位置描述，用于 source_ref

    def approx_chars(self) -> int:
        return len(self.raw)


def iter_source_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT and not p.name.startswith("."):
            out.append(p)
    return out


def extract_text(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".pdf":
        return _extract_pdf(path)
    if suf in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    raise ExtractError(f"不支持的格式: {path.suffix}")


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ExtractError("需要 pypdf：pip install pypdf") from e
    try:
        reader = PdfReader(str(path))
        pages = [(pg.extract_text() or "") for pg in reader.pages]
    except Exception as e:
        raise ExtractError(f"PDF 解析失败: {e}") from e
    text = "\n".join(pages).strip()
    if len(text) < 50:
        raise ExtractError("疑似扫描图 PDF（无可提取文本层），请先 OCR 或跳过")
    return text


# ---------- Q&A 边界识别 ----------

_Q_PATTERNS = [
    # 1. / 1、 / 1) / （1） / 1． 开头，行内含问号
    re.compile(r"^\s*(?:\d{1,3}\s*[.、)．]|[（(]\d{1,3}[)）])\s*(.{2,120}[?？])\s*$"),
    # 一、二、 …… 开头且行内含问号
    re.compile(r"^\s*[一二三四五六七八九十]{1,3}\s*[、.．)）]\s*(.{2,120}[?？])\s*$"),
    # Q1: / Q: / 问： 前缀
    re.compile(r"^\s*(?:Q\s*\d{0,3}\s*[:：.]|问\s*[:：])\s*(.{2,150})\s*$", re.IGNORECASE),
    # 独立短行以问号结尾（≤80 字）
    re.compile(r"^\s*([^\s].{0,78}[?？])\s*$"),
]


def _match_question(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 160:
        return None
    for pat in _Q_PATTERNS:
        m = pat.match(stripped)
        if m:
            q = m.group(1).strip() if m.groups() else stripped
            return q
    return None


def _clean_line(line: str) -> str:
    # PDF 抽取常见问题：中英文之间多余空格
    return re.sub(r"[ \t]{2,}", " ", line.rstrip())


def chunk_qa(text: str, source_name: str = "", max_fallback_chars: int = 1600) -> list[QABlock]:
    lines = [_clean_line(l) for l in text.splitlines()]
    blocks: list[QABlock] = []
    cur_q: str | None = None
    cur_a: list[str] = []

    def flush():
        nonlocal cur_q, cur_a
        if cur_q is not None and any(l.strip() for l in cur_a):
            ans = "\n".join(cur_a).strip()
            blocks.append(
                QABlock(
                    question=cur_q,
                    answer=ans[:4000],
                    raw=(cur_q + "\n" + ans)[:4500],
                    source_name=source_name,
                    locator=f"Q: {cur_q[:60]}",
                )
            )
        cur_q, cur_a = None, []

    for line in lines:
        q = _match_question(line)
        if q and q != cur_q:
            flush()
            cur_q = q
        elif cur_q is not None:
            cur_a.append(line)
        # 问题行之前的内容先丢弃（通常是页眉/来源信息）
    flush()

    # 无问题边界的兜底：滑窗切块
    if not blocks:
        plain = "\n".join(lines).strip()
        for i in range(0, len(plain), max_fallback_chars):
            seg = plain[i : i + max_fallback_chars].strip()
            if len(seg) > 80:
                blocks.append(
                    QABlock(
                        question="",
                        answer=seg,
                        raw=seg,
                        source_name=source_name,
                        locator=f"段落在偏移 {i}",
                    )
                )
    return blocks
