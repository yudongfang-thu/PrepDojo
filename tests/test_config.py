"""配置 fail-closed、兼容格式与安全写入测试。"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo import cli, config as config_module  # noqa: E402
from prepdojo.config import (  # noqa: E402
    Config,
    ConfigError,
    ensure_dirs,
    load_config,
    update_llm_config,
)


@pytest.fixture(autouse=True)
def clean_config_env(monkeypatch):
    for name in (
        "PREPDOJO_API_KEY", "PREPDOJO_BASE_URL", "PREPDOJO_MODEL",
        "PREPDOJO_TEMPERATURE", "PREPDOJO_TIMEOUT", "PREPDOJO_MULTIUSER",
        "PREPDOJO_REGISTRATION", "PREPDOJO_REGISTRATION_CODE",
        "PREPDOJO_JUDGE_DOCKER_IMAGE", "PREPDOJO_DAILY_LIMIT_PER_USER",
        "PREPDOJO_DAILY_LIMIT_GLOBAL", "PREPDOJO_LLM_CONCURRENCY_PER_USER",
        "PREPDOJO_LLM_CONCURRENCY_GLOBAL", "PREPDOJO_JUDGE_CONCURRENCY_PER_USER",
        "PREPDOJO_JUDGE_CONCURRENCY_GLOBAL", "PREPDOJO_SECURE_COOKIE",
        "PREPDOJO_ALLOWED_HOSTS", "DEEPSEEK_API_KEY", "OPENAI_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_nested_config_is_loaded_and_validated(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """llm:
  api_key: test-key
  base_url: http://127.0.0.1:8000/v1
  model: local-model
  temperature: 0.7
  timeout: 30
judge:
  time_limit_ms: 1200
  mem_limit_mb: 256
  cpp_compiler: g++
  docker_image: prepdojo-judge:latest
server:
  multiuser: true
  registration: code
  registration_code: a-long-invite-code
  daily_limit_per_user: 20
  daily_limit_global: 200
  llm_concurrency_per_user: 2
  llm_concurrency_global: 5
  judge_concurrency_per_user: 3
  judge_concurrency_global: 6
  secure_cookie: true
  allowed_hosts: [prepdojo.example.com, 127.0.0.1]
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.api_key == "test-key"
    assert cfg.judge_time_limit_ms == 1200
    assert cfg.judge_docker_image == "prepdojo-judge:latest"
    assert cfg.multiuser is True
    assert cfg.daily_limit_global == 200
    assert cfg.llm_concurrency_per_user == 2
    assert cfg.llm_concurrency_global == 5
    assert cfg.judge_concurrency_per_user == 3
    assert cfg.judge_concurrency_global == 6
    assert cfg.allowed_hosts == ["prepdojo.example.com", "127.0.0.1"]
    assert cfg.db_path == tmp_path / "prepdojo.db"


