#!/usr/bin/env bash
# PrepDojo 多用户服务管理。默认仅监听 loopback，由 Caddy 提供 HTTPS。
# 可覆盖：PREPDOJO_DATA_DIR、PREPDOJO_HOST、PREPDOJO_PORT。
set -Eeuo pipefail
umask 077

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DATA_DIR_INPUT=${PREPDOJO_DATA_DIR:-"$ROOT_DIR/data"}
HOST=${PREPDOJO_HOST:-127.0.0.1}
PORT=${PREPDOJO_PORT:-8686}
PYTHON=${PREPDOJO_PYTHON:-"$ROOT_DIR/.venv/bin/python"}

if [[ ! -x "$PYTHON" ]]; then
  echo "错误：找不到 $PYTHON，请先创建虚拟环境并安装依赖" >&2
  exit 1
fi
DATA_DIR=$("$PYTHON" -c \
  'import sys; from pathlib import Path; p=Path(sys.argv[2]).expanduser(); print(p if p.is_absolute() else (Path(sys.argv[1])/p).resolve())' \
  "$ROOT_DIR" "$DATA_DIR_INPUT")
PIDFILE="$DATA_DIR/serve.pid"
LOGFILE="$DATA_DIR/serve.log"
dir_mode() {
  "$PYTHON" -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$1"
}
if [[ -e "$DATA_DIR" && ! -d "$DATA_DIR" ]]; then
  echo "错误：数据路径不是目录：$DATA_DIR" >&2
  exit 1
fi
if [[ ! -e "$DATA_DIR" ]]; then
  mkdir -p "$DATA_DIR"
  chmod 700 "$DATA_DIR"
elif [[ "$DATA_DIR" == "$ROOT_DIR/data" ]]; then
  chmod 700 "$DATA_DIR"
elif [[ $(dir_mode "$DATA_DIR") != 700 ]]; then
  echo "错误：外部数据目录权限必须是 0700：$DATA_DIR" >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "错误：缺少 flock（通常由 util-linux 提供）" >&2
  exit 1
fi
exec 9>"$DATA_DIR/.serve-control.lock"
if ! flock -n 9; then
  echo "错误：另一个 serve.sh 管理操作正在进行" >&2
  exit 1
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  echo "错误：PREPDOJO_PORT 必须是 1..65535 的整数" >&2
  exit 1
fi

pid_value() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid=$(<"$PIDFILE")
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$pid"
}

running() {
  local pid command
  pid=$(pid_value) || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  command=$(ps -p "$pid" -o command= 2>/dev/null || true)
  [[ "$command" == *"prepdojo.cli serve"* ]]
}

ready_url() {
  local target=${HOST#[}
  target=${target%]}
  case "$target" in
    0.0.0.0) printf 'http://127.0.0.1:%s/api/health' "$PORT" ;;
    ::) printf 'http://[::1]:%s/api/health' "$PORT" ;;
    *:*) printf 'http://[%s]:%s/api/health' "$target" "$PORT" ;;
    *) printf 'http://%s:%s/api/health' "$target" "$PORT" ;;
  esac
}

wait_ready() {
  local url
  url=$(ready_url)
  for _ in {1..60}; do
    if ! running; then
      return 1
    fi
    if "$PYTHON" -c \
      'import json,sys,urllib.request; d=json.load(urllib.request.urlopen(sys.argv[1], timeout=.5)); raise SystemExit(0 if d.get("ok") and d.get("judge", {}).get("mode") == "docker" else 1)' \
      "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

validate_deploy_config() {
  (
    cd "$ROOT_DIR"
    PREPDOJO_DATA_DIR="$DATA_DIR" "$PYTHON" - "$HOST" <<'PY'
import ipaddress
import sys
from prepdojo.cli import _docker_preflight
from prepdojo.config import load_config

cfg = load_config()
if not cfg.multiuser:
    raise SystemExit("错误：deploy/serve.sh 要求 server.multiuser: true")
if not cfg.secure_cookie:
    raise SystemExit("错误：Caddy HTTPS 部署要求 server.secure_cookie: true")
if "*" in cfg.allowed_hosts:
    raise SystemExit("错误：公共部署禁止 allowed_hosts 使用通配符 *")
external = []
for host in cfg.allowed_hosts:
    try:
        if not ipaddress.ip_address(host).is_loopback:
            external.append(host)
    except ValueError:
        if host not in {"localhost", "testserver"}:
            external.append(host)
if not external:
    raise SystemExit("错误：server.allowed_hosts 必须包含对外访问域名或服务器 IP")
listen_host = sys.argv[1].strip("[]").lower()
probe_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(listen_host, listen_host)
if probe_host not in {host.lower().rstrip(".") for host in cfg.allowed_hosts}:
    raise SystemExit(
        f"错误：server.allowed_hosts 必须包含内部健康检查主机 {probe_host}")
_docker_preflight(cfg.judge_docker_image)
PY
  )
}

start_service() {
  if running; then
    echo "已在运行：内部监听 http://$HOST:$PORT（pid $(pid_value)）"
    return 0
  fi
  validate_deploy_config
  rm -f "$PIDFILE"
  touch "$LOGFILE"
  chmod 600 "$LOGFILE"
  (
    cd "$ROOT_DIR"
    nohup "$PYTHON" -m prepdojo.cli serve \
      --host "$HOST" --port "$PORT" --multiuser 9>&- >>"$LOGFILE" 2>&1 &
    printf '%s\n' "$!" >"$PIDFILE.tmp"
    mv -f "$PIDFILE.tmp" "$PIDFILE"
  )
  if wait_ready; then
    echo "已启动：内部监听 http://$HOST:$PORT（请通过 HTTPS 域名访问；pid $(pid_value)，日志 $LOGFILE）"
    return 0
  fi
  echo "启动失败或健康检查超时，最近日志：" >&2
  tail -n 40 "$LOGFILE" >&2 || true
  if running; then
    kill "$(pid_value)" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  return 1
}

stop_service() {
  if ! running; then
    if [[ -f "$PIDFILE" ]]; then
      echo "发现失效 PID 文件，已清理"
      rm -f "$PIDFILE"
    else
      echo "未在运行"
    fi
    return 0
  fi
  local pid
  pid=$(pid_value)
  kill "$pid"
  for _ in {1..30}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PIDFILE"
      echo "已停止"
      return 0
    fi
    sleep 0.5
  done
  echo "进程未在 15 秒内退出，发送 SIGKILL" >&2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
}

case "${1:-start}" in
  start) start_service ;;
  stop) stop_service ;;
  restart) stop_service; start_service ;;
  status)
    if running; then
      echo "运行中（pid $(pid_value)，内部监听 http://$HOST:$PORT）"
      tail -n 5 "$LOGFILE" 2>/dev/null || true
    else
      echo "未在运行"
      exit 1
    fi
    ;;
  *)
    echo "用法：$0 [start|stop|restart|status]" >&2
    exit 2
    ;;
esac
