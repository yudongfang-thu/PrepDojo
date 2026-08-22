"""知识接入的整批原子性与增量标记回归测试。"""

from pathlib import Path

import pytest

from prepdojo.config import Config
from prepdojo.db import DB
from prepdojo.extract import QABlock
from prepdojo.llm import LLMCancelled
from prepdojo import ingest


def _card(block: QABlock) -> dict:
    return {
        "question": block.question,
        "answer_points": [block.answer],
        "follow_ups": [],
        "topic_tags": ["测试"],
        "difficulty": 2,
    }


def test_failed_file_keeps_previous_cards_and_sha(tmp_path, monkeypatch):
    source = tmp_path / "knowledge.md"
    source.write_text("new content", encoding="utf-8")
    db = DB(tmp_path / "ingest.db")
    old_card = {
        "question": "旧问题", "answer_points": ["旧答案"], "follow_ups": [],
        "topic_tags": ["旧"], "difficulty": 2, "source_ref": "knowledge.md|old",
    }
    old_id = db.replace_source_cards(
        str(source.resolve()), "old-sha", "knowledge", [old_card])[0]
    blocks = [
        QABlock("问题一", "答案一", "raw1", locator="1"),
        QABlock("问题二", "答案二", "raw2", locator="2"),
    ]
    monkeypatch.setattr(ingest, "file_sha256", lambda _: "new-sha")
    monkeypatch.setattr(ingest, "extract_text", lambda _: "text")
    monkeypatch.setattr(ingest, "chunk_qa", lambda *_args, **_kwargs: blocks)

    calls = 0

    def fail_second(_llm, block, on_delta=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("模型临时失败")
        return _card(block)

    monkeypatch.setattr(ingest, "structure_block", fail_second)
    stats = ingest.ingest_dir(
        source, db, Config(db_path=tmp_path / "ingest.db"), object(), sleep_s=0)
    assert stats["files_failed"] == 1 and stats["cards_added"] == 0
    assert db.source_sha(str(source.resolve())) == "old-sha"
    assert db.get_card(old_id)["question"] == "旧问题"


def test_success_replaces_stale_cards_and_partial_run_is_not_final(tmp_path, monkeypatch):
    source = tmp_path / "knowledge.md"
    source.write_text("new content", encoding="utf-8")
    db = DB(tmp_path / "ingest.db")
    blocks = [
        QABlock("问题一", "答案一", "raw1", locator="1"),
        QABlock("问题二", "答案二", "raw2", locator="2"),
    ]
    monkeypatch.setattr(ingest, "file_sha256", lambda _: "new-sha")
    monkeypatch.setattr(ingest, "extract_text", lambda _: "text")
    monkeypatch.setattr(ingest, "chunk_qa", lambda *_args, **_kwargs: blocks)
    monkeypatch.setattr(
        ingest, "structure_block", lambda _llm, block, on_delta=None: _card(block))
    cfg = Config(db_path=tmp_path / "ingest.db")

    partial = ingest.ingest_dir(
        source, db, cfg, object(), limit_blocks=1, sleep_s=0)
    assert partial["cards_added"] == 0
    assert db.source_sha(str(source.resolve())) is None

    complete = ingest.ingest_dir(source, db, cfg, object(), sleep_s=0)
    assert complete["files_skipped"] == 0 and complete["cards_added"] == 2
    assert db.source_sha(str(source.resolve())) == "new-sha"
    assert db.stats()["cards"] == 2


def test_empty_extraction_never_deletes_previous_version(tmp_path, monkeypatch):
    source = tmp_path / "knowledge.md"
    source.write_text("changed", encoding="utf-8")
    db = DB(tmp_path / "ingest.db")
    old = {"question": "保留我", "answer_points": ["旧答案"], "follow_ups": [],
           "topic_tags": ["旧"], "difficulty": 2, "source_ref": "old"}
    old_id = db.replace_source_cards(
        str(source.resolve()), "old-sha", "knowledge", [old])[0]
    monkeypatch.setattr(ingest, "file_sha256", lambda _: "new-sha")
    monkeypatch.setattr(ingest, "extract_text", lambda _: "")
    monkeypatch.setattr(ingest, "chunk_qa", lambda *_args, **_kwargs: [])

    stats = ingest.ingest_dir(
        source, db, Config(db_path=db.path), object(), sleep_s=0)
    assert stats["files_failed"] == 1 and stats["files_done"] == 0
    assert db.source_sha(str(source.resolve())) == "old-sha"
    assert db.get_card(old_id)["question"] == "保留我"


def test_partial_preview_never_replaces_existing_source(tmp_path, monkeypatch):
    source = tmp_path / "knowledge.md"
    source.write_text("changed", encoding="utf-8")
    db = DB(tmp_path / "ingest.db")
    old_cards = [
        {"question": f"旧问题{i}", "answer_points": ["旧答案"], "follow_ups": [],
         "topic_tags": ["旧"], "difficulty": 2, "source_ref": f"old|{i}"}
        for i in range(3)
    ]
    old_ids = db.replace_source_cards(
        str(source.resolve()), "old-sha", "knowledge", old_cards)
    monkeypatch.setattr(ingest, "file_sha256", lambda _: "new-sha")
    monkeypatch.setattr(ingest, "extract_text", lambda _: "text")
    blocks = [QABlock(f"新问题{i}", "新答案", "raw", locator=str(i)) for i in range(3)]
    monkeypatch.setattr(ingest, "chunk_qa", lambda *_args, **_kwargs: blocks)
    monkeypatch.setattr(
        ingest, "structure_block", lambda _llm, block, on_delta=None: _card(block))

    stats = ingest.ingest_dir(
        source, db, Config(db_path=db.path), object(), limit_blocks=1, sleep_s=0)
    assert stats["cards_added"] == 0
    assert db.source_sha(str(source.resolve())) == "old-sha"
    assert all(db.get_card(card_id) for card_id in old_ids)


@pytest.mark.parametrize("bad", [
    {"question": "q", "answer_points": "abc", "follow_ups": [],
     "topic_tags": ["t"], "difficulty": 2},
    {"question": "q", "answer_points": ["a"], "follow_ups": [],
     "topic_tags": ["t"] * 5, "difficulty": 2},
    {"question": "q", "answer_points": ["a"], "follow_ups": [],
     "topic_tags": ["t"], "difficulty": "2"},
])
def test_structure_block_rejects_malformed_or_amplified_json(bad):
    class FakeLLM:
        def stream_json(self, *_args, **_kwargs):
            return {"json": bad, "reasoning": ""}

    with pytest.raises(ValueError):
        ingest.structure_block(
            FakeLLM(), QABlock("原问题", "原答案", "raw", locator="1"))


def test_cancel_stops_ingest_before_remaining_blocks(tmp_path, monkeypatch):
    source = tmp_path / "knowledge.md"
    source.write_text("content", encoding="utf-8")
    db = DB(tmp_path / "ingest.db")
    blocks = [QABlock(f"问题{i}", "答案", "raw", locator=str(i)) for i in range(5)]
    monkeypatch.setattr(ingest, "extract_text", lambda _: "text")
    monkeypatch.setattr(ingest, "chunk_qa", lambda *_args, **_kwargs: blocks)
    cancelled = False
    calls = 0

    def one_then_cancel(_llm, block, on_delta=None):
        nonlocal cancelled, calls
        calls += 1
        cancelled = True
        return _card(block)

    monkeypatch.setattr(ingest, "structure_block", one_then_cancel)
    with pytest.raises(LLMCancelled):
        ingest.ingest_dir(
            source, db, Config(db_path=db.path), object(), sleep_s=0,
            cancel_check=lambda: cancelled)
    assert calls == 1 and db.source_sha(str(source.resolve())) is None


def test_hashing_rejects_oversized_source_before_reading_it(tmp_path):
    source = tmp_path / "oversized.md"
    with source.open("wb") as stream:
        stream.seek(ingest.MAX_SOURCE_FILE_BYTES)
        stream.write(b"x")
    with pytest.raises(ingest.ExtractError, match="文件超过"):
        ingest.file_sha256(source)
