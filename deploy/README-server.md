# PrepDojo 多用户 HTTPS 部署手册

本手册面向受控团队或小规模公共访问。多用户部署不是“打开一个开关”：必须同时启用登录、Docker 判题、调用配额、HTTPS、Secure Cookie、Host 白名单、私有文件权限与备份。判题容器用于降低用户代码风险，但不是虚拟机级强隔离；高风险、强对抗或大规模公网场景应使用独立主机/虚拟机和专业沙箱。

推荐拓扑：

```text
Internet ── HTTPS :443 ── Caddy ── HTTP 127.0.0.1:8686 ── PrepDojo
                                                   └──── Docker judge
```

PrepDojo 不应直接监听公网端口。防火墙只开放 80/443，8686 仅供本机 Caddy 使用。

## 1. 安装与配置

要求：Linux、Python 3.10+、Docker Engine、Caddy，以及一个已解析到服务器的域名。只有使用可选的 `deploy/serve.sh` 时才需要 util-linux 提供的 `flock`；systemd 生产部署不依赖该脚本。

```bash
# 先建立不可登录的专用账号与私有数据目录
sudo useradd --system --user-group --home /var/lib/prepdojo --shell /usr/sbin/nologin prepdojo
sudo usermod -aG docker prepdojo
sudo install -d -o prepdojo -g prepdojo -m 0700 /var/lib/prepdojo

# /opt 通常不可由普通用户写，代码和虚拟环境由 root 安装、服务账号只读
sudo git clone https://github.com/yudongfang-thu/PrepDojo.git /opt/prepdojo
sudo python3 -m venv /opt/prepdojo/.venv
sudo /opt/prepdojo/.venv/bin/python -m pip install --upgrade pip==26.2.1
sudo /opt/prepdojo/.venv/bin/python -m pip install -r /opt/prepdojo/requirements.txt
sudo /opt/prepdojo/.venv/bin/python -m pip check
cd /opt/prepdojo

# 构建并验证判题镜像
sudo docker build -f deploy/Dockerfile.judge -t prepdojo-judge:latest .
sudo -u prepdojo docker info
sudo -u prepdojo docker image inspect prepdojo-judge:latest
sudo -u prepdojo docker run --rm --network none prepdojo-judge:latest python3 -c 'print("judge image ok")'

# 代码树在运行时保持只读；所有可变数据均放到仓库外
sudo chown -R root:root /opt/prepdojo
```

先执行 `sudo install -o prepdojo -g prepdojo -m 0600 /dev/null /var/lib/prepdojo/config.yaml`，再用 `sudoedit /var/lib/prepdojo/config.yaml` 写入下列内容，并将域名替换为真实值：

```yaml
llm:
  api_key: ""                   # 也可由管理员登录后保存
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  temperature: 0.3
  timeout: 120
judge:
  time_limit_ms: 5000
  mem_limit_mb: 512
  cpp_compiler: "g++"
  docker_image: "prepdojo-judge:latest"
server:
  multiuser: true
  registration: "off"          # 默认仅管理员建号；开放前先评估滥用风险
  registration_code: ""
  daily_limit_per_user: 100
  daily_limit_global: 1000
  llm_concurrency_per_user: 2
  llm_concurrency_global: 4
  judge_concurrency_per_user: 2
  judge_concurrency_global: 4
  secure_cookie: true
  allowed_hosts:
    - "prepdojo.example.com"
    - "127.0.0.1"               # 本机健康检查
    - "localhost"
```

```bash
sudo chown prepdojo:prepdojo /var/lib/prepdojo/config.yaml
sudo chmod 0600 /var/lib/prepdojo/config.yaml

# 创建首位管理员；密码至少 8 位。环境变量方式适合自动化，但注意 shell 历史与进程环境。
sudo -u prepdojo env PREPDOJO_DATA_DIR=/var/lib/prepdojo \
  /opt/prepdojo/.venv/bin/python -m prepdojo.cli user add admin --admin
```

