"""SQLite 存储层：知识题卡、代码题、提交记录、八股练习记录。

所有用户数据都在本地 data/prepdojo.db（.gitignore 排除）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE NOT NULL,
  sha256 TEXT NOT NULL,
  title TEXT,
  n_cards INTEGER NOT NULL DEFAULT 0,
  ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  answer_points TEXT NOT NULL,
  follow_ups TEXT NOT NULL,
  topic_tags TEXT NOT NULL,
  difficulty INTEGER NOT NULL DEFAULT 2,
  source_id INTEGER REFERENCES sources(id),
  source_ref TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL REFERENCES cards(id),
  question_snapshot TEXT,
  answer TEXT NOT NULL,
  score REAL,
  feedback TEXT,
  mode TEXT NOT NULL DEFAULT 'standard',
  asked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coding_problems (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  difficulty TEXT NOT NULL,
  tags TEXT NOT NULL,
  statement TEXT NOT NULL,
  time_limit_ms INTEGER NOT NULL DEFAULT 5000,
  mem_limit_mb INTEGER NOT NULL DEFAULT 512,
  languages TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  problem_id TEXT NOT NULL REFERENCES coding_problems(id),
  idx INTEGER NOT NULL,
  input TEXT NOT NULL,
  expected_output TEXT NOT NULL,
  is_sample INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  problem_id TEXT NOT NULL,
  language TEXT NOT NULL,
  code TEXT NOT NULL,
  verdict TEXT NOT NULL,
  detail TEXT,
  runtime_ms INTEGER,
  reviewed INTEGER NOT NULL DEFAULT 0,
  review TEXT,
  submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_tags ON cards(topic_tags);
CREATE INDEX IF NOT EXISTS idx_cases_problem ON test_cases(problem_id);
CREATE INDEX IF NOT EXISTS idx_submissions_problem ON submissions(problem_id);
CREATE INDEX IF NOT EXISTS idx_attempts_card ON quiz_attempts(card_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 同步端点跑在线程池：允许跨线程使用，用锁串行化（单用户本地足够）
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """线程安全的执行入口（普通写操作自动提交）。

        带 RETURNING 的写语句不在此提交（cursor 未消费前 commit 会报
        "SQL statements in progress"），调用方 fetch 完成后调用 commit()。
        """
        with self._lock:
            cur = self.conn.execute(sql, params)
            head = sql.lstrip().upper()
            if not head.startswith("SELECT") and "RETURNING" not in sql.upper():
                self.conn.commit()
            return cur

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    # ---------- sources / cards ----------

    def upsert_source(self, path: str, sha256: str, title: str) -> int:
        cur = self.execute(
            "INSERT INTO sources(path, sha256, title, n_cards, ingested_at) "
            "VALUES(?,?,?,0,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
            "title=excluded.title, ingested_at=excluded.ingested_at RETURNING id",
            (path, sha256, title, _now()),
        )
        row = cur.fetchone()
        self.commit()  # RETURNING 写：消费后再提交
        return int(row["id"])

    def source_sha(self, path: str) -> Optional[str]:
        row = self.execute("SELECT sha256 FROM sources WHERE path=?", (path,)).fetchone()
        return row["sha256"] if row else None

    def update_source_count(self, source_id: int) -> None:
        self.execute(
            "UPDATE sources SET n_cards=(SELECT COUNT(*) FROM cards WHERE source_id=?) WHERE id=?",
            (source_id, source_id),
        )

    def insert_card(
        self,
        question: str,
        answer_points: list[str],
        follow_ups: list[str],
        topic_tags: list[str],
        difficulty: int,
        source_id: Optional[int],
        source_ref: str,
    ) -> str:
        cid = "kc-" + uuid.uuid4().hex[:12]
        self.execute(
            "INSERT INTO cards(id, question, answer_points, follow_ups, topic_tags, difficulty, "
            "source_id, source_ref, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                cid,
                question,
                json.dumps(answer_points, ensure_ascii=False),
                json.dumps(follow_ups, ensure_ascii=False),
                json.dumps(topic_tags, ensure_ascii=False),
                difficulty,
                source_id,
                source_ref,
                _now(),
            ),
        )
        return cid

    def get_card(self, card_id: str) -> Optional[dict[str, Any]]:
        row = self.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return self._card_row(row) if row else None

    def _card_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "question": row["question"],
            "answer_points": json.loads(row["answer_points"]),
            "follow_ups": json.loads(row["follow_ups"]),
            "topic_tags": json.loads(row["topic_tags"]),
            "difficulty": row["difficulty"],
            "source_ref": row["source_ref"],
            "created_at": row["created_at"],
        }

    def pick_cards(
        self, tags: Optional[list[str]] = None, n: int = 10,
        exclude_seen_days: int = 3, difficulty: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """抽题：默认排除最近 N 天练过的卡；无 LLM 依赖。"""
        q = (
            "SELECT c.* FROM cards c WHERE c.id NOT IN ("
            "  SELECT card_id FROM quiz_attempts WHERE asked_at >= datetime('now', ?)"
            ")"
        )
        args: list[Any] = [f"-{exclude_seen_days} days"]
        if tags:
            conds = " OR ".join("c.topic_tags LIKE ?" for _ in tags)
            q += f" AND ({conds})"
            args += [f'%"{t}"%' for t in tags]
        if difficulty is not None:
            q += " AND c.difficulty=?"
            args.append(difficulty)
        q += " ORDER BY RANDOM() LIMIT ?"
        args.append(n)
        rows = self.execute(q, args).fetchall()
        return [self._card_row(r) for r in rows]

    def all_tags(self) -> list[tuple[str, int]]:
        rows = self.execute("SELECT topic_tags FROM cards").fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            for t in json.loads(r["topic_tags"]):
                counter[t] = counter.get(t, 0) + 1
        return sorted(counter.items(), key=lambda kv: -kv[1])

    # ---------- quiz attempts ----------

    def record_attempt(
        self, card_id: str, question: str, answer: str, score: float,
        feedback: dict, mode: str = "standard",
    ) -> int:
        cur = self.execute(
            "INSERT INTO quiz_attempts(card_id, question_snapshot, answer, score, feedback, mode, asked_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (card_id, question, answer, score, json.dumps(feedback, ensure_ascii=False), mode, _now()),
        )
        return int(cur.lastrowid)

    # ---------- coding problems ----------

    def upsert_problem(self, p: dict[str, Any], cases: list[dict[str, Any]]) -> None:
        self.execute(
            "INSERT INTO coding_problems(id, title, difficulty, tags, statement, time_limit_ms, "
            "mem_limit_mb, languages, created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, difficulty=excluded.difficulty, "
            "tags=excluded.tags, statement=excluded.statement, time_limit_ms=excluded.time_limit_ms, "
            "mem_limit_mb=excluded.mem_limit_mb, languages=excluded.languages",
            (
                p["id"], p["title"], p["difficulty"], json.dumps(p["tags"], ensure_ascii=False),
                p["statement"], p.get("time_limit_ms", 5000), p.get("mem_limit_mb", 512),
                json.dumps(p.get("languages", ["python", "cpp"])), _now(),
            ),
        )
        self.execute("DELETE FROM test_cases WHERE problem_id=?", (p["id"],))
        for i, c in enumerate(cases):
            self.execute(
                "INSERT INTO test_cases(problem_id, idx, input, expected_output, is_sample) "
                "VALUES(?,?,?,?,?)",
                (p["id"], i, c["input"], c["output"], 1 if c.get("sample") else 0),
            )

    def list_problems(self) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM test_cases t WHERE t.problem_id=p.id) AS n_cases "
            "FROM coding_problems p ORDER BY p.id"
        ).fetchall()
        return [
            {
                "id": r["id"], "title": r["title"], "difficulty": r["difficulty"],
                "tags": json.loads(r["tags"]), "n_cases": r["n_cases"],
            }
            for r in rows
        ]

    def get_problem(self, pid: str) -> Optional[dict[str, Any]]:
        r = self.execute("SELECT * FROM coding_problems WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        cases = self.execute(
            "SELECT idx, input, expected_output, is_sample FROM test_cases "
            "WHERE problem_id=? ORDER BY idx", (pid,),
        ).fetchall()
        return {
            "id": r["id"], "title": r["title"], "difficulty": r["difficulty"],
            "tags": json.loads(r["tags"]), "statement": r["statement"],
            "time_limit_ms": r["time_limit_ms"], "mem_limit_mb": r["mem_limit_mb"],
            "languages": json.loads(r["languages"]),
            "samples": [
                {"input": c["input"], "output": c["expected_output"]}
                for c in cases if c["is_sample"]
            ],
            "n_cases": len(cases),
        }

    # ---------- submissions ----------

    def record_submission(
        self, problem_id: str, language: str, code: str, verdict: str,
        detail: dict, runtime_ms: int,
    ) -> int:
        cur = self.execute(
            "INSERT INTO submissions(problem_id, language, code, verdict, detail, runtime_ms, submitted_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (problem_id, language, code, verdict, json.dumps(detail, ensure_ascii=False),
             runtime_ms, _now()),
        )
        return int(cur.lastrowid)

    def get_submission(self, sid: int) -> Optional[dict[str, Any]]:
        r = self.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        return {
            "id": r["id"], "problem_id": r["problem_id"], "language": r["language"],
            "code": r["code"], "verdict": r["verdict"],
            "detail": json.loads(r["detail"]) if r["detail"] else {},
            "runtime_ms": r["runtime_ms"], "review": json.loads(r["review"]) if r["review"] else None,
            "submitted_at": r["submitted_at"],
        }

    def set_review(self, sid: int, review: dict) -> None:
        self.execute(
            "UPDATE submissions SET reviewed=1, review=? WHERE id=?",
            (json.dumps(review, ensure_ascii=False), sid),
        )

    # ---------- stats ----------

    def stats(self) -> dict[str, Any]:
        def one(q: str, *a: Any) -> int:
            return int(self.execute(q, a).fetchone()[0])

        return {
            "cards": one("SELECT COUNT(*) FROM cards"),
            "sources": one("SELECT COUNT(*) FROM sources"),
            "problems": one("SELECT COUNT(*) FROM coding_problems"),
            "submissions": one("SELECT COUNT(*) FROM submissions"),
            "ac": one("SELECT COUNT(*) FROM submissions WHERE verdict='AC'"),
            "quiz_attempts": one("SELECT COUNT(*) FROM quiz_attempts"),
            "quiz_avg_score": self.execute(
                "SELECT ROUND(AVG(score),1) FROM quiz_attempts WHERE score IS NOT NULL"
            ).fetchone()[0],
        }
