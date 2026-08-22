"""PrepDojo 命令行入口。

用法：
  prepdojo seed                 # 导入种子代码题
  prepdojo ingest <dir> [...]   # 接入知识目录（--dry-run 无需 API key）
  prepdojo quiz                 # 终端八股练习
  prepdojo serve                # 启动本地 Web UI
  prepdojo stats                # 查看统计
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import SEEDS_DIR, Config, ConfigError, ensure_dirs, load_config
from .db import DB


def _db(cfg: Config) -> DB:
    return DB(cfg.db_path)


def cmd_seed(cfg: Config, args: argparse.Namespace) -> None:
    from .seed_loader import load_seed_dir

    db = _db(cfg)
    n = load_seed_dir(db, SEEDS_DIR / "coding")
    s = db.stats()
    print(f"种子题导入完成：{n} 道；库中共 {s['problems']} 道代码题。")


def cmd_ingest(cfg: Config, args: argparse.Namespace) -> None:
    from .ingest import ingest_dir
    from .llm import LLMClient, LLMNotConfigured

    ensure_dirs()
    db = _db(cfg)
    llm = None
    if not args.dry_run:
        try:
            llm = LLMClient(cfg.base_url, cfg.api_key, cfg.model, cfg.timeout, cfg.temperature)
        except LLMNotConfigured as e:
            print(f"错误：{e}", file=sys.stderr)
            sys.exit(2)
    stats = ingest_dir(
        Path(args.directory), db, cfg, llm,
        limit_files=args.limit_files, limit_blocks=args.limit_blocks,
        dry_run=args.dry_run, sleep_s=args.sleep,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_quiz(cfg: Config, args: argparse.Namespace) -> None:
    from .quiz import grade_answer
    from .llm import LLMClient, LLMNotConfigured

    db = _db(cfg)
    try:
        llm = LLMClient(cfg.base_url, cfg.api_key, cfg.model, cfg.timeout, cfg.temperature)
    except LLMNotConfigured as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)

    tags = args.tags.split(",") if args.tags else None
    cards = db.pick_cards(tags=tags, n=args.num)
    if not cards:
        print("题库为空或近期题已刷完。先用 `prepdojo ingest <知识目录>` 接入知识。")
        return
    for card in cards:
        print("\n" + "=" * 60)
        print(f"[{card['id']}] 难度{card['difficulty']} 标签: {', '.join(card['topic_tags'])}")
        print(f"问：{card['question']}")
        input("\n(思考后按回车作答，输入多行，单独一行 END 结束)")
        lines = []
        while True:
            line = input("> ")
            if line.strip() == "END":
                break
            lines.append(line)
        answer = "\n".join(lines)
        result = grade_answer(llm, card, answer)
        db.record_attempt(card["id"], card["question"], answer,
                          result.get("score", 0), result, mode="cli")
        print(f"\n得分：{result.get('score')}/10")
        print(f"总评：{result.get('overall')}")
        print(f"追问：{result.get('follow_up')}")
        print(f"遗漏：{result.get('missed')}")


def _is_loopback_host(host: str) -> bool:
    """判断监听地址是否只可由本机访问。"""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _docker_preflight(image: str) -> None:
    """确认 Docker CLI、daemon 和配置的判题镜像均可用。"""
    if not shutil.which("docker"):
        raise ConfigError("多用户模式必须使用 Docker，但系统中找不到 docker 命令")
    checks = (
        (["docker", "info"], "Docker daemon 不可用"),
        (["docker", "image", "inspect", image], f"判题镜像不存在: {image}"),
    )
    for command, message in checks:
        try:
            result = subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError(f"{message}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            suffix = f"（{detail[-1][:200]}）" if detail else ""
            raise ConfigError(message + suffix)


def _validate_serve_mode(cfg: Config, host: str, multiuser: bool) -> None:
    loopback = _is_loopback_host(host)
    if not loopback and not multiuser:
        raise ConfigError("非 loopback 地址禁止单用户模式；请配置 multiuser: true")
    if multiuser and not cfg.judge_docker_image:
        raise ConfigError("多用户模式必须配置 judge.docker_image，禁止在宿主机执行用户代码")
    if multiuser and not cfg.secure_cookie:
        raise ConfigError("多用户模式必须设置 server.secure_cookie: true 并通过 HTTPS 访问")
    if multiuser and "*" in cfg.allowed_hosts:
        raise ConfigError("多用户模式禁止 server.allowed_hosts 使用通配符 *")
    if not loopback:
        if all(_is_loopback_host(item) for item in cfg.allowed_hosts):
            raise ConfigError("非 loopback 部署必须在 server.allowed_hosts 中明确填写访问域名或服务器 IP")
    if multiuser:
        _docker_preflight(cfg.judge_docker_image)


def cmd_serve(cfg: Config, args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ConfigError("serve 端口必须是 1..65535")
    multiuser = args.multiuser or cfg.multiuser
    _validate_serve_mode(cfg, args.host, multiuser)
    ensure_dirs()
    import uvicorn

    from .seed_loader import load_seed_dir
    from .web.server import create_app

    db = _db(cfg)
    # 首次使用自动导入种子题，朋友拿到包即可一条命令开玩
    if db.stats()["problems"] == 0:
        n = load_seed_dir(db, SEEDS_DIR / "coding")
        print(f"[首次启动] 已自动导入 {n} 道种子代码题")
    if multiuser:
        users = db.list_users()
        if not users:
            print("[多用户模式] 库中还没有任何用户：请先执行 "
                  "`.venv/bin/python -m prepdojo.cli user add <名字> --admin` 创建管理员。")
    app = create_app(cfg, db, multiuser=multiuser)
    if not cfg.llm_ready:
        print("[提示] 未配置 LLM API key：判题/学习可用，AI 点评/讲解/八股打分不可用。"
              "配置方法见 data/config.yaml 或 README。")
    mode = "多用户（需登录）" if multiuser else "单机模式"
    display_host = "localhost" if _is_loopback_host(args.host) else args.host
    if multiuser:
        print(f"\n  PrepDojo 内部监听: http://{display_host}:{args.port}  [{mode}]"
              "\n  请通过配置好的 HTTPS 反向代理域名访问。\n")
    else:
        print(f"\n  PrepDojo 已启动: http://{display_host}:{args.port}  [{mode}]\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def _read_password() -> str:
    """密码来源：环境变量 PREPDOJO_USER_PASSWORD（非交互）> 终端两次输入。"""
    import getpass
    import os

    env = os.environ.get("PREPDOJO_USER_PASSWORD")
    if env:
        if len(env) < 8:
            raise ConfigError("PREPDOJO_USER_PASSWORD 至少需要 8 个字符")
        return env
    while True:
        a = getpass.getpass("设置密码: ")
        if len(a) < 8:
            print("密码至少 8 位，请重试。")
            continue
        b = getpass.getpass("再输入一次: ")
        if a != b:
            print("两次不一致，请重试。")
            continue
        return a


def cmd_user(cfg: Config, args: argparse.Namespace) -> None:
    db = _db(cfg)
    action = args.action
    if action == "add":
        if not db.create_user(args.name, _read_password(), bool(args.admin)):
            sys.exit("创建失败：用户名已存在或非法（勿含空格/引号/斜杠）")
        print(f"已创建用户 {args.name}" + ("（管理员）" if args.admin else ""))
    elif action == "list":
        for u in db.list_users():
            role = "管理员" if u["is_admin"] else "成员"
            print(f"{u['username']:<16} {role}  今日AI调用 {db.llm_usage_today(u['username'])} 次")
    elif action == "passwd":
        if not db.set_user_password(args.name, _read_password()):
            sys.exit(f"用户不存在: {args.name}")
        print(f"已重置 {args.name} 的密码")
    elif action == "del":
        if not db.delete_user(args.name):
            sys.exit(f"用户不存在: {args.name}")
        print(f"已删除用户 {args.name}（其练习记录保留）")
    else:
        sys.exit(f"未知操作: {action}")


def cmd_stats(cfg: Config, args: argparse.Namespace) -> None:
    db = _db(cfg)
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    tags = db.all_tags()[:15]
    if tags:
        print("高频标签：", "、".join(f"{t}({n})" for t, n in tags))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prepdojo", description="秋招刷题小助手（local-first）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="导入种子代码题")

    p_ing = sub.add_parser("ingest", help="接入知识目录（PDF/MD/TXT → LLM 结构化题卡）")
    p_ing.add_argument("directory", help="知识目录路径")
    p_ing.add_argument("--limit-files", type=int, default=None)
    p_ing.add_argument("--limit-blocks", type=int, default=None, help="每文件最多处理块数")
    p_ing.add_argument("--dry-run", action="store_true", help="只抽取分块统计，不调 LLM")
    p_ing.add_argument("--sleep", type=float, default=0.2, help="每次 LLM 调用间隔秒数")

    p_quiz = sub.add_parser("quiz", help="终端八股练习")
    p_quiz.add_argument("--tags", default=None, help="逗号分隔标签过滤")
    p_quiz.add_argument("--num", type=int, default=5)

    p_serve = sub.add_parser("serve", help="启动本地 Web UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8686)
    p_serve.add_argument("--multiuser", action="store_true",
                         help="启用多用户登录（也可在 config.yaml 设 multiuser: true）")

    sub.add_parser("stats", help="查看统计")

    p_user = sub.add_parser("user", help="用户管理（多用户模式）")
    u_sub = p_user.add_subparsers(dest="action", required=True)
    u_add = u_sub.add_parser("add", help="创建用户")
    u_add.add_argument("name")
    u_add.add_argument("--admin", action="store_true", help="设为管理员")
    u_sub.add_parser("list", help="列出用户")
    u_pw = u_sub.add_parser("passwd", help="重置密码")
    u_pw.add_argument("name")
    u_del = u_sub.add_parser("del", help="删除用户")
    u_del.add_argument("name")

    args = parser.parse_args(argv)
    handlers = {"seed": cmd_seed, "ingest": cmd_ingest, "quiz": cmd_quiz,
                "serve": cmd_serve, "stats": cmd_stats, "user": cmd_user}
    old_umask = os.umask(0o077)
    try:
        # 所有命令都可能读写数据库；先建立 0700 数据目录和 0600 配置，
        # 避免 `seed` / `stats` 等非 serve 命令创建出可被其他用户读取的文件。
        ensure_dirs()
        cfg = load_config()
        handlers[args.command](cfg, args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    sys.exit(main())