配置错误会阻止启动。多用户模式缺少 Docker、daemon 不可用或镜像不存在时也会直接失败，不会退回宿主判题。

## 2. systemd 与 Caddy

生产环境使用 systemd；不要同时运行 `deploy/serve.sh`。

```bash
sudo cp /opt/prepdojo/deploy/prepdojo.service.example /etc/systemd/system/prepdojo.service
sudo systemctl daemon-reload
sudo systemctl enable --now prepdojo
sudo systemctl status prepdojo
curl --fail http://127.0.0.1:8686/api/health
```

将 [Caddyfile.example](Caddyfile.example) 中的域名替换为真实域名，再安装并重载：

```bash
sudo cp /opt/prepdojo/deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail https://prepdojo.example.com/api/health
```

Caddy 自动申请证书需要公网 DNS 正确、80/443 可达。正式访问必须使用 HTTPS；不要用 `http://服务器IP:8686` 绕过代理。

若只做临时运维测试，可用 `./deploy/serve.sh start|status|stop|restart`。脚本默认绑定 `127.0.0.1`，会等待健康检查通过才报告成功；可通过 `PREPDOJO_DATA_DIR`、`PREPDOJO_HOST`、`PREPDOJO_PORT` 覆盖。相对路径统一以仓库根目录解析，生产环境仍建议使用绝对路径。多用户安全校验不可绕过。

## 3. 账号、注册与配额

```bash
sudo -u prepdojo env PREPDOJO_DATA_DIR=/var/lib/prepdojo \
  /opt/prepdojo/.venv/bin/python -m prepdojo.cli user add alice
sudo -u prepdojo env PREPDOJO_DATA_DIR=/var/lib/prepdojo \
  /opt/prepdojo/.venv/bin/python -m prepdojo.cli user passwd alice
sudo -u prepdojo env PREPDOJO_DATA_DIR=/var/lib/prepdojo \
  /opt/prepdojo/.venv/bin/python -m prepdojo.cli user list
```

- 优先由管理员建号，保持 `registration: off`。
- 邀请码注册使用 `registration: code` 并设置不可猜测的邀请码；泄露后立即轮换。
- `registration: open` 只适合另有入口限流和滥用防护的场景。
- 用户上限和全站上限均按自然日计算；`0` 表示不限制，不建议公共部署使用 `0`。
- 重置密码或删除账号属于安全操作，应检查活跃会话和审计日志。

## 4. 备份与恢复

`backup.sh` 使用 SQLite 在线备份 API，先检查源库，再验证目标库，连同 `config.yaml` 和 SHA-256 清单打包；最终文件在同一文件系统内原子发布。默认保留 14 份。

建议备份到仓库和数据盘之外，并限制为 PrepDojo 用户可读：

```bash
sudo install -d -o prepdojo -g prepdojo -m 0700 /srv/backups/prepdojo
sudo -u prepdojo touch /var/lib/prepdojo/backup.log
sudo chmod 0600 /var/lib/prepdojo/backup.log
sudo -u prepdojo env \
  PREPDOJO_DATA_DIR=/var/lib/prepdojo \
  PREPDOJO_BACKUP_DIR=/srv/backups/prepdojo \
  PREPDOJO_BACKUP_RETENTION=30 \
  /opt/prepdojo/deploy/backup.sh
```

cron 示例（以 `prepdojo` 用户安装）：

```cron
0 4 * * * PREPDOJO_DATA_DIR=/var/lib/prepdojo PREPDOJO_BACKUP_DIR=/srv/backups/prepdojo PREPDOJO_BACKUP_RETENTION=30 /opt/prepdojo/deploy/backup.sh >>/var/lib/prepdojo/backup.log 2>&1
```

备份包含 API key、密码哈希、会话、用户代码和学习记录，必须加密传输并保存到访问受控的异机/对象存储。只在同一块磁盘保留副本不算灾备。

