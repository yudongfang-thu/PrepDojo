"""LLM 客户端：OpenAI 兼容协议（DeepSeek / 硅基流动 / Ollama 本地模型均可）。

隐私说明：调用时会把所给文本发送到 base_url 指向的服务商；
需要完全不出网时，把 base_url 指向本地 Ollama（http://localhost:11434/v1）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

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
                "未配置 API key。请在 data/config.yaml 填写，或设置 PREPDOJO_API_KEY 环境变量；"
                "使用本地 Ollama 时 key 可填任意非空字符串（如 ollama）。"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def chat(self, system: str, user: str, max_tokens: int = 3000) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    raise LLMError(f"服务端错误 {resp.status_code}")
                if resp.status_code == 401:
                    raise LLMError("API key 无效（401）")
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not content:
                    raise LLMError("空响应")
                return content
            except LLMError:
                raise
            except Exception as e:  # 网络类错误重试
                last_err = e
        raise LLMError(f"LLM 调用失败（已重试）: {last_err}")

    def chat_json(self, system: str, user: str, max_tokens: int = 3000) -> dict[str, Any]:
        """要求 JSON 输出并解析；解析失败带错误反馈重试。"""
        sys2 = system + "\n输出要求：只输出一个合法的 JSON 对象，不要任何额外文字或代码块标记。"
        last_raw = ""
        for attempt in range(3):
            extra = "" if attempt == 0 else f"\n你上次的输出无法解析为 JSON（错误信息见下），请严格修正后重新输出完整 JSON：\n{last_raw[:800]}"
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
