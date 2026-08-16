#!/bin/bash
# 双击启动 PrepDojo（秋招刷题小助手）
cd "$(dirname "$0")"
if lsof -i :8686 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "PrepDojo 已在运行：http://localhost:8686"
else
  .venv/bin/python -m prepdojo.cli serve --port 8686 > data/serve.log 2>&1 &
  sleep 3
  echo "PrepDojo 已启动：http://localhost:8686（日志：data/serve.log）"
fi
open http://localhost:8686
echo "（此窗口可直接关闭；停止服务：lsof -ti :8686 | xargs kill）"
sleep 5
