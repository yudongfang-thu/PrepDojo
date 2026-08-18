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
    # ===== server-beta：多用户部署 =====
    "multiuser": False,            # True 时启用登录鉴权
    "registration": "code",        # 注册模式：off=仅管理员建号 / code=邀请码自助注册 / open=完全开放
    "registration_code": "",       # registration=code 时的邀请码
    "judge_docker_image": "",      # 非空时判题在 Docker 沙箱执行（如 prepdojo-judge:latest）
    "daily_limit_per_user": 0,     # 每用户每日 AI 调用上限，0 = 不限
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
    multiuser: bool = DEFAULTS["multiuser"]
    registration: str = DEFAULTS["registration"]
    registration_code: str = DEFAULTS["registration_code"]
    judge_docker_image: str = DEFAULTS["judge_docker_image"]
    daily_limit_per_user: int = DEFAULTS["daily_limit_per_user"]
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
        if not isinstance(obj, dict):
            return {}
        # 顶层键（multiuser / judge_docker_image / ...）与 llm: 段合并，llm 优先
        llm_sec = obj.pop("llm", None)
        merged = dict(obj)
        if isinstance(llm_sec, dict):
            merged.update(llm_sec)
        return merged
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
  # DeepSeek 官方 API（https://platform.deepseek.com 申请）
  # 也支持 vLLM / SGLang 等 OpenAI-compatible 本地服务；api_key 可填任意非空值，例如 dummy-key
  api_key: "sk-..."
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  temperature: 0.3
  timeout: 120
judge:
  time_limit_ms: 5000
  mem_limit_mb: 512
  cpp_compiler: "clang++"
# 多用户模式（实验室服务器）：
# multiuser: true            # 启用登录
# registration: code         # off=仅管理员建号 / code=邀请码自助注册 / open=完全开放
# registration_code: "LAB-XXX"  # 自助注册邀请码（registration=code 时）
"""


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(example_config_yaml(), encoding="utf-8")
