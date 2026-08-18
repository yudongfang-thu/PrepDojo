"""SQLite 存储层：知识题卡、代码题、提交记录、八股练习记录、用户与会话。

所有用户数据都在本地 data/prepdojo.db（.gitignore 排除）。

多用户模型（server-beta）：
- 内容数据共享：sources / cards / coding_problems / test_cases。
- 个人数据按 user_id 隔离：submissions / quiz_attempts / ai_judgements /
  card_learn_state / llm_usage。单机模式统一写 'local' 用户。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .auth import hash_password, new_session_token, verify_password

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
  created_at TEXT NOT NULL,
  learned INTEGER NOT NULL DEFAULT 0,
  learned_at TEXT,
  explanation TEXT
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT 'local',
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
  user_id TEXT NOT NULL DEFAULT 'local',
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

CREATE TABLE IF NOT EXISTS ai_judgements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL DEFAULT 'local',
  problem_id TEXT NOT NULL,
  language TEXT NOT NULL,
  code TEXT NOT NULL,
  verdict TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);

-- ===== 多用户（server-beta）=====

CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL DEFAULT '',
  salt TEXT NOT NULL DEFAULT '',
  is_admin INTEGER NOT NULL DEFAULT 0,
  api_key TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

-- 学习状态从 cards 表拆出：卡内容共享，learned 按人记
CREATE TABLE IF NOT EXISTS card_learn_state (
  user_id TEXT NOT NULL,
  card_id TEXT NOT NULL REFERENCES cards(id),
  learned INTEGER NOT NULL DEFAULT 0,
  learned_at TEXT,
  PRIMARY KEY(user_id, card_id)
);

CREATE TABLE IF NOT EXISTS llm_usage (
  user_id TEXT NOT NULL,
  day TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 同步端点跑在线程池：允许跨线程使用，用锁串行化（小规模多人足够）
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """轻量迁移：为旧库补新列（ADD COLUMN 带默认值是原子操作）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(cards)")}
        for col, ddl in (
            ("learned", "ALTER TABLE cards ADD COLUMN learned INTEGER NOT NULL DEFAULT 0"),
            ("learned_at", "ALTER TABLE cards ADD COLUMN learned_at TEXT"),
            ("explanation", "ALTER TABLE cards ADD COLUMN explanation TEXT"),
        ):
            if col not in cols:
                self.conn.execute(ddl)
        # 单用户旧库 → 多用户：补 user_id 列并回填 'local'
        for table in ("submissions", "quiz_attempts", "ai_judgements"):
            tcols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in tcols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'")
            self.conn.execute(
                f"UPDATE {table} SET user_id='local' WHERE user_id IS NULL OR user_id=''")
        # user_id 索引在补列之后再建（旧库首次升级时列尚不存在）
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_user ON quiz_attempts(user_id)")
        # 旧 cards.learned 状态迁到 card_learn_state（归属 local 用户）
        self.conn.execute(
            "INSERT OR IGNORE INTO card_learn_state(user_id, card_id, learned, learned_at) "
            "SELECT 'local', id, learned, learned_at FROM cards WHERE learned=1")
        self.conn.commit()

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

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        """锁内取单值；无行时返回 None（调用方给默认值）。"""
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    # ---------- 用户 / 会话 ----------

    def create_user(self, username: str, password: str, is_admin: bool = False) -> bool:
        """创建用户；已存在或用户名非法返回 False。"""
        username = username.strip()
        if not username or any(c in username for c in "/\\'\" "):
            return False
        h, salt = hash_password(password)
        try:
            self.execute(
                "INSERT INTO users(username, password_hash, salt, is_admin, created_at) "
                "VALUES(?,?,?,?,?)",
                (username, h, salt, 1 if is_admin else 0, _now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_user(self, username: str) -> Optional[dict[str, Any]]:
        r = self.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return self._user_row(r) if r else None

    def _user_row(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "username": r["username"],
            "is_admin": bool(r["is_admin"]),
            "api_key": r["api_key"],
            "has_password": bool(r["password_hash"]),
            "created_at": r["created_at"],
        }

    def verify_login(self, username: str, password: str) -> Optional[dict[str, Any]]:
        r = self.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        if not r:
            # 恒定时间比较防时序探测（不存在用户也做一次哈希）
            verify_password(password, "00" * 32, "00" * 16)
            return None
        if not verify_password(password, r["password_hash"], r["salt"]):
            return None
        return self._user_row(r)

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._user_row(r) for r in rows]

    def delete_user(self, username: str) -> bool:
        cur = self.execute("DELETE FROM users WHERE username=?", (username,))
        self.execute("DELETE FROM sessions WHERE username=?", (username,))
        return cur.rowcount > 0

    def set_user_password(self, username: str, password: str) -> bool:
        h, salt = hash_password(password)
        cur = self.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE username=?",
            (h, salt, username))
        return cur.rowcount > 0

    def set_user_api_key(self, username: str, api_key: Optional[str]) -> bool:
        cur = self.execute(
            "UPDATE users SET api_key=? WHERE username=?",
            (api_key or None, username))
        return cur.rowcount > 0

    def create_session(self, username: str, days: int = 30) -> str:
        token = new_session_token()
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        self.execute(
            "INSERT INTO sessions(token, username, created_at, expires_at) VALUES(?,?,?,?)",
            (token, username, _now(), expires))
        return token

    def session_user(self, token: str) -> Optional[dict[str, Any]]:
        """有效会话返回用户；顺手清理过期会话。"""
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        r = self.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.username=s.username "
            "WHERE s.token=?", (token,)).fetchone()
        return self._user_row(r) if r else None

    def delete_session(self, token: str) -> None:
        self.execute("DELETE FROM sessions WHERE token=?", (token,))

    # ---------- LLM 用量 ----------

    def llm_usage_today(self, user_id: str) -> int:
        day = datetime.now().date().isoformat()
        return int(self.scalar(
            "SELECT count FROM llm_usage WHERE user_id=? AND day=?", (user_id, day)) or 0)

    def bump_llm_usage(self, user_id: str) -> int:
        day = datetime.now().date().isoformat()
        cur = self.execute(
            "INSERT INTO llm_usage(user_id, day, count) VALUES(?,?,1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1 "
            "RETURNING count", (user_id, day))
        row = cur.fetchone()
        self.commit()
        return int(row["count"])

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

    def get_card(self, card_id: str, user_id: str = "local") -> Optional[dict[str, Any]]:
        row = self.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            return None
        learned = self.scalar(
            "SELECT learned FROM card_learn_state WHERE user_id=? AND card_id=?",
            (user_id, card_id))
        return self._card_row(row, learned=bool(learned))

    def _card_row(self, row: sqlite3.Row, learned: bool = False) -> dict[str, Any]:
        return {
            "id": row["id"],
            "question": row["question"],
            "answer_points": json.loads(row["answer_points"]),
            "follow_ups": json.loads(row["follow_ups"]),
            "topic_tags": json.loads(row["topic_tags"]),
            "difficulty": row["difficulty"],
            "source_ref": row["source_ref"],
            "created_at": row["created_at"],
            "learned": learned,
            "learned_at": None,
            "explanation": row["explanation"],
        }

    def _learned_map(self, user_id: str, card_ids: list[str]) -> dict[str, bool]:
        if not card_ids:
            return {}
        marks = ",".join("?" for _ in card_ids)
        rows = self.execute(
            f"SELECT card_id, learned FROM card_learn_state "
            f"WHERE user_id=? AND card_id IN ({marks}) AND learned=1",
            [user_id, *card_ids]).fetchall()
        return {r["card_id"]: True for r in rows}

    def pick_cards(
        self, user_id: str = "local", tags: Optional[list[str]] = None, n: int = 10,
        exclude_seen_days: int = 3, difficulty: Optional[int] = None,
        only_learned: bool = False,
    ) -> list[dict[str, Any]]:
        """测验抽题：排除该用户最近 N 天练过的卡；only_learned 时仅从其已学卡抽。"""
        q = (
            "SELECT c.* FROM cards c WHERE c.id NOT IN ("
            "  SELECT card_id FROM quiz_attempts"
            "  WHERE user_id=? AND asked_at >= datetime('now', ?)"
            ")"
        )
        args: list[Any] = [user_id, f"-{exclude_seen_days} days"]
        if only_learned:
            q += " AND c.id IN (SELECT card_id FROM card_learn_state WHERE user_id=? AND learned=1)"
            args.append(user_id)
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
        learned = self._learned_map(user_id, [r["id"] for r in rows])
        return [self._card_row(r, learned.get(r["id"], False)) for r in rows]

    # ---------- 学习模式 ----------

    def pick_learn_cards(
        self, user_id: str = "local", tags: Optional[list[str]] = None, n: int = 10,
        only_unlearned: bool = True,
    ) -> list[dict[str, Any]]:
        """学习模式抽卡：默认只抽该用户未学卡。"""
        q = ("SELECT c.* FROM cards c LEFT JOIN card_learn_state cls "
             "ON cls.card_id=c.id AND cls.user_id=?")
        args: list[Any] = [user_id]
        conds: list[str] = []
        if only_unlearned:
            conds.append("(cls.learned IS NULL OR cls.learned=0)")
        if tags:
            conds.append("(" + " OR ".join("c.topic_tags LIKE ?" for _ in tags) + ")")
            args += [f'%"{t}"%' for t in tags]
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY RANDOM() LIMIT ?"
        args.append(n)
        rows = self.execute(q, args).fetchall()
        learned = self._learned_map(user_id, [r["id"] for r in rows])
        return [self._card_row(r, learned.get(r["id"], False)) for r in rows]

    def mark_learned(self, card_id: str, learned: bool, user_id: str = "local") -> bool:
        exists = self.scalar("SELECT 1 FROM cards WHERE id=?", (card_id,))
        if not exists:
            return False
        self.execute(
            "INSERT INTO card_learn_state(user_id, card_id, learned, learned_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, card_id) DO UPDATE SET learned=excluded.learned, "
            "learned_at=excluded.learned_at",
            (user_id, card_id, 1 if learned else 0, _now() if learned else None),
        )
        return True

    def set_explanation(self, card_id: str, explanation: str) -> None:
        self.execute("UPDATE cards SET explanation=? WHERE id=?",
                     (explanation, card_id))

    def learn_progress(self, user_id: str = "local") -> dict[str, Any]:
        total = int(self.scalar("SELECT COUNT(*) FROM cards") or 0)
        learned = int(self.scalar(
            "SELECT COUNT(*) FROM card_learn_state WHERE user_id=? AND learned=1",
            (user_id,)) or 0)
        learned_ids = {
            r["card_id"] for r in self.execute(
                "SELECT card_id FROM card_learn_state WHERE user_id=? AND learned=1",
                (user_id,)).fetchall()
        }
        rows = self.execute("SELECT id, topic_tags FROM cards").fetchall()
        tag_stat: dict[str, list[int]] = {}
        for r in rows:
            for t in json.loads(r["topic_tags"]):
                s = tag_stat.setdefault(t, [0, 0])
                s[1] += 1
                if r["id"] in learned_ids:
                    s[0] += 1
        tags = sorted(tag_stat.items(), key=lambda kv: -(kv[1][1] - kv[1][0]))  # 未学多的在前
        return {
            "total": total, "learned": learned,
            "tags": [{"tag": t, "learned": v[0], "total": v[1]} for t, v in tags],
        }

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
        feedback: dict, mode: str = "standard", user_id: str = "local",
    ) -> int:
        cur = self.execute(
            "INSERT INTO quiz_attempts(user_id, card_id, question_snapshot, answer, score, "
            "feedback, mode, asked_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, card_id, question, answer, score,
             json.dumps(feedback, ensure_ascii=False), mode, _now()),
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

    def list_problems(self, user_id: str = "local") -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM test_cases t WHERE t.problem_id=p.id) AS n_cases "
            "FROM coding_problems p ORDER BY p.id"
        ).fetchall()
        status = self.problem_status_map(user_id)
        return [
            {
                "id": r["id"], "title": r["title"], "difficulty": r["difficulty"],
                "tags": json.loads(r["tags"]), "n_cases": r["n_cases"],
                **status.get(r["id"], {"ever_ac": False, "attempts": 0}),
            }
            for r in rows
        ]

    def problem_status_map(self, user_id: str = "local") -> dict[str, dict[str, Any]]:
        """每题提交状态（按用户）：ever_ac / attempts / last_verdict。"""
        rows = self.execute(
            "SELECT problem_id,"
            " MAX(CASE WHEN verdict='AC' THEN 1 ELSE 0 END) AS ever_ac,"
            " COUNT(*) AS attempts,"
            " (SELECT verdict FROM submissions s2"
            "  WHERE s2.problem_id=s1.problem_id AND s2.user_id=s1.user_id"
            "  ORDER BY id DESC LIMIT 1) AS last_verdict"
            " FROM submissions s1 WHERE user_id=? GROUP BY problem_id",
            (user_id,),
        ).fetchall()
        return {r["problem_id"]: {"ever_ac": bool(r["ever_ac"]),
                                  "attempts": r["attempts"],
                                  "last_verdict": r["last_verdict"]}
                for r in rows}

    def wrong_problem_ids(self, user_id: str = "local") -> list[str]:
        """错题本（按用户）：提交过但从未 AC 的题。AC 即自动移出。"""
        rows = self.execute(
            "SELECT problem_id FROM submissions WHERE user_id=? GROUP BY problem_id "
            "HAVING MAX(CASE WHEN verdict='AC' THEN 1 ELSE 0 END)=0 "
            "ORDER BY RANDOM()",
            (user_id,),
        ).fetchall()
        return [r["problem_id"] for r in rows]

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

    def record_ai_judgement(
        self, problem_id: str, language: str, code: str,
        verdict: str, detail: dict, user_id: str = "local",
    ) -> int:
        cur = self.execute(
            "INSERT INTO ai_judgements(user_id, problem_id, language, code, verdict, detail, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, problem_id, language, code, verdict,
             json.dumps(detail, ensure_ascii=False), _now()),
        )
        return int(cur.lastrowid)

    # ---------- submissions ----------

    def record_submission(
        self, problem_id: str, language: str, code: str, verdict: str,
        detail: dict, runtime_ms: int, user_id: str = "local",
    ) -> int:
        cur = self.execute(
            "INSERT INTO submissions(user_id, problem_id, language, code, verdict, detail, "
            "runtime_ms, submitted_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, problem_id, language, code, verdict,
             json.dumps(detail, ensure_ascii=False), runtime_ms, _now()),
        )
        return int(cur.lastrowid)

    def get_submission(self, sid: int, user_id: str = "local") -> Optional[dict[str, Any]]:
        """按属主取提交：非本人提交返回 None（防越权枚举）。"""
        r = self.execute(
            "SELECT * FROM submissions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        if not r:
            return None
        return {
            "id": r["id"], "problem_id": r["problem_id"], "language": r["language"],
            "code": r["code"], "verdict": r["verdict"],
            "detail": json.loads(r["detail"]) if r["detail"] else {},
            "runtime_ms": r["runtime_ms"], "review": json.loads(r["review"]) if r["review"] else None,
            "submitted_at": r["submitted_at"],
        }

    def last_submission_code(self, problem_id: str, language: str = "",
                             user_id: str = "local") -> Optional[dict]:
        """取某道题的上次提交代码与语言（草稿丢失时恢复用）。
        language 为空时不限制语言，取最近一次提交。"""
        if language:
            r = self.execute(
                "SELECT code, language FROM submissions WHERE problem_id=? AND language=? AND user_id=? "
                "ORDER BY id DESC LIMIT 1", (problem_id, language, user_id)).fetchone()
        else:
            r = self.execute(
                "SELECT code, language FROM submissions WHERE problem_id=? AND user_id=? "
                "ORDER BY id DESC LIMIT 1", (problem_id, user_id)).fetchone()
        return {"code": r["code"], "language": r["language"]} if r else None

    def set_review(self, sid: int, review: dict, user_id: str = "local") -> bool:
        cur = self.execute(
            "UPDATE submissions SET reviewed=1, review=? WHERE id=? AND user_id=?",
            (json.dumps(review, ensure_ascii=False), sid, user_id),
        )
        return cur.rowcount > 0

    # ---------- stats ----------

    def stats(self, user_id: str = "local") -> dict[str, Any]:
        def one(q: str, *a: Any) -> int:
            return int(self.scalar(q, a) or 0)

        return {
            # 内容（全组共享）
            "cards": one("SELECT COUNT(*) FROM cards"),
            "sources": one("SELECT COUNT(*) FROM sources"),
            "problems": one("SELECT COUNT(*) FROM coding_problems"),
            # 个人
            "user_id": user_id,
            "learned_cards": one(
                "SELECT COUNT(*) FROM card_learn_state WHERE user_id=? AND learned=1", user_id),
            "submissions": one(
                "SELECT COUNT(*) FROM submissions WHERE user_id=?", user_id),
            "ac": one(
                "SELECT COUNT(*) FROM submissions WHERE user_id=? AND verdict='AC'", user_id),
            "quiz_attempts": one(
                "SELECT COUNT(*) FROM quiz_attempts WHERE user_id=?", user_id),
            "quiz_avg_score": self.scalar(
                "SELECT ROUND(AVG(score),1) FROM quiz_attempts "
                "WHERE user_id=? AND score IS NOT NULL", (user_id,)),
        }