def test_legacy_top_level_is_supported_and_section_wins(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """model: old-model
multiuser: true
registration: off
judge_docker_image: prepdojo-judge:old
daily_limit_per_user: 9
llm:
  model: nested-model
judge:
  docker_image: prepdojo-judge:new
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.model == "nested-model"
    assert cfg.multiuser is True
    assert cfg.registration == "off"
    assert cfg.judge_docker_image == "prepdojo-judge:new"
    assert cfg.daily_limit_per_user == 9


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("llm: [", "无法读取配置"),
        ("- not-a-map\n", "顶层必须是映射"),
        ("server:\n  multuser: true\n", "未知字段"),
        ("multiuser: true\nmultiuser: false\n", "重复配置字段"),
        ("multiuser: yes\n", "必须是 true 或 false"),
        ("registration: code\n", "registration_code"),
        ("registration: code\nregistration_code: short\n", "至少需要 8"),
        ("base_url: ftp://example.com\n", "HTTP/HTTPS URL"),
        ("base_url: http://example.com/v1\n", "必须使用 HTTPS"),
        ("base_url: https://example.com:bad/v1\n", "端口不合法"),
        ("base_url: https://example.com/v1?debug=1\n", "查询串"),
        ("judge_docker_image: --help\n", "镜像引用"),
        ("daily_limit_global: -1\n", "daily_limit_global"),
    ],
)
def test_invalid_config_fails_closed(tmp_path, content, message):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_environment_values_are_typed_and_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "generic-key")
    monkeypatch.setenv("PREPDOJO_API_KEY", "specific-key")
    monkeypatch.setenv("PREPDOJO_MULTIUSER", "true")
    monkeypatch.setenv("PREPDOJO_SECURE_COOKIE", "1")
    monkeypatch.setenv("PREPDOJO_DAILY_LIMIT_GLOBAL", "321")
    monkeypatch.setenv("PREPDOJO_ALLOWED_HOSTS", "one.example.com,two.example.com")
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.multiuser is True
    assert cfg.secure_cookie is True
    assert cfg.daily_limit_global == 321
    assert cfg.allowed_hosts == ["one.example.com", "two.example.com"]
    assert cfg.api_key == "specific-key"

    monkeypatch.setenv("PREPDOJO_MULTIUSER", "maybe")
    with pytest.raises(ConfigError, match="必须是 true 或 false"):
        load_config(tmp_path / "missing.yaml")


def test_ensure_dirs_and_atomic_llm_update_preserve_known_blocks(tmp_path):
    data = tmp_path / "private-data"
    path = data / "config.yaml"
    ensure_dirs(data, path)
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path).registration == "off"
    db_path = data / "prepdojo.db"
    db_path.write_bytes(b"")
    os.chmod(db_path, 0o644)
    ensure_dirs(data, path)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    path.write_text(
        """api_key: legacy-key-must-disappear
model: legacy-model-must-disappear
llm:
  api_key: old
judge:
  time_limit_ms: 2500
  mem_limit_mb: 256
  cpp_compiler: g++
  docker_image: prepdojo-judge:latest
server:
  multiuser: true
  registration: off
  daily_limit_per_user: 20
  daily_limit_global: 200
  judge_concurrency_per_user: 3
  judge_concurrency_global: 6
  secure_cookie: true
  allowed_hosts: [prepdojo.example.com]
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o644)
    update_llm_config(
        api_key="",
        base_url="https://example.com/v1",
        model="new-model",
        temperature=0.2,
        timeout=45,
        path=path,
    )
    obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert obj["llm"]["api_key"] == ""
    assert obj["llm"]["model"] == "new-model"
    assert obj["judge"]["docker_image"] == "prepdojo-judge:latest"
    assert obj["judge"]["time_limit_ms"] == 2500
    assert obj["server"]["multiuser"] is True
    assert obj["server"]["daily_limit_global"] == 200
    assert obj["server"]["judge_concurrency_per_user"] == 3
    assert obj["server"]["judge_concurrency_global"] == 6
    assert obj["server"]["allowed_hosts"] == ["prepdojo.example.com"]
    assert "api_key" not in obj
    assert "model" not in obj
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(data.glob(".config.yaml.*"))


@pytest.mark.parametrize(
    "content",
    [
        "llm:\n  api_key: old\n  unknown_option: true\n",
        "server:\n  multiuser: false\ncustom_extension:\n  enabled: true\n",
    ],
)
def test_llm_update_rejects_unknown_extensions_without_changing_file(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    before = path.read_bytes()
    with pytest.raises(ConfigError, match="未知"):
        update_llm_config(
            api_key="new-key",
            base_url="https://example.com/v1",
            model="new-model",
            temperature=0.2,
            timeout=45,
            path=path,
        )
    assert path.read_bytes() == before


def test_default_config_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    path = tmp_path / "runtime-config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    update_llm_config(
        api_key="runtime-key",
        base_url="https://example.com/v1",
        model="runtime-model",
        temperature=0.1,
        timeout=10,
    )
    assert load_config(path).model == "runtime-model"


def test_external_data_dir_is_not_silently_chmodded(tmp_path):
    data = tmp_path / "shared"
    data.mkdir(mode=0o755)
    with pytest.raises(ConfigError, match="权限必须是 0700"):
        ensure_dirs(data)
    assert stat.S_IMODE(data.stat().st_mode) == 0o755


def test_data_and_config_symlinks_are_rejected(tmp_path):
    real_data = tmp_path / "real-data"
    real_data.mkdir(mode=0o700)
    linked_data = tmp_path / "linked-data"
    linked_data.symlink_to(real_data, target_is_directory=True)
    with pytest.raises(ConfigError, match="符号链接数据目录"):
        ensure_dirs(linked_data)

    config_target = real_data / "real-config.yaml"
    config_target.write_text("server:\n  multiuser: false\n", encoding="utf-8")
    os.chmod(config_target, 0o600)
    config_link = real_data / "config.yaml"
    config_link.symlink_to(config_target)
    with pytest.raises(ConfigError, match="符号链接配置文件"):
        ensure_dirs(real_data, config_link)
    with pytest.raises(ConfigError, match="符号链接配置文件"):
        update_llm_config(
            api_key="new-key", base_url="https://example.com/v1",
            model="new-model", temperature=0.2, timeout=45, path=config_link,
        )

    dangling_db = real_data / "prepdojo.db"
    dangling_db.symlink_to(tmp_path / "missing.db")
    with pytest.raises(ConfigError, match="符号链接数据库文件"):
        ensure_dirs(real_data, config_target)


