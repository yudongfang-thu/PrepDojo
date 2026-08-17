# PrepDojo 服务器内测版部署手册（server-beta 分支）

面向场景：实验室同学在校园网内一起用（~10 人规模），部署在 Linux 服务器（如 ladd90）。
单机版用法见根目录 README；本手册只覆盖服务器部署。

## 架构变化（相对 main 分支）

- **多用户**：登录 + Cookie 会话；提交/练习/学习进度按用户隔离，题库与八股知识库全组共享。
- **判题沙箱 Docker 化**：所有用户代码在一次性容器内执行（断网 `--network=none`、
  只读 rootfs、内存/CPU/进程数限额），容器内没有宿主 `data/`，API key 无法被提交的代码读到。
- **API Key 混合**：服务器共享 key 由管理员在设置页配置；成员可在设置页填自己的 key，
  填了就用自己的额度。可配每用户每日 AI 调用上限。
- **权限**：知识库管理（ingest/出题/导入/删题）、全局 LLM 配置、用户管理仅管理员可用。

## 首次部署

```bash
# 1) 服务器上获取代码（公开仓库直接 clone）
git clone https://github.com/yudongfang-thu/PrepDojo.git
cd PrepDojo

# 2) 装依赖（Python 3.10+）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3) 构建判题沙箱镜像（一次性；判题与 AI 出题的沙箱验证都在里面跑）
docker build -f deploy/Dockerfile.judge -t prepdojo-judge:latest .

# 4) 写服务器配置 data/config.yaml（首次会自动生成模板，改成下面这样）
cat > data/config.yaml <<'EOF'
multiuser: true                 # 开启登录
judge_docker_image: prepdojo-judge:latest   # 判题走 Docker 沙箱
cpp_compiler: g++               # 容器镜像里是 g++（自带 bits/stdc++.h）
llm:
  api_key: ""                   # 管理员也可登录后在网页设置页填写
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  daily_limit_per_user: 0       # 每人每日 AI 调用上限，0=不限；防跑飞可设 100
EOF

# 5) 创建管理员（账号即登录名；--admin 才能进知识库与设置）
.venv/bin/python -m prepdojo.cli user add ydf --admin
#   非交互（脚本里用）：PREPDOJO_USER_PASSWORD=xxx .venv/bin/python -m prepdojo.cli user add ydf --admin

# 6) 启动
chmod +x deploy/serve.sh && ./deploy/serve.sh start
```

访问 `http://10.103.12.90:8686`（校园网内）。给同学开账号：管理员登录 →
设置页 → 用户管理 → 添加用户；或 CLI `user add <名字>`。

## 日常运维

```bash
./deploy/serve.sh status|restart|stop    # 服务管理（日志 data/serve.log）
.venv/bin/python -m prepdojo.cli user list        # 看用户与今日 AI 用量
.venv/bin/python -m prepdojo.cli user passwd 名字 # 重置密码
```

- **每日备份**（crontab -e，一行即可，日期逻辑在脚本里）：
  `0 4 * * * /mnt/dataY/ydf/projects/PrepDojo/deploy/backup.sh >> /mnt/dataY/ydf/projects/PrepDojo/data/backup.log 2>&1`
  备份落在 `data/backups/`，自动保留最近 14 份。
- **升级代码**：开发机 rsync 后 `./deploy/serve.sh restart`（数据库 schema 自动迁移，
  单用户旧数据会归属到 `local` 用户，不影响新用户）。
- **镜像升级**（改 Dockerfile 后）：重新 build，然后 restart 服务。

## 安全说明（诚实边界）

- 判题在 Docker 沙箱内（断网/只读/限额），防一般恶意代码；但不是银行级隔离，
  仅面向实验室互信小团体，别暴露到公网。
- 登录走 HTTP 明文（校园网内可接受）；介意的话前面挂 Caddy/nginx 加自签 HTTPS。
- 服务器共享 key 在 `data/config.yaml`（仅服务器文件权限保护）；成员的个人 key
  存 SQLite，均不入 git。