该脚本只备份 SQLite 数据库和 `config.yaml`。知识接入所引用的原始 PDF/Markdown/TXT、Caddy 配置、systemd 单元及其他运维文件不在包内，必须另行备份；恢复原始资料时应尽量保持原路径，避免来源引用失效。

恢复前先保留当前数据。以下代码块会替换当前数据库和配置，必须整块执行；它在一个 fail-fast 的 root shell 中执行（因为备份目录是 0700），归档、哈希、配置或 SQLite 校验任一失败都会在移动现有数据之前退出：

```bash
sudo bash <<'BASH'
set -Eeuo pipefail
umask 077
restore_dir=$(mktemp -d)
trap 'rm -rf -- "$restore_dir"' EXIT

# 只提取备份脚本定义的三个普通文件，拒绝额外路径和符号链接。
/opt/prepdojo/.venv/bin/python - \
  /srv/backups/prepdojo/prepdojo-YYYYMMDDTHHMMSSZ.tar.gz "$restore_dir" <<'PY'
import shutil, sys, tarfile
from pathlib import Path

archive, destination = sys.argv[1:]
required = {"prepdojo.db", "config.yaml", "manifest.json"}
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    if {member.name for member in members} != required or len(members) != len(required):
        raise SystemExit("备份归档文件集合不正确")
    if any(not member.isfile() for member in members):
        raise SystemExit("备份归档只允许普通文件")
    for member in members:
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"无法读取备份成员: {member.name}")
        with (Path(destination) / member.name).open("xb") as target:
            shutil.copyfileobj(source, target, length=1 << 20)
PY

# 校验清单和数据库；任一不一致都会非零退出
/opt/prepdojo/.venv/bin/python - "$restore_dir" <<'PY'
import hashlib, json, os, sqlite3, sys
from pathlib import Path
d = Path(sys.argv[1])

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
required = {"prepdojo.db", "config.yaml"}
if set(m.get("files", {})) != required:
    raise SystemExit("备份清单文件集合不正确")
for name in sorted(required):
    expected = m["files"][name]
    path = d / name
    if path.stat().st_size != expected["size"]:
        raise SystemExit(f"大小校验失败: {name}")
    actual = sha256_file(path)
    if actual != expected["sha256"]:
        raise SystemExit(f"校验失败: {name}")
db = sqlite3.connect(f"file:{(d / 'prepdojo.db').resolve()}?mode=ro", uri=True)
result = db.execute("PRAGMA integrity_check").fetchone()[0]
db.close()
if result != "ok":
    raise SystemExit(f"数据库损坏: {result}")
sys.path.insert(0, "/opt/prepdojo")
# 只校验归档内的配置，不允许当前 root shell 的环境变量掩盖错误。
for key in list(os.environ):
    if key.startswith("PREPDOJO_") or key in {"DEEPSEEK_API_KEY", "OPENAI_API_BASE"}:
        os.environ.pop(key)
from prepdojo.config import load_config
load_config(d / "config.yaml")
print("备份校验通过")
PY

# 先把当前主库、配置及所有 SQLite sidecar 一起移入可回滚目录，
# 防止旧 WAL/SHM/journal 在新主库启动时被重放。
sudo systemctl stop prepdojo
rollback_dir="/var/lib/prepdojo/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
sudo -u prepdojo mkdir -m 0700 "$rollback_dir"
for name in prepdojo.db prepdojo.db-wal prepdojo.db-shm prepdojo.db-journal config.yaml; do
  if sudo test -e "/var/lib/prepdojo/$name"; then
    sudo mv -- "/var/lib/prepdojo/$name" "$rollback_dir/"
  fi
done

sudo install -o prepdojo -g prepdojo -m 0600 "$restore_dir/prepdojo.db" /var/lib/prepdojo/prepdojo.db
sudo install -o prepdojo -g prepdojo -m 0600 "$restore_dir/config.yaml" /var/lib/prepdojo/config.yaml
for name in prepdojo.db-wal prepdojo.db-shm prepdojo.db-journal; do
  sudo test ! -e "/var/lib/prepdojo/$name" || { echo "发现残留 sidecar: $name"; exit 1; }
done
sudo systemctl start prepdojo
ready=false
for _ in {1..60}; do
  if /opt/prepdojo/.venv/bin/python -c \
    'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8686/api/health", timeout=.5)); raise SystemExit(0 if d.get("ok") and d.get("judge", {}).get("mode") == "docker" else 1)' \
    >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.5
done
[[ "$ready" == true ]] || { echo "PrepDojo 恢复后未在 30 秒内就绪" >&2; exit 1; }
BASH
```

