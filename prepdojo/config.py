"""PrepDojo 配置加载与安全写入。

优先级：环境变量 > ``data/config.yaml`` > 内置默认值。配置文件格式或
已知字段不合法时拒绝继续运行，避免服务在无鉴权/无 Docker 的默认值下误启动。
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - requirements.txt 固定安装 PyYAML
    yaml = None

if yaml is not None:
    class _ConfigLoader(yaml.SafeLoader):
        """按 YAML 1.2 语义解析布尔值，仅接受 true/false。"""

    _ConfigLoader.yaml_implicit_resolvers = {
        key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    for first, resolvers in _ConfigLoader.yaml_implicit_resolvers.items():
        _ConfigLoader.yaml_implicit_resolvers[first] = [
            item for item in resolvers if item[0] != "tag:yaml.org,2002:bool"
        ]
    _ConfigLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
    )

    def _construct_unique_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "配置键不可哈希", key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"重复配置字段: {key}", key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _ConfigLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


DATA_DIR = _env_path("PREPDOJO_DATA_DIR", REPO_ROOT / "data")
SEEDS_DIR = REPO_ROOT / "seeds"
CONFIG_PATH = _env_path("PREPDOJO_CONFIG_PATH", DATA_DIR / "config.yaml")

DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "timeout": 120,
    "judge_time_limit_ms": 5000,
    "judge_mem_limit_mb": 512,
    "cpp_compiler": "clang++",
    "multiuser": False,
    "registration": "off",
    "registration_code": "",
    "judge_docker_image": "",
    "daily_limit_per_user": 100,
    "daily_limit_global": 1000,
    "llm_concurrency_per_user": 2,
    "llm_concurrency_global": 4,
    "judge_concurrency_per_user": 2,
    "judge_concurrency_global": 4,
    "secure_cookie": False,
    "allowed_hosts": ["127.0.0.1", "localhost", "::1", "testserver"],
}

_PLACEHOLDER_KEYS = {"", "sk-...", "your-api-key-here", "sk-your-real-api-key-here", "请填写"}
_WRITE_LOCK = threading.Lock()
_HOST_RE = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class ConfigError(RuntimeError):
    """配置缺失依赖、无法解析或字段不合法。"""


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
    daily_limit_global: int = DEFAULTS["daily_limit_global"]
    llm_concurrency_per_user: int = DEFAULTS["llm_concurrency_per_user"]
    llm_concurrency_global: int = DEFAULTS["llm_concurrency_global"]
    judge_concurrency_per_user: int = DEFAULTS["judge_concurrency_per_user"]
    judge_concurrency_global: int = DEFAULTS["judge_concurrency_global"]
    secure_cookie: bool = DEFAULTS["secure_cookie"]
    allowed_hosts: list[str] = field(default_factory=lambda: list(DEFAULTS["allowed_hosts"]))
    db_path: Path = field(default_factory=lambda: DATA_DIR / "prepdojo.db")

    @property
    def llm_ready(self) -> bool:
        return not is_placeholder_key(self.api_key)


_TOP_LEVEL_KEYS = set(DEFAULTS)
_SECTIONS: dict[str, dict[str, str]] = {
    "llm": {
        "api_key": "api_key",
        "base_url": "base_url",
        "model": "model",
        "temperature": "temperature",
        "timeout": "timeout",
        # 早期部署文档曾把该字段误放在 llm 段，继续兼容。
        "daily_limit_per_user": "daily_limit_per_user",
    },
    "judge": {
        "time_limit_ms": "judge_time_limit_ms",
        "judge_time_limit_ms": "judge_time_limit_ms",
        "mem_limit_mb": "judge_mem_limit_mb",
        "judge_mem_limit_mb": "judge_mem_limit_mb",
        "cpp_compiler": "cpp_compiler",
        "docker_image": "judge_docker_image",
        "judge_docker_image": "judge_docker_image",
    },
    "server": {
        "multiuser": "multiuser",
        "registration": "registration",
        "registration_code": "registration_code",
        "daily_limit_per_user": "daily_limit_per_user",
        "daily_limit_global": "daily_limit_global",
        "llm_concurrency_per_user": "llm_concurrency_per_user",
        "llm_concurrency_global": "llm_concurrency_global",
        "judge_concurrency_per_user": "judge_concurrency_per_user",
        "judge_concurrency_global": "judge_concurrency_global",
        "secure_cookie": "secure_cookie",
        "allowed_hosts": "allowed_hosts",
    },
}


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise ConfigError("缺少 PyYAML，无法读取配置；请重新安装 requirements.txt")
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = yaml.load(f, Loader=_ConfigLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ConfigError(f"配置 {path} 的顶层必须是映射（key: value）")
    return obj


def _flatten_config(obj: dict[str, Any], path: Path) -> dict[str, Any]:
    """合并旧顶层格式和三段式格式；段内值优先。"""
    merged: dict[str, Any] = {}
    unknown = sorted(str(k) for k in obj if k not in _TOP_LEVEL_KEYS and k not in _SECTIONS)
    if unknown:
        raise ConfigError(f"配置 {path} 含未知顶层字段: {', '.join(unknown)}")

    for key in _TOP_LEVEL_KEYS:
        if key in obj:
            merged[key] = obj[key]
    for section, aliases in _SECTIONS.items():
        if section not in obj:
            continue
        values = obj[section]
        if not isinstance(values, dict):
            raise ConfigError(f"配置 {path} 的 {section} 必须是映射")
        bad = sorted(str(k) for k in values if k not in aliases)
        if bad:
            raise ConfigError(f"配置 {path} 的 {section} 含未知字段: {', '.join(bad)}")
        for source, target in aliases.items():
            if source in values:
                merged[target] = values[source]
    return merged


def _expect_str(values: dict[str, Any], key: str, *, allow_empty: bool = True) -> str:
    value = values[key]
    if not isinstance(value, str):
        raise ConfigError(f"配置字段 {key} 必须是字符串")
    value = value.strip()
    if not allow_empty and not value:
        raise ConfigError(f"配置字段 {key} 不能为空")
    return value


def _expect_bool(values: dict[str, Any], key: str) -> bool:
    value = values[key]
    if not isinstance(value, bool):
        raise ConfigError(f"配置字段 {key} 必须是 true 或 false")
    return value


def _expect_int(values: dict[str, Any], key: str, low: int, high: int) -> int:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"配置字段 {key} 必须是 {low}..{high} 的整数")
    return value


def _validate_host(host: str) -> str:
    host = host.strip()
    if not host or "/" in host or "://" in host or any(c.isspace() for c in host):
        raise ConfigError(f"allowed_hosts 中的主机名不合法: {host!r}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host != "*" and not _HOST_RE.fullmatch(host):
            raise ConfigError(f"allowed_hosts 中的主机名不合法: {host!r}")
    return host


def _validate(values: dict[str, Any]) -> dict[str, Any]:
    clean = dict(values)
    for key in ("api_key", "registration_code", "judge_docker_image"):
        clean[key] = _expect_str(clean, key)
    for key in ("base_url", "model", "cpp_compiler"):
        clean[key] = _expect_str(clean, key, allow_empty=False)

    parsed = urlparse(clean["base_url"])
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or any(c.isspace() for c in clean["base_url"])):
        raise ConfigError("配置字段 base_url 必须是完整的 HTTP/HTTPS URL")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError("配置字段 base_url 的端口不合法") from exc
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ConfigError("配置字段 base_url 的端口不合法")
    if parsed.username or parsed.password:
        raise ConfigError("配置字段 base_url 不得包含用户名或密码")
    if parsed.params or parsed.query or parsed.fragment:
        raise ConfigError("配置字段 base_url 不得包含参数、查询串或片段")
    if parsed.scheme == "http":
        hostname = (parsed.hostname or "").lower()
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if not loopback:
            raise ConfigError("非本机 LLM base_url 必须使用 HTTPS")

    docker_image = clean["judge_docker_image"]
    if docker_image and (docker_image.startswith("-")
                         or any(char.isspace() for char in docker_image)):
        raise ConfigError("配置字段 judge_docker_image 不是合法的镜像引用")

    temp = clean["temperature"]
    if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not 0 <= float(temp) <= 2:
        raise ConfigError("配置字段 temperature 必须是 0..2 的数字")
    clean["temperature"] = float(temp)
    clean["timeout"] = _expect_int(clean, "timeout", 1, 3600)
    clean["judge_time_limit_ms"] = _expect_int(clean, "judge_time_limit_ms", 100, 60_000)
    clean["judge_mem_limit_mb"] = _expect_int(clean, "judge_mem_limit_mb", 16, 4096)
    clean["daily_limit_per_user"] = _expect_int(clean, "daily_limit_per_user", 0, 1_000_000)
    clean["daily_limit_global"] = _expect_int(clean, "daily_limit_global", 0, 100_000_000)
    clean["llm_concurrency_per_user"] = _expect_int(
        clean, "llm_concurrency_per_user", 1, 128)
    clean["llm_concurrency_global"] = _expect_int(
        clean, "llm_concurrency_global", 1, 1024)
    if clean["llm_concurrency_per_user"] > clean["llm_concurrency_global"]:
        raise ConfigError(
            "llm_concurrency_per_user 不能大于 llm_concurrency_global")
    clean["judge_concurrency_per_user"] = _expect_int(
        clean, "judge_concurrency_per_user", 1, 128)
    clean["judge_concurrency_global"] = _expect_int(
        clean, "judge_concurrency_global", 1, 1024)
    if clean["judge_concurrency_per_user"] > clean["judge_concurrency_global"]:
        raise ConfigError(
            "judge_concurrency_per_user 不能大于 judge_concurrency_global")
    clean["multiuser"] = _expect_bool(clean, "multiuser")
    clean["secure_cookie"] = _expect_bool(clean, "secure_cookie")

    registration = _expect_str(clean, "registration", allow_empty=False).lower()
    if registration not in {"off", "code", "open"}:
        raise ConfigError("配置字段 registration 只能是 off、code 或 open")
    if registration == "code" and not clean["registration_code"]:
        raise ConfigError("registration=code 时必须设置 registration_code")
    if registration == "code" and len(clean["registration_code"]) < 8:
        raise ConfigError("registration_code 至少需要 8 个字符")
    clean["registration"] = registration

    hosts = clean["allowed_hosts"]
    if isinstance(hosts, str):
        hosts = [item for item in hosts.split(",") if item.strip()]
    if not isinstance(hosts, list) or not hosts or not all(isinstance(item, str) for item in hosts):
        raise ConfigError("配置字段 allowed_hosts 必须是非空字符串列表")
    clean["allowed_hosts"] = list(dict.fromkeys(_validate_host(item) for item in hosts))
    return clean


def _environment_overrides() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    env_map = {
        # 通用兼容变量优先级较低，PREPDOJO_* 可明确覆盖。
        "DEEPSEEK_API_KEY": "api_key",
        "OPENAI_API_BASE": "base_url",
        "PREPDOJO_API_KEY": "api_key",
        "PREPDOJO_BASE_URL": "base_url",
        "PREPDOJO_MODEL": "model",
        "PREPDOJO_TEMPERATURE": "temperature",
        "PREPDOJO_TIMEOUT": "timeout",
        "PREPDOJO_MULTIUSER": "multiuser",
        "PREPDOJO_REGISTRATION": "registration",
        "PREPDOJO_REGISTRATION_CODE": "registration_code",
        "PREPDOJO_JUDGE_DOCKER_IMAGE": "judge_docker_image",
        "PREPDOJO_DAILY_LIMIT_PER_USER": "daily_limit_per_user",
        "PREPDOJO_DAILY_LIMIT_GLOBAL": "daily_limit_global",
        "PREPDOJO_LLM_CONCURRENCY_PER_USER": "llm_concurrency_per_user",
        "PREPDOJO_LLM_CONCURRENCY_GLOBAL": "llm_concurrency_global",
        "PREPDOJO_JUDGE_CONCURRENCY_PER_USER": "judge_concurrency_per_user",
        "PREPDOJO_JUDGE_CONCURRENCY_GLOBAL": "judge_concurrency_global",
        "PREPDOJO_SECURE_COOKIE": "secure_cookie",
        "PREPDOJO_ALLOWED_HOSTS": "allowed_hosts",
    }
    for env, key in env_map.items():
        value = os.environ.get(env)
        if value != "" and value is not None:
            raw[key] = value

    if "temperature" in raw:
        try:
            raw["temperature"] = float(raw["temperature"])
        except ValueError as exc:
            raise ConfigError("环境变量中的 temperature 必须是数字") from exc
    for key in ("timeout", "daily_limit_per_user", "daily_limit_global",
                "llm_concurrency_per_user", "llm_concurrency_global",
                "judge_concurrency_per_user", "judge_concurrency_global"):
        if key in raw:
            try:
                raw[key] = int(raw[key])
            except ValueError as exc:
                raise ConfigError(f"环境变量中的 {key} 必须是整数") from exc
    for key in ("multiuser", "secure_cookie"):
        if key in raw:
            value = str(raw[key]).strip().lower()
            if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                raise ConfigError(f"环境变量中的 {key} 必须是 true 或 false")
            raw[key] = value in {"1", "true", "yes", "on"}
    return raw


def load_config(path: Path | None = None) -> Config:
    """读取并验证配置。``path=None`` 时在调用时解析当前 ``CONFIG_PATH``。"""
    config_path = Path(path) if path is not None else CONFIG_PATH
    cfg = dict(DEFAULTS)
    cfg["allowed_hosts"] = list(DEFAULTS["allowed_hosts"])
    cfg.update(_flatten_config(_read_yaml_mapping(config_path), config_path))
    cfg.update(_environment_overrides())
    clean = _validate(cfg)
    db_directory = config_path.parent if path is not None else DATA_DIR
    clean["db_path"] = db_directory / "prepdojo.db"
    return Config(**clean)


def example_config_yaml() -> str:
    return """# PrepDojo 配置（data/ 永不入库；配置文件权限应为 0600）
