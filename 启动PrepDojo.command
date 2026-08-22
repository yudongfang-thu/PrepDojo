#!/usr/bin/env bash
# macOS 双击启动 PrepDojo；只有健康检查通过后才报告成功并打开浏览器。
set -u
umask 077

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT_DIR" || exit 1
PYTHON="$ROOT_DIR/.venv/bin/python"
DATA_DIR="$ROOT_DIR/data"
LOGFILE="$DATA_DIR/local-serve.log"
PIDFILE="$DATA_DIR/local-serve.pid"
URL="http://127.0.0.1:8686"
# 双击启动始终使用仓库内的本地数据，避免 Finder 继承到外部部署路径。
export PREPDOJO_DATA_DIR="$DATA_DIR"
export PREPDOJO_CONFIG_PATH="$DATA_DIR/config.yaml"

fail() {
  echo "启动失败：$1"
  if [[ -f "$LOGFILE" ]]; then
    echo "最近日志："
    tail -n 30 "$LOGFILE"
  fi
  echo "按回车关闭窗口。"
  read -r _
  exit 1
}

if [[ ! -x "$PYTHON" ]]; then
  fail "找不到 .venv/bin/python，请先按 QUICKSTART.md 安装依赖"
fi
mkdir -p "$DATA_DIR" || fail "无法创建 data 目录"
chmod 700 "$DATA_DIR" || fail "无法设置 data 目录权限"
if ! "$PYTHON" -c 'from prepdojo.config import load_config; raise SystemExit(1 if load_config().multiuser else 0)'; then
  fail "当前配置为多用户模式或配置文件有误；请按 deploy/README-server.md 通过 HTTPS 部署"
fi

healthy() {
  "$PYTHON" -c \
    'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8686/api/health", timeout=.5)); raise SystemExit(0 if d.get("ok") and d.get("judge", {}).get("mode") == "local" else 1)' \
    >/dev/null 2>&1
}

started_here=false
if healthy; then
  echo "PrepDojo 已在运行：$URL"
else
  nohup "$PYTHON" -m prepdojo.cli serve --host 127.0.0.1 --port 8686 \
    >>"$LOGFILE" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" >"$PIDFILE"
  ready=false
  for _ in {1..30}; do
    if healthy; then
      ready=true
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  if [[ "$ready" != true ]]; then
    kill "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    fail "服务未通过健康检查"
  fi
  started_here=true
  echo "PrepDojo 已启动：$URL（日志：$LOGFILE）"
fi

open "$URL"
if [[ "$started_here" == true ]]; then
  echo "此窗口可关闭。停止服务：kill \$(cat '$PIDFILE')"
else
  echo "此窗口可关闭。请用原来启动该服务的终端或管理方式停止。"
fi
sleep 3