至少定期在隔离环境做一次恢复演练；“脚本运行成功”不等于备份可恢复。

## 5. 升级与安全边界

```bash
(
set -Eeuo pipefail
cd /opt/prepdojo

# 先生成可验证的库/配置备份，并保留当前判题镜像标签
sudo -u prepdojo env \
  PREPDOJO_DATA_DIR=/var/lib/prepdojo \
  PREPDOJO_BACKUP_DIR=/srv/backups/prepdojo \
  PREPDOJO_BACKUP_RETENTION=30 \
  /opt/prepdojo/deploy/backup.sh
previous_commit=$(sudo git -C /opt/prepdojo rev-parse HEAD)
rollback_image="prepdojo-judge:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
sudo docker image inspect prepdojo-judge:latest >/dev/null
sudo docker tag prepdojo-judge:latest "$rollback_image"
printf '回滚基线：commit=%s image=%s\n' "$previous_commit" "$rollback_image"

# 避免旧 Python 进程在升级窗口读到新前端静态文件，导致 API 契约混版。
sudo systemctl stop prepdojo
sudo git pull --ff-only
sudo /opt/prepdojo/.venv/bin/python -m pip install --upgrade pip==26.2.1
sudo /opt/prepdojo/.venv/bin/python -m pip install -r /opt/prepdojo/requirements.txt
sudo /opt/prepdojo/.venv/bin/python -m pip check
sudo /opt/prepdojo/.venv/bin/python -m compileall -q /opt/prepdojo/prepdojo
sudo docker build -f deploy/Dockerfile.judge -t prepdojo-judge:latest .
sudo systemctl start prepdojo
ready=false
for _ in {1..60}; do
  if /opt/prepdojo/.venv/bin/python -c \
    'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8686/api/health", timeout=.5)); raise SystemExit(0 if d.get("ok") and d.get("judge", {}).get("mode") == "docker" else 1)' \
    >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.5
done
[[ "$ready" == true ]] || { echo "PrepDojo 升级后未在 30 秒内就绪" >&2; exit 1; }
)
```

完整 pytest 与 Docker 集成测试应在 CI 或安装了 `requirements-dev.txt` 的预发布环境通过后再升级生产机；生产虚拟环境只安装 `requirements.txt`。
升级后必须检查健康端点并做一次 Python/C++ 判题烟雾测试。失败时先停服务，用记录的 commit 恢复代码/依赖，将保留的 rollback 镜像重新 tag 为 `prepdojo-judge:latest`；如新版已修改数据库 schema，再按上文经验证的备份恢复，不要用新程序继续写旧库。

- `data/`、外置数据目录、备份和环境文件绝不提交 Git。
- LLM 功能会把用户提供的知识文本、题目、代码和对话上下文发送到所配置的第三方 API；部署者必须向用户披露并取得必要授权。
- Docker 沙箱已断网、只读并限制资源，仍不能替代虚拟机级租户隔离。Docker daemon 权限等同主机高权限，只授予专用服务账号。
- 定期更新操作系统、Docker、Caddy 和锁定依赖；关注 CI 的依赖审计结果。
- 对 Internet-facing 服务另行配置防火墙、监控、日志轮转、异机备份与入口层速率限制。