llm:
  # DeepSeek 或其他 OpenAI-compatible 服务；未配置时本地判题仍可用
  api_key: "sk-..."
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  temperature: 0.3
  timeout: 120
judge:
  time_limit_ms: 5000
  mem_limit_mb: 512
  cpp_compiler: "clang++"
  docker_image: ""             # 多用户模式必须填写，如 prepdojo-judge:latest
server:
  multiuser: false
  registration: "off"          # off / code / open；默认关闭自助注册
  registration_code: ""
  daily_limit_per_user: 100     # 0 表示不限制
  daily_limit_global: 1000      # 全站每日上限；0 表示不限制
  llm_concurrency_per_user: 2   # 单用户同时外发的 LLM 请求数
  llm_concurrency_global: 4     # 全站同时外发的 LLM 请求数
  judge_concurrency_per_user: 2 # 单用户同时运行的判题数
  judge_concurrency_global: 4   # 全站同时运行的判题数
  secure_cookie: false          # HTTPS 部署必须为 true
  allowed_hosts:
    - "127.0.0.1"
    - "localhost"
    - "::1"
"""


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, mode)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def ensure_dirs(data_dir: Path | None = None, config_path: Path | None = None) -> None:
    """创建私有数据目录和配置模板，并修正现有路径权限。"""
    directory = Path(data_dir) if data_dir is not None else DATA_DIR
    path = Path(config_path) if config_path is not None else (
        CONFIG_PATH if data_dir is None else directory / "config.yaml"
    )
    try:
        if directory.is_symlink():
            raise ConfigError(f"拒绝使用符号链接数据目录: {directory}")
        directory_existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        default_directory = (REPO_ROOT / "data").resolve()
        if not directory_existed or directory.resolve() == default_directory:
            os.chmod(directory, 0o700)
        elif stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise ConfigError(
                f"外部数据目录权限必须是 0700，请先执行 chmod 700 {directory}")
        if path.is_symlink():
            raise ConfigError(f"拒绝使用符号链接配置文件: {path}")
        if not path.exists():
            with _WRITE_LOCK:
                if not path.exists():
                    _atomic_write(path, example_config_yaml())
        else:
            if not path.is_file():
                raise ConfigError(f"配置路径不是普通文件: {path}")
            if path.parent.resolve() == directory.resolve():
                os.chmod(path, 0o600)
            elif stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ConfigError(
                    f"外部配置文件权限必须是 0600，请先执行 chmod 600 {path}")
        for name in ("prepdojo.db", "prepdojo.db-wal", "prepdojo.db-shm", "prepdojo.db-journal"):
            db_file = directory / name
            if db_file.is_symlink():
                raise ConfigError(f"拒绝使用符号链接数据库文件: {db_file}")
            if db_file.exists():
                if not db_file.is_file():
                    raise ConfigError(f"数据库路径不是普通文件: {db_file}")
                os.chmod(db_file, 0o600)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(f"无法创建或保护数据目录/配置文件: {exc}") from exc


def update_llm_config(
    *,
    api_key: Optional[str],
    base_url: str,
    model: str,
    temperature: float,
    timeout: int,
    path: Path | None = None,
) -> None:
    """原子更新 LLM 段并保留其他合法配置段。

    ``api_key=""`` 表示明确清空密钥，``None`` 表示保留磁盘中的值；路径参数为 ``None`` 时在调用时读取
    ``CONFIG_PATH``，便于测试或嵌入方替换配置位置。
    """
    config_path = Path(path) if path is not None else CONFIG_PATH
    if config_path.is_symlink():
        raise ConfigError(f"拒绝更新符号链接配置文件: {config_path}")
    with _WRITE_LOCK:
        obj = _read_yaml_mapping(config_path)
        llm_section = obj.get("llm", {})
        if llm_section is None:
            llm_section = {}
        if not isinstance(llm_section, dict):
            raise ConfigError(f"配置 {config_path} 的 llm 必须是映射")
        if api_key is None:
            disk_key = llm_section.get("api_key", obj.get("api_key", ""))
            if not isinstance(disk_key, str):
                raise ConfigError("配置字段 api_key 必须是字符串")
            api_key = disk_key
        candidate = dict(DEFAULTS)
        candidate["allowed_hosts"] = list(DEFAULTS["allowed_hosts"])
        candidate.update({
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "temperature": temperature,
            "timeout": timeout,
        })
        clean = _validate(candidate)
        new_llm = dict(llm_section)
        new_llm.update({
            "api_key": clean["api_key"],
            "base_url": clean["base_url"],
            "model": clean["model"],
            "temperature": clean["temperature"],
            "timeout": clean["timeout"],
        })
        obj["llm"] = new_llm
        # 旧版允许 LLM 字段直接放在顶层；迁移到 llm 段后删除重复值，
        # 尤其避免“清空密钥”后旧 api_key 仍以明文残留在磁盘。
        for legacy_key in ("api_key", "base_url", "model", "temperature", "timeout"):
            obj.pop(legacy_key, None)
        # 与启动路径使用同一套严格校验：运行中被手工加入的未知字段不能
        # 被“成功保存”到下次重启才暴露为 fail-closed 故障。
        full_candidate = dict(DEFAULTS)
        full_candidate["allowed_hosts"] = list(DEFAULTS["allowed_hosts"])
        full_candidate.update(_flatten_config(obj, config_path))
        full_candidate.update(_environment_overrides())
        _validate(full_candidate)
        if yaml is None:  # pragma: no cover
            raise ConfigError("缺少 PyYAML，无法保存配置")
        text = yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)
        try:
            _atomic_write(config_path, text)
        except OSError as exc:
            raise ConfigError(f"无法安全保存配置 {config_path}: {exc}") from exc
