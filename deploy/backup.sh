#!/bin/bash
# 每日备份 SQLite（保留最近 14 份轮转）；crontab 示例见 README-server.md
cd "$(dirname "$0")/.."
mkdir -p data/backups
D=$(date +%F)
.venv/bin/python -c "import sqlite3
c = sqlite3.connect('data/prepdojo.db')
d = sqlite3.connect('data/backups/backup-$D.db')
c.backup(d)
d.close()"
ls -1t data/backups/backup-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f
