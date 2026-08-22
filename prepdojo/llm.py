"""LLM 客户端：OpenAI 兼容协议（DeepSeek 官方 API）。

流式优先：stream_chat 逐段产出 reasoning（thinking）与 content 增量，
chat / chat_json / stream_json 在其上构建。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

MAX_LLM_STREAM_BYTES = 4 << 20
MAX_LLM_LINE_BYTES = 1 << 20


class LLMNotConfigured(Exception):
    pass


class LLMError(Exception):
    pass


class LLMTransientError(LLMError):
    """网络故障或 5xx；允许一次短退避重试。"""


class LLMHTTPError(LLMError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class LLMQuotaExceeded(LLMError):
    """真实外发请求在原子配额检查时被拒绝。"""

    def __init__(self, message: str, *, scope: str = "user", limit: int = 0):
        super().__init__(message)
        self.scope = scope
        self.limit = limit


class LLMCancelled(LLMError):
    pass


class LLMBusy(LLMError):
    pass


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strict_json_loads(raw: str | bytes | bytearray) -> Any:
    def reject_constant(value: str):
        raise ValueError(f"JSON 中不允许 {value}")

    return json.loads(raw, parse_constant=reject_constant)


def _strip_fence(s: str) -> str:
    m = _JSON_FENCE.search(s)
    if m:
        return m.group(1).strip()
    return s.strip()


def _find_json_object(s: str) -> Optional[str]:
    """从模型输出中提取第一个平衡的 JSON 对象。"""
    s = _strip_fence(s)
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 120, temperature: float = 0.3,
                 *, before_request: Optional[Callable[[], None]] = None,
                 after_request: Optional[Callable[[], None]] = None,
                 cancel_check: Optional[Callable[[], bool]] = None):
        from .config import is_placeholder_key

        if is_placeholder_key(api_key):
            raise LLMNotConfigured(
                "未配置 API key。请在 设置 页面或 data/config.yaml 填写，或设置 PREPDOJO_API_KEY 环境变量。"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.before_request = before_request
        self.after_request = after_request
        self.cancel_check = cancel_check

    # ---------- 底层流式 ----------

    def stream_chat(
        self, system: str, user: str, max_tokens: int = 3000,
        tools: Optional[list] = None, _messages_override: Optional[list] = None,
        total_timeout: float = 0,
    ):
        """生成器：yield {"type": "reasoning_delta"|"content_delta"|"done", ...}。

        done 事件携带完整 content、reasoning 与聚合后的 tool_calls。
        _messages_override：内部使用（工具循环传完整消息历史）。
        total_timeout：总墙钟超时秒数，0=使用客户端 timeout。超时后 yield error。
        """
        messages = _messages_override or [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        request_started = False
        try:
            import time as _time
            t_start = _time.monotonic()
            if self.cancel_check and self.cancel_check():
                raise LLMCancelled("请求已取消")
            if self.before_request:
                # 放在每次真实 HTTP 请求之前，因此重试、工具循环都会按实际成本计数。
                self.before_request()
                request_started = True
            hard_timeout = float(total_timeout) if total_timeout > 0 else float(self.timeout)
            request_timeout = min(float(self.timeout), hard_timeout)

            def parse_sse_line(raw_line: bytes) -> list[dict[str, Any]]:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                if not line.startswith("data:"):
                    return []
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    return []
                try:
                    chunk = _strict_json_loads(data)
                except (json.JSONDecodeError, ValueError):
                    return []
                if not isinstance(chunk, dict):
                    return []
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices \
                        or not isinstance(choices[0], dict):
                    return []
                delta = choices[0].get("delta") or {}
                if not isinstance(delta, dict):
                    return []
                events: list[dict[str, Any]] = []
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
                    events.append({"type": "reasoning_delta", "text": rc})
                cc = delta.get("content")
                if isinstance(cc, str) and cc:
                    content_parts.append(cc)
                    events.append({"type": "content_delta", "text": cc})
                calls = delta.get("tool_calls") or []
                if isinstance(calls, list):
                    for tc in calls[:32]:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index", 0)
                        if not isinstance(idx, int) or not 0 <= idx < 32:
                            continue
                        acc = tool_calls_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""})
                        call_id = tc.get("id")
                        if isinstance(call_id, str) and call_id:
                            acc["id"] = call_id
                        fn = tc.get("function") or {}
                        if not isinstance(fn, dict):
                            continue
                        name = fn.get("name")
                        arguments = fn.get("arguments")
                        if isinstance(name, str):
                            acc["name"] += name
                        if isinstance(arguments, str):
                            acc["arguments"] += arguments
                return events

            with httpx.stream("POST", f"{self.base_url}/chat/completions",
                              json=payload, headers=headers,
                              timeout=request_timeout) as resp:
                if resp.status_code >= 500:
                    raise LLMTransientError(f"服务端错误 {resp.status_code}")
                if resp.status_code == 401:
                    raise LLMError("API key 无效（401）")
                if resp.status_code != 200:
                    body_bytes = bytearray()
                    for error_chunk in resp.iter_bytes():
                        body_bytes.extend(error_chunk[:4096 - len(body_bytes)])
                        if len(body_bytes) >= 4096:
                            break
                    body = bytes(body_bytes).decode(errors="replace")[:300]
                    raise LLMHTTPError(
                        resp.status_code, f"API 错误 {resp.status_code}: {body}")
                buffer = bytearray()
                received = 0
                for raw in resp.iter_bytes():
                    if self.cancel_check and self.cancel_check():
                        raise LLMCancelled("请求已取消")
                    if _time.monotonic() - t_start > hard_timeout:
                        yield {"type": "error", "message":
                               f"AI 响应超时（>{hard_timeout:.0f}秒），请重试或简化输入"}
                        return
                    received += len(raw)
                    if received > MAX_LLM_STREAM_BYTES:
                        yield {"type": "error", "message": "AI 响应过大，已停止接收"}
                        return
                    buffer.extend(raw)
                    while True:
                        pos = buffer.find(b"\n")
                        if pos < 0:
                            break
                        line = bytes(buffer[:pos])
                        del buffer[:pos + 1]
                        for event in parse_sse_line(line):
                            yield event
                    if len(buffer) > MAX_LLM_LINE_BYTES:
                        yield {"type": "error", "message": "AI 流式响应单行过大，已停止接收"}
                        return
                if buffer:
                    for event in parse_sse_line(bytes(buffer)):
                        yield event
        except httpx.HTTPError as e:
            raise LLMTransientError(f"网络错误: {e}") from e
        finally:
            if request_started and self.after_request:
                self.after_request()
        yield {
            "type": "done",
            "content": "".join(content_parts),
            "reasoning": "".join(reasoning_parts),
            "tool_calls": [
                {"id": v["id"], "type": "function",
                 "function": {"name": v["name"], "arguments": v["arguments"]}}
                for k, v in sorted(tool_calls_acc.items()) if v["name"]
            ],
        }

    # ---------- 非流式封装（带重试） ----------

    def chat(self, system: str, user: str, max_tokens: int = 3000) -> str:
        import time

        last_err: Exception | None = None
        for attempt in range(2):
            try:
                final = None
                for ev in self.stream_chat(system, user, max_tokens):
                    if ev["type"] == "done":
                        final = ev
                    elif ev["type"] == "error":
                        raise LLMError(ev.get("message", "AI 响应超时"))
                if final and final["content"]:
                    return final["content"]
                raise LLMTransientError("空响应")
            except LLMTransientError as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                break
            except LLMError:
                # 4xx、配额、取消、响应过大等确定性错误不得即时重试。
                raise
        raise LLMError(f"LLM 调用失败（重试 1 次后仍失败）: {last_err}")

    def chat_json(self, system: str, user: str, max_tokens: int = 3000) -> dict[str, Any]:
        """要求 JSON 输出并解析；解析失败带错误反馈重试。"""
        sys2 = system + "\n输出要求：只输出一个合法的 JSON 对象，不要任何额外文字或代码块标记。"
        last_raw = ""
        for attempt in range(2):
            extra = "" if attempt == 0 else (
                f"\n你上次的输出无法解析为 JSON，请严格修正后重新输出完整 JSON：\n{last_raw[:800]}")
            raw = self.chat(sys2, user + extra, max_tokens=max_tokens)
            last_raw = raw
            cand = _find_json_object(raw)
            if cand:
                try:
                    obj = _strict_json_loads(cand)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, ValueError):
                    continue
        raise LLMError("模型未能输出可解析的 JSON")

    def stream_json(
        self, system: str, user: str, max_tokens: int = 3000,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> dict[str, Any]:
        """流式版 chat_json：实时回调增量（type: reasoning_delta/content_delta），
        返回 {"json": obj, "reasoning": str}。"""
        sys2 = system + "\n输出要求：只输出一个合法的 JSON 对象，不要任何额外文字或代码块标记。"
        last_raw = ""
        for attempt in range(2):
            content, reasoning = "", ""
            extra = "" if attempt == 0 else (
                "\n你上次的输出无法解析为 JSON，请只输出修正后的完整 JSON。"
                f"上次输出节选：\n{last_raw[:800]}")
            try:
                for ev in self.stream_chat(sys2, user + extra, max_tokens=max_tokens):
                    if ev["type"] == "done":
                        content, reasoning = ev["content"], ev["reasoning"]
                    elif ev["type"] == "error":
                        raise LLMError(ev.get("message", "AI 响应超时"))
                    elif on_delta and ev["type"] in ("reasoning_delta", "content_delta"):
                        on_delta(ev["type"], ev["text"])
            except (LLMQuotaExceeded, LLMCancelled, LLMBusy):
                raise
            except LLMTransientError:
                if attempt == 1:
                    raise
                continue
            except LLMError:
                raise
            last_raw = content
            cand = _find_json_object(content)
            if cand:
                try:
                    obj = _strict_json_loads(cand)
                    if isinstance(obj, dict):
                        return {"json": obj, "reasoning": reasoning}
                except (json.JSONDecodeError, ValueError):
                    continue
        raise LLMError("模型未能输出可解析的 JSON")
