#!/usr/bin/env bash
# 在线一致性备份 SQLite 与配置；产物为原子发布的私有 tar.gz。
set -Eeuo pipefail
umask 077

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DATA_DIR_INPUT=${PREPDOJO_DATA_DIR:-"$ROOT_DIR/data"}
DB_PATH_INPUT=${PREPDOJO_DB_PATH:-}
CONFIG_PATH_INPUT=${PREPDOJO_CONFIG_PATH:-}
BACKUP_DIR_INPUT=${PREPDOJO_BACKUP_DIR:-}
RETENTION=${PREPDOJO_BACKUP_RETENTION:-14}
PYTHON=${PREPDOJO_PYTHON:-"$ROOT_DIR/.venv/bin/python"}

if [[ ! -x "$PYTHON" ]]; then
  echo "错误：找不到 $PYTHON" >&2
  exit 1
fi
resolve_path() {
  "$PYTHON" -c \
    'import sys; from pathlib import Path; p=Path(sys.argv[2]).expanduser(); print(p if p.is_absolute() else (Path(sys.argv[1])/p).resolve())' \
    "$ROOT_DIR" "$1"
}
DATA_DIR=$(resolve_path "$DATA_DIR_INPUT")
DB_PATH=$(resolve_path "${DB_PATH_INPUT:-$DATA_DIR/prepdojo.db}")
CONFIG_PATH=$(resolve_path "${CONFIG_PATH_INPUT:-$DATA_DIR/config.yaml}")
BACKUP_DIR=$(resolve_path "${BACKUP_DIR_INPUT:-$DATA_DIR/backups}")
path_mode() {
  "$PYTHON" -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$1"
}
if [[ ! -f "$DB_PATH" || ! -s "$DB_PATH" ]]; then
  echo "错误：源数据库不存在或为空：$DB_PATH" >&2
  exit 1
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "错误：配置文件不存在：$CONFIG_PATH" >&2
  exit 1
fi
if [[ -L "$DB_PATH" || -L "$CONFIG_PATH" ]]; then
  echo "错误：数据库和配置文件不得是符号链接" >&2
  exit 1
fi
if [[ $(path_mode "$DB_PATH") != 600 || $(path_mode "$CONFIG_PATH") != 600 ]]; then
  echo "错误：数据库和配置文件权限必须是 0600" >&2
  exit 1
fi
if [[ ! "$RETENTION" =~ ^[1-9][0-9]*$ ]]; then
  echo "错误：PREPDOJO_BACKUP_RETENTION 必须是正整数" >&2
  exit 1
fi

if [[ -e "$BACKUP_DIR" && ! -d "$BACKUP_DIR" ]]; then
  echo "错误：备份路径不是目录：$BACKUP_DIR" >&2
  exit 1
fi
if [[ ! -e "$BACKUP_DIR" ]]; then
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
elif [[ $(path_mode "$BACKUP_DIR") != 700 ]]; then
  echo "错误：已有备份目录权限必须是 0700：$BACKUP_DIR" >&2
  exit 1
fi
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL="$BACKUP_DIR/prepdojo-$STAMP.tar.gz"
if [[ -e "$FINAL" ]]; then
  echo "错误：同名备份已存在：$FINAL" >&2
  exit 1
fi
STAGING=$(mktemp -d "$BACKUP_DIR/.backup-$STAMP.XXXXXX")
TMP_ARCHIVE=$(mktemp "$BACKUP_DIR/.prepdojo-$STAMP.tar.gz.XXXXXX")
cleanup() {
  rm -rf "$STAGING"
  rm -f "$TMP_ARCHIVE"
}
trap cleanup EXIT

"$PYTHON" - "$DB_PATH" "$STAGING/prepdojo.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

source_path, target_path = sys.argv[1:]
source_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
source = sqlite3.connect(source_uri, uri=True)
target = sqlite3.connect(target_path)
try:
    source_check = source.execute("PRAGMA quick_check").fetchone()[0]
    if source_check != "ok":
        raise SystemExit(f"源数据库 quick_check 失败: {source_check}")
    source.backup(target)
    target.commit()
    target_check = target.execute("PRAGMA integrity_check").fetchone()[0]
    if target_check != "ok":
        raise SystemExit(f"备份数据库 integrity_check 失败: {target_check}")
finally:
    target.close()
    source.close()
PY
install -m 600 "$CONFIG_PATH" "$STAGING/config.yaml"

"$PYTHON" - "$STAGING" "$DB_PATH" "$CONFIG_PATH" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

directory = Path(sys.argv[1])

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

manifest = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source_db": str(Path(sys.argv[2]).resolve()),
    "source_config": str(Path(sys.argv[3]).resolve()),
    "files": {},
}
for name in ("prepdojo.db", "config.yaml"):
    path = directory / name
    manifest["files"][name] = {
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }
(directory / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
chmod 600 "$STAGING/manifest.json" "$STAGING/prepdojo.db"

COPYFILE_DISABLE=1 tar -C "$STAGING" -czf "$TMP_ARCHIVE" prepdojo.db config.yaml manifest.json
tar -tzf "$TMP_ARCHIVE" >/dev/null
chmod 600 "$TMP_ARCHIVE"
if ! ln "$TMP_ARCHIVE" "$FINAL"; then
  echo "错误：无法原子发布备份（同名文件可能已存在）：$FINAL" >&2
  exit 1
fi

"$PYTHON" - "$BACKUP_DIR" "$RETENTION" <<'PY'
import sys
from pathlib import Path

directory = Path(sys.argv[1]).resolve()
retention = int(sys.argv[2])
archives = sorted(directory.glob("prepdojo-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
for archive in archives[retention:]:
    if archive.resolve().parent != directory:
        raise SystemExit(f"拒绝删除备份目录之外的路径: {archive}")
    archive.unlink()
PY

echo "备份完成：$FINAL"
