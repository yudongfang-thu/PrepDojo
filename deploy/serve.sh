#!/bin/bash
# 服务器上启动/重启 PrepDojo（多用户内测版）
# 用法：./deploy/serve.sh [start|stop|restart|status]
set -u
cd "$(dirname "$0")/.."

PORT=8686
PIDFILE=data/serve.pid

running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-start}" in
  start)
    if running; then echo "已在运行: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT (pid $(cat $PIDFILE))"; exit 0; fi
    mkdir -p data
    nohup .venv/bin/python -m prepdojo.cli serve --host 0.0.0.0 --port $PORT >> data/serve.log 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if running; then
      echo "已启动: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT (pid $(cat $PIDFILE)，日志 data/serve.log)"
    else
      echo "启动失败，最近日志："; tail -20 data/serve.log; exit 1
    fi
    ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE" && echo "已停止"; else echo "未在运行"; fi
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    if running; then
      echo "运行中 (pid $(cat "$PIDFILE"))"; tail -5 data/serve.log
    else
      echo "未在运行"
    fi
    ;;
  *) echo "用法: $0 [start|stop|restart|status]"; exit 1;;
esac