def test_safe_serve_mode_requires_auth_docker_https_and_hosts(monkeypatch):
    with pytest.raises(ConfigError, match="禁止单用户模式"):
        cli._validate_serve_mode(Config(), "0.0.0.0", multiuser=False)
    with pytest.raises(ConfigError, match="必须配置 judge.docker_image"):
        cli._validate_serve_mode(Config(), "127.0.0.1", multiuser=True)
    with pytest.raises(ConfigError, match="secure_cookie"):
        cli._validate_serve_mode(
            Config(judge_docker_image="prepdojo-judge:latest"),
            "127.0.0.1", multiuser=True,
        )

    cfg = Config(
        multiuser=True,
        judge_docker_image="prepdojo-judge:latest",
        secure_cookie=True,
        allowed_hosts=["prepdojo.example.com"],
    )
    called = []
    monkeypatch.setattr(cli, "_docker_preflight", lambda image: called.append(image))
    cli._validate_serve_mode(cfg, "0.0.0.0", multiuser=True)
    assert called == ["prepdojo-judge:latest"]


def test_docker_preflight_checks_daemon_and_image(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/docker")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._docker_preflight("prepdojo-judge:latest")
    assert calls == [
        ["docker", "info"],
        ["docker", "image", "inspect", "prepdojo-judge:latest"],
    ]


def test_noninteractive_password_also_enforces_minimum(monkeypatch):
    monkeypatch.setenv("PREPDOJO_USER_PASSWORD", "short")
    with pytest.raises(ConfigError, match="至少需要 8"):
        cli._read_password()
    monkeypatch.setenv("PREPDOJO_USER_PASSWORD", "long-enough")
    assert cli._read_password() == "long-enough"


def test_cli_reports_config_error_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: (_ for _ in ()).throw(ConfigError("bad yaml")))
    assert cli.main(["stats"]) == 2
    assert "配置错误：bad yaml" in capsys.readouterr().err


def test_backup_script_end_to_end_with_relative_paths(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    config_path = data / "config.yaml"
    config_path.write_text("server:\n  multiuser: false\n", encoding="utf-8")
    os.chmod(config_path, 0o600)
    db_path = data / "prepdojo.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.execute("INSERT INTO marker VALUES ('backup-ok')")
    conn.commit()
    conn.close()
    os.chmod(db_path, 0o600)

    env = os.environ.copy()
    for name in ("PREPDOJO_CONFIG_PATH", "PREPDOJO_DB_PATH"):
        env.pop(name, None)
    env.update({
        "PREPDOJO_PYTHON": sys.executable,
        "PREPDOJO_DATA_DIR": os.path.relpath(data, repo),
        "PREPDOJO_BACKUP_DIR": os.path.relpath(backups, repo),
        "PREPDOJO_BACKUP_RETENTION": "2",
    })
    result = subprocess.run(
        [str(repo / "deploy" / "backup.sh")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    archives = list(backups.glob("prepdojo-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], "r:gz") as bundle:
        assert set(bundle.getnames()) == {"prepdojo.db", "config.yaml", "manifest.json"}
        restored_db = tmp_path / "restored.db"
        db_member = bundle.extractfile("prepdojo.db")
        assert db_member is not None
        restored_db.write_bytes(db_member.read())
    restored = sqlite3.connect(restored_db)
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT value FROM marker").fetchone()[0] == "backup-ok"
    restored.close()


def test_serve_child_does_not_inherit_control_lock():
    repo = Path(__file__).resolve().parent.parent
    script = (repo / "deploy" / "serve.sh").read_text(encoding="utf-8")
    assert "--multiuser 9>&-" in script
    assert 'd.get("judge", {}).get("mode") == "docker"' in script
    assert "allowed_hosts 必须包含内部健康检查主机" in script
    launcher = (repo / "启动PrepDojo.command").read_text(encoding="utf-8")
    assert 'd.get("judge", {}).get("mode") == "local"' in launcher
