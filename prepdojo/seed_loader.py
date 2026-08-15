"""种子题载入：seeds/coding/*.json → SQLite。

种子题（题面与用例）全部为 PrepDojo 原创自写，可安全开源；
期望输出由 scripts/gen_seeds.py 用参考解实际运行生成，保证自洽。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_seed_dir(db, seed_dir: Path) -> int:
    seed_dir = Path(seed_dir)
    if not seed_dir.is_dir():
        raise FileNotFoundError(f"种子目录不存在: {seed_dir}")
    count = 0
    for fp in sorted(seed_dir.glob("*.json")):
        obj = json.loads(fp.read_text(encoding="utf-8"))
        problem = {
            "id": obj["id"],
            "title": obj["title"],
            "difficulty": obj["difficulty"],
            "tags": obj["tags"],
            "statement": obj["statement"],
            "time_limit_ms": obj.get("time_limit_ms", 5000),
            "mem_limit_mb": obj.get("mem_limit_mb", 512),
            "languages": obj.get("languages", ["python", "cpp"]),
        }
        cases = [{"input": c["input"], "output": c["output"], "sample": c.get("sample", False)}
                 for c in obj["test_cases"]]
        db.upsert_problem(problem, cases)
        count += 1
    return count
