"""LLM 流边界、错误分类与重试预算。"""

from __future__ import annotations

import json

import pytest

from prepdojo.llm import LLMClient, LLMError, LLMHTTPError, _strict_json_loads


class FakeResponse:
    def __init__(self, status_code=200, chunks=(), body=b""):
        self.status_code = status_code
        self._chunks = list(chunks)
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        yield from self._chunks

    def read(self):
        return self._body


def _client(**kwargs):
    return LLMClient("https://llm.example/v1", "sk-test", "model", **kwargs)


def test_json_parser_rejects_nonstandard_numeric_constants():
    with pytest.raises(ValueError, match="不允许"):
        _strict_json_loads('{"score": NaN}')


def test_fragmented_sse_is_parsed_with_balanced_callbacks(monkeypatch):
    payload = json.dumps({
        "choices": [{"delta": {"reasoning_content": "想", "content": "答案"}}]
    }, ensure_ascii=False).encode()
    wire = b"data: " + payload + b"\n\ndata: [DONE]\n"
    chunks = [wire[:9], wire[9:23], wire[23:]]
    monkeypatch.setattr(
        "prepdojo.llm.httpx.stream",
        lambda *_args, **_kwargs: FakeResponse(chunks=chunks))
    events = []
    callbacks = []
    client = _client(
        before_request=lambda: callbacks.append("before"),
        after_request=lambda: callbacks.append("after"))
    events.extend(client.stream_chat("s", "u"))
    final = events[-1]
    assert final["type"] == "done"
    assert final["content"] == "答案" and final["reasoning"] == "想"
    assert callbacks == ["before", "after"]


@pytest.mark.parametrize("status", [400, 403, 404, 429])
def test_deterministic_http_errors_are_not_retried(monkeypatch, status):
    calls = 0

    def stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(status, body=b"bad request")

    monkeypatch.setattr("prepdojo.llm.httpx.stream", stream)
    with pytest.raises(LLMHTTPError) as exc:
        _client().chat("s", "u")
    assert exc.value.status_code == status and calls == 1


def test_transient_5xx_retries_only_once(monkeypatch):
    calls = 0

    def stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(503)

    monkeypatch.setattr("prepdojo.llm.httpx.stream", stream)
    with pytest.raises(LLMError, match="重试 1 次"):
        _client().chat("s", "u")
    assert calls == 2


def test_oversized_stream_stops_without_retry(monkeypatch):
    monkeypatch.setattr("prepdojo.llm.MAX_LLM_STREAM_BYTES", 20)
    calls = 0

    def stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(chunks=[b"x" * 21])

    monkeypatch.setattr("prepdojo.llm.httpx.stream", stream)
    with pytest.raises(LLMError, match="响应过大"):
        _client().chat("s", "u")
    assert calls == 1
