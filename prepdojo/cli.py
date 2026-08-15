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
import json
import sys
from pathlib import Path

from .config import SEEDS_DIR, Config, ensure_dirs, load_config
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


def cmd_serve(cfg: Config, args: argparse.Namespace) -> None:
    ensure_dirs()
    import uvicorn

    from .seed_loader import load_seed_dir
    from .web.server import create_app

    db = _db(cfg)
    # 首次使用自动导入种子题，朋友拿到包即可一条命令开玩
    if db.stats()["problems"] == 0:
        n = load_seed_dir(db, SEEDS_DIR / "coding")
        print(f"[首次启动] 已自动导入 {n} 道种子代码题")
    app = create_app(cfg, db)
    if not cfg.llm_ready:
        print("[提示] 未配置 LLM API key：判题/学习可用，AI 点评/讲解/八股打分不可用。"
              "配置方法见 data/config.yaml 或 README。")
    print(f"\n  PrepDojo 已启动: http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


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

    sub.add_parser("stats", help="查看统计")

    args = parser.parse_args(argv)
    cfg = load_config()
    {"seed": cmd_seed, "ingest": cmd_ingest, "quiz": cmd_quiz,
     "serve": cmd_serve, "stats": cmd_stats}[args.command](cfg, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
