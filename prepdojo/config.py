"""配置加载：环境变量 > data/config.yaml > 内置默认值。

API key 永远不进仓库：.gitignore 已排除 data/ 与 .env。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:  # yaml 可选
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SEEDS_DIR = REPO_ROOT / "seeds"
CONFIG_PATH = DATA_DIR / "config.yaml"

DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "timeout": 120,
    "judge_time_limit_ms": 5000,
    "judge_mem_limit_mb": 512,
    "cpp_compiler": "clang++",
}


_PLACEHOLDER_KEYS = {"", "sk-...", "your-api-key-here", "sk-your-real-api-key-here", "请填写"}


def is_placeholder_key(key: str) -> bool:
    return not key or key.strip().lower() in _PLACEHOLDER_KEYS


@dataclass
class Config:
    api_key: str = ""
    base_url: str = DEFAULTS["base_url"]
    model: str = DEFAULTS["model"]
    temperature: float = DEFAULTS["temperature"]
    timeout: int = DEFAULTS["timeout"]
    judge_time_limit_ms: int = DEFAULTS["judge_time_limit_ms"]
    judge_mem_limit_mb: int = DEFAULTS["judge_mem_limit_mb"]
    cpp_compiler: str = DEFAULTS["cpp_compiler"]
    db_path: Path = field(default_factory=lambda: DATA_DIR / "prepdojo.db")

    @property
    def llm_ready(self) -> bool:
        return not is_placeholder_key(self.api_key)


def _load_yaml(path: Path) -> dict:
    if not path.exists() or yaml is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = yaml.safe_load(f) or {}
        return obj.get("llm", obj) if isinstance(obj, dict) else {}
    except Exception:
        return {}


def load_config() -> Config:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in _load_yaml(CONFIG_PATH).items() if k in cfg})
    # 环境变量覆盖
    env_map = {
        "PREPDOJO_API_KEY": "api_key",
        "PREPDOJO_BASE_URL": "base_url",
        "PREPDOJO_MODEL": "model",
        "DEEPSEEK_API_KEY": "api_key",  # 常用惯例
        "OPENAI_API_BASE": "base_url",
    }
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return Config(**cfg)


def example_config_yaml() -> str:
    return """# PrepDojo 配置（本文件位于 data/ 目录，已被 .gitignore 排除）
llm:
  # 三选一：
  # 1) DeepSeek 官方:      api_key + base_url https://api.deepseek.com/v1 + model deepseek-chat
  # 2) 硅基流动:           api_key + base_url https://api.siliconflow.cn/v1 + model deepseek-ai/DeepSeek-V3
  # 3) Ollama 本地模型:    api_key 任意 + base_url http://localhost:11434/v1 + model qwen2.5:7b
  api_key: "sk-..."
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  temperature: 0.3
  timeout: 120
judge:
  time_limit_ms: 5000
  mem_limit_mb: 512
  cpp_compiler: "clang++"
"""


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(example_config_yaml(), encoding="utf-8")
