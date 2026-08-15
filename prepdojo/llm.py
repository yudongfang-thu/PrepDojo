"""LLM 客户端：OpenAI 兼容协议（DeepSeek 官方 API）。

流式优先：stream_chat 逐段产出 reasoning（thinking）与 content 增量，
chat / chat_json / stream_json 在其上构建。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx


class LLMNotConfigured(Exception):
    pass


class LLMError(Exception):
    pass


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


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
                 timeout: int = 120, temperature: float = 0.3):
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

    # ---------- 底层流式 ----------

    def stream_chat(
        self, system: str, user: str, max_tokens: int = 3000,
        tools: Optional[list] = None, _messages_override: Optional[list] = None,
    ):
        """生成器：yield {"type": "reasoning_delta"|"content_delta"|"done", ...}。

        done 事件携带完整 content、reasoning 与聚合后的 tool_calls。
        _messages_override：内部使用（工具循环传完整消息历史）。
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
        try:
            with httpx.stream("POST", f"{self.base_url}/chat/completions",
                              json=payload, headers=headers,
                              timeout=self.timeout) as resp:
                if resp.status_code >= 500:
                    raise LLMError(f"服务端错误 {resp.status_code}")
                if resp.status_code == 401:
                    raise LLMError("API key 无效（401）")
                if resp.status_code != 200:
                    body = resp.read().decode(errors="replace")[:300]
                    raise LLMError(f"API 错误 {resp.status_code}: {body}")
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        reasoning_parts.append(rc)
                        yield {"type": "reasoning_delta", "text": rc}
                    cc = delta.get("content")
                    if cc:
                        content_parts.append(cc)
                        yield {"type": "content_delta", "text": cc}
                    # 工具调用增量聚合（按 index 拼接 arguments）
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tool_calls_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] += fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
        except httpx.HTTPError as e:
            raise LLMError(f"网络错误: {e}") from e
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
        last_err: Exception | None = None
        for _ in range(3):
            try:
                final = None
                for ev in self.stream_chat(system, user, max_tokens):
                    if ev["type"] == "done":
                        final = ev
                if final and final["content"]:
                    return final["content"]
                raise LLMError("空响应")
            except LLMError as e:
                if "401" in str(e) or "key" in str(e):
                    raise
                last_err = e
        raise LLMError(f"LLM 调用失败（已重试）: {last_err}")

    def chat_json(self, system: str, user: str, max_tokens: int = 3000) -> dict[str, Any]:
        """要求 JSON 输出并解析；解析失败带错误反馈重试。"""
        sys2 = system + "\n输出要求：只输出一个合法的 JSON 对象，不要任何额外文字或代码块标记。"
        last_raw = ""
        for attempt in range(3):
            extra = "" if attempt == 0 else (
                f"\n你上次的输出无法解析为 JSON，请严格修正后重新输出完整 JSON：\n{last_raw[:800]}")
            raw = self.chat(sys2, user + extra, max_tokens=max_tokens)
            last_raw = raw
            cand = _find_json_object(raw)
            if cand:
                try:
                    obj = json.loads(cand)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
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
        for attempt in range(3):
            content, reasoning = "", ""
            try:
                for ev in self.stream_chat(sys2, user, max_tokens=max_tokens):
                    if ev["type"] == "done":
                        content, reasoning = ev["content"], ev["reasoning"]
                    elif on_delta:
                        on_delta(ev["type"], ev["text"])
            except LLMError:
                if attempt == 2:
                    raise
                continue
            last_raw = content
            cand = _find_json_object(content)
            if cand:
                try:
                    obj = json.loads(cand)
                    if isinstance(obj, dict):
                        return {"json": obj, "reasoning": reasoning}
                except json.JSONDecodeError:
                    continue
        raise LLMError("模型未能输出可解析的 JSON")
