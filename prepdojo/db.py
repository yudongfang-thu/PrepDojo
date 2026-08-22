"""SQLite 存储层：知识题卡、代码题、提交记录、八股练习记录、用户与会话。

所有用户数据都在本地 data/prepdojo.db（.gitignore 排除）。

多用户模型（server-beta）：
- 内容数据共享：sources / cards / coding_problems / test_cases。
- 个人数据按 user_id 隔离：submissions / quiz_attempts / ai_judgements /
  card_learn_state / llm_usage。单机模式统一写 'local' 用户。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .auth import (PBKDF2_ITERATIONS, hash_password, new_session_token,
                   password_needs_rehash, verify_password)

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
  explanation TEXT,
  content_revision TEXT NOT NULL DEFAULT ''
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
  revision TEXT NOT NULL DEFAULT '',
  valid INTEGER NOT NULL DEFAULT 1,
  validation_error TEXT,
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
  problem_revision TEXT NOT NULL DEFAULT '',
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
  problem_revision TEXT NOT NULL DEFAULT '',
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
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

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


_PROBLEM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DIFFICULTIES = {"easy", "medium", "hard"}
_LANGUAGES = {"python", "cpp"}


def _validated_problem(
    p: dict[str, Any], cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """校验并规范化代码题，所有导入路径最终都经过此边界。"""
    if not isinstance(p, dict):
        raise ValueError("代码题必须是对象")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 200:
        raise ValueError("代码题测试用例数量必须为 1-200")

    def text(key: str, maximum: int) -> str:
        value = p.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"代码题字段 {key} 必须是非空字符串")
        value = value.strip()
        if len(value) > maximum:
            raise ValueError(f"代码题字段 {key} 不能超过 {maximum} 字符")
        return value

    pid = text("id", 128)
    if not _PROBLEM_ID_RE.fullmatch(pid):
        raise ValueError("代码题 id 只能包含字母、数字、点、下划线和连字符")
    title = text("title", 200)
    statement = text("statement", 100_000)
    difficulty = p.get("difficulty")
    if not isinstance(difficulty, str) or difficulty not in _DIFFICULTIES:
        raise ValueError("代码题 difficulty 必须是 easy、medium 或 hard")

    tags = p.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 20:
        raise ValueError("代码题 tags 必须是包含 1-20 项的字符串数组")
    if any(not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 50
           for tag in tags):
        raise ValueError("代码题 tags 每项必须是长度 1-50 的非空字符串")
    clean_tags = list(dict.fromkeys(tag.strip() for tag in tags))

    languages = p.get("languages", ["python", "cpp"])
    if (not isinstance(languages, list) or not languages
            or any(not isinstance(lang, str) or lang not in _LANGUAGES
                   for lang in languages)):
        raise ValueError("代码题 languages 必须是 python/cpp 的非空数组")
    clean_languages = list(dict.fromkeys(languages))

    time_limit = p.get("time_limit_ms", 5000)
    mem_limit = p.get("mem_limit_mb", 512)
    if type(time_limit) is not int or not 100 <= time_limit <= 60_000:
        raise ValueError("代码题 time_limit_ms 必须是 100-60000 的整数")
    if type(mem_limit) is not int or not 16 <= mem_limit <= 4096:
        raise ValueError("代码题 mem_limit_mb 必须是 16-4096 的整数")

    clean_cases: list[dict[str, Any]] = []
    total_chars = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"测试用例 #{index} 必须是对象")
        stdin, output = case.get("input"), case.get("output")
        if not isinstance(stdin, str) or not isinstance(output, str):
            raise ValueError(f"测试用例 #{index} 的 input/output 必须是字符串")
        if len(stdin) > 1_000_000 or len(output) > 1_000_000:
            raise ValueError(f"测试用例 #{index} 的 input/output 不能超过 1000000 字符")
        sample = case.get("sample", False)
        if type(sample) is not bool:
            raise ValueError(f"测试用例 #{index} 的 sample 必须是布尔值")
        total_chars += len(stdin) + len(output)
        clean_cases.append({"input": stdin, "output": output, "sample": sample})
    if total_chars > 8_000_000:
        raise ValueError("单道代码题的全部测试数据不能超过 8000000 字符")

    clean_problem = {
        "id": pid, "title": title, "difficulty": difficulty,
        "tags": clean_tags, "statement": statement,
        "time_limit_ms": time_limit, "mem_limit_mb": mem_limit,
        "languages": clean_languages,
    }
    # 版本只覆盖影响解答/判定的内容；改标题、难度或标签不应抹掉已通过状态。
    judged_content = {
        "statement": statement, "time_limit_ms": time_limit,
        "mem_limit_mb": mem_limit, "languages": clean_languages,
    }
    judged_cases = [{"input": case["input"], "output": case["output"]}
                    for case in clean_cases]
    canonical = json.dumps(
        {"problem": judged_content, "cases": judged_cases}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return clean_problem, clean_cases, revision


def _json_string_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else []


def _card_content_revision(
    question: Any, answer_points: Any, follow_ups: Any,
) -> str:
    canonical = json.dumps(
        {"question": question, "answer_points": answer_points,
         "follow_ups": follow_ups},
        ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(cards, list) or not cards:
        raise ValueError("来源至少需要一张题卡")
    clean: list[dict[str, Any]] = []
    refs: set[str] = set()
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            raise ValueError(f"题卡 #{index} 必须是对象")
        question = card.get("question")
        source_ref = card.get("source_ref")
        if not isinstance(question, str) or not question.strip() or len(question.strip()) > 2000:
            raise ValueError(f"题卡 #{index} 的 question 必须是长度 1-2000 的字符串")
        if (not isinstance(source_ref, str) or not source_ref.strip()
                or len(source_ref.strip()) > 4096):
            raise ValueError(f"题卡 #{index} 的 source_ref 必须是非空字符串")
        source_ref = source_ref.strip()
        if source_ref in refs:
            raise ValueError(f"题卡 source_ref 重复: {source_ref}")
        refs.add(source_ref)

        def values(key: str, low: int, high: int, item_max: int) -> list[str]:
            raw = card.get(key)
            if not isinstance(raw, list) or not low <= len(raw) <= high:
                raise ValueError(f"题卡 #{index} 的 {key} 数量必须为 {low}-{high}")
            if any(not isinstance(item, str) or not item.strip()
                   or len(item.strip()) > item_max for item in raw):
                raise ValueError(f"题卡 #{index} 的 {key} 含非法项")
            return [item.strip() for item in raw]

        difficulty = card.get("difficulty")
        if type(difficulty) is not int or not 1 <= difficulty <= 3:
            raise ValueError(f"题卡 #{index} 的 difficulty 必须是 1-3 的整数")
        clean_points = values("answer_points", 1, 8, 2000)
        clean_follow_ups = values("follow_ups", 0, 3, 1000)
        clean.append({
            "question": question.strip(),
            "answer_points": clean_points,
            "follow_ups": clean_follow_ups,
            "topic_tags": values("topic_tags", 1, 4, 100),
            "difficulty": difficulty,
            "source_ref": source_ref,
            "content_revision": _card_content_revision(
                question.strip(), clean_points, clean_follow_ups),
        })
    return clean


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 同步端点跑在线程池：允许跨线程使用，用锁串行化（小规模多人足够）
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
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
            ("content_revision", "ALTER TABLE cards ADD COLUMN content_revision "
                                 "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in cols:
                self.conn.execute(ddl)
        card_rows = self.conn.execute(
            "SELECT id, question, answer_points, follow_ups, content_revision FROM cards"
        ).fetchall()
        for row in card_rows:
            if not row["content_revision"]:
                revision = _card_content_revision(
                    row["question"], _json_string_list(row["answer_points"]),
                    _json_string_list(row["follow_ups"]))
                self.conn.execute(
                    "UPDATE cards SET content_revision=? WHERE id=?",
                    (revision, row["id"]))
        # 单用户旧库 → 多用户：补 user_id 列并回填 'local'
        for table in ("submissions", "quiz_attempts", "ai_judgements"):
            tcols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in tcols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'")
            if table in {"submissions", "ai_judgements"} and "problem_revision" not in tcols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN problem_revision "
                    "TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                f"UPDATE {table} SET user_id='local' WHERE user_id IS NULL OR user_id=''")
        problem_cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(coding_problems)")}
        if "revision" not in problem_cols:
            self.conn.execute(
                "ALTER TABLE coding_problems ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
        if "valid" not in problem_cols:
            self.conn.execute(
                "ALTER TABLE coding_problems ADD COLUMN valid INTEGER NOT NULL DEFAULT 1")
        if "validation_error" not in problem_cols:
            self.conn.execute(
                "ALTER TABLE coding_problems ADD COLUMN validation_error TEXT")
        for row in self.conn.execute("SELECT * FROM coding_problems").fetchall():
            case_rows = self.conn.execute(
                "SELECT input, expected_output, is_sample FROM test_cases "
                "WHERE problem_id=? ORDER BY idx", (row["id"],)).fetchall()
            try:
                _, _, revision = _validated_problem(
                    {"id": row["id"], "title": row["title"],
                     "difficulty": row["difficulty"], "tags": json.loads(row["tags"]),
                     "statement": row["statement"],
                     "time_limit_ms": row["time_limit_ms"],
                     "mem_limit_mb": row["mem_limit_mb"],
                     "languages": json.loads(row["languages"])},
                    [{"input": case["input"], "output": case["expected_output"],
                      "sample": bool(case["is_sample"])} for case in case_rows],
                )
                valid, validation_error = 1, None
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                # 旧版本曾允许更宽松的数据。升级时不删除也不阻断整个服务，
                # 将原始内容稳定哈希并隔离该题，等待管理员修复/重新导入。
                raw = {
                    "id": row["id"], "title": row["title"],
                    "difficulty": row["difficulty"], "tags": row["tags"],
                    "statement": row["statement"],
                    "time_limit_ms": row["time_limit_ms"],
                    "mem_limit_mb": row["mem_limit_mb"],
                    "languages": row["languages"],
                    "cases": [(case["input"], case["expected_output"], case["is_sample"])
                              for case in case_rows],
                }
                revision = hashlib.sha256(json.dumps(
                    raw, ensure_ascii=False, sort_keys=True, default=str,
                    separators=(",", ":")).encode("utf-8")).hexdigest()
                valid, validation_error = 0, str(exc)[:500]
            stored_revision = row["revision"] or revision
            self.conn.execute(
                "UPDATE coding_problems SET revision=?, valid=?, validation_error=? WHERE id=?",
                (stored_revision, valid, validation_error, row["id"]))
        # 旧库没有版本信息；首次升级时将历史提交归入当时的当前题目版本。
        self.conn.execute(
            "UPDATE submissions SET problem_revision=COALESCE((SELECT revision "
            "FROM coding_problems p WHERE p.id=submissions.problem_id), '') "
            "WHERE problem_revision='' OR problem_revision IS NULL")
        self.conn.execute(
            "UPDATE ai_judgements SET problem_revision=COALESCE((SELECT revision "
            "FROM coding_problems p WHERE p.id=ai_judgements.problem_id), '') "
            "WHERE problem_revision='' OR problem_revision IS NULL")
        # user_id 索引在补列之后再建（旧库首次升级时列尚不存在）
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_submissions_current "
            "ON submissions(user_id, problem_id, problem_revision)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_user ON quiz_attempts(user_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        # 旧 cards.learned 状态迁到 card_learn_state（归属 local 用户）
        self.conn.execute(
            "INSERT OR IGNORE INTO card_learn_state(user_id, card_id, learned, learned_at) "
            "SELECT 'local', id, learned, learned_at FROM cards WHERE learned=1")
        # 用户名同时被用作个人数据的稳定属主。删除用户时保留 tombstone，禁止同名
        # 账号复活后继承旧提交；local 是单机模式保留身份，不能注册成真实账号。
        user_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "disabled" not in user_cols:
            self.conn.execute(
                "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "UPDATE users SET disabled=1, password_hash='', salt='', api_key=NULL "
            "WHERE lower(username)='local'")
        self.conn.execute(
            "DELETE FROM sessions WHERE lower(username)='local'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """线程安全的执行入口（普通写操作自动提交，失败自动回滚）。"""
        with self._lock:
            head = sql.lstrip().upper()
            try:
                cur = self.conn.execute(sql, params)
                if not head.startswith(("SELECT", "PRAGMA", "EXPLAIN")):
                    self.conn.commit()
                return cur
            except Exception:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise

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
        if (not username or len(username) > 64 or username.casefold() == "local"
                or any(c.isspace() or c in "/\\'\"" or ord(c) < 32 for c in username)):
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
        r = self.execute(
            "SELECT * FROM users WHERE username=? AND disabled=0", (username,)).fetchone()
        return self._user_row(r) if r else None

    def _user_row(self, r: sqlite3.Row, *, include_api_key: bool = True) -> dict[str, Any]:
        row = {
            "username": r["username"],
            "is_admin": bool(r["is_admin"]),
            "has_api_key": bool(r["api_key"]),
            "has_password": bool(r["password_hash"]),
            "created_at": r["created_at"],
        }
        if include_api_key:
            row["api_key"] = r["api_key"]
        return row

    def verify_login(self, username: str, password: str) -> Optional[dict[str, Any]]:
        r = self.execute(
            "SELECT * FROM users WHERE username=? AND disabled=0",
            (username.strip(),)).fetchone()
        if not r:
            # 恒定时间比较防时序探测（不存在用户也做一次哈希）
            verify_password(
                password, f"pbkdf2_sha256${PBKDF2_ITERATIONS}${'00' * 32}",
                "00" * 16)
            return None
        if not verify_password(password, r["password_hash"], r["salt"]):
            return None
        if password_needs_rehash(r["password_hash"]):
            new_hash, new_salt = hash_password(password)
            # 仅当校验期间密码未被重置时升级；否则本次旧凭据认证作废。
            cur = self.execute(
                "UPDATE users SET password_hash=?, salt=? WHERE username=? "
                "AND password_hash=? AND salt=? AND disabled=0",
                (new_hash, new_salt, r["username"],
                 r["password_hash"], r["salt"]))
            if cur.rowcount != 1:
                return None
        return self._user_row(r)

    def authenticate_and_create_session(
        self, username: str, password: str, days: int = 30,
    ) -> Optional[tuple[dict[str, Any], str]]:
        """校验密码并原子创建会话，阻断密码重置与登录之间的竞态。"""
        username = username.strip()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE username=? AND disabled=0",
                (username,)).fetchone()
        if not row:
            verify_password(
                password, f"pbkdf2_sha256${PBKDF2_ITERATIONS}${'00' * 32}",
                "00" * 16)
            return None
        if not verify_password(password, row["password_hash"], row["salt"]):
            return None

        replacement = hash_password(password) if password_needs_rehash(
            row["password_hash"]) else None
        token = new_session_token()
        now = _now()
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
            timespec="seconds")
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                current = self.conn.execute(
                    "SELECT * FROM users WHERE username=? AND password_hash=? "
                    "AND salt=? AND disabled=0",
                    (username, row["password_hash"], row["salt"])).fetchone()
                if not current:
                    self.conn.rollback()
                    return None
                if replacement:
                    self.conn.execute(
                        "UPDATE users SET password_hash=?, salt=? WHERE username=?",
                        (replacement[0], replacement[1], username))
                self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
                self.conn.execute(
                    "INSERT INTO sessions(token, username, created_at, expires_at) "
                    "VALUES(?,?,?,?)", (token, username, now, expires))
                self.conn.commit()
                return self._user_row(current), token
            except Exception:
                self.conn.rollback()
                raise

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM users WHERE disabled=0 ORDER BY created_at").fetchall()
        # 管理列表只暴露是否配置密钥，绝不返回密钥明文。
        return [self._user_row(r, include_api_key=False) for r in rows]

    def delete_user(self, username: str) -> bool:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                cur = self.conn.execute(
                    "UPDATE users SET disabled=1, password_hash='', salt='', api_key=NULL "
                    "WHERE username=? AND disabled=0", (username,))
                self.conn.execute("DELETE FROM sessions WHERE username=?", (username,))
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def set_user_password(self, username: str, password: str) -> bool:
        h, salt = hash_password(password)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                cur = self.conn.execute(
                    "UPDATE users SET password_hash=?, salt=? "
                    "WHERE username=? AND disabled=0", (h, salt, username))
                # 修改密码立即撤销所有已有会话，避免失窃 cookie 继续有效。
                self.conn.execute("DELETE FROM sessions WHERE username=?", (username,))
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def set_user_api_key(self, username: str, api_key: Optional[str]) -> bool:
        cur = self.execute(
            "UPDATE users SET api_key=? WHERE username=? AND disabled=0",
            (api_key or None, username))
        return cur.rowcount > 0

    def create_session(self, username: str, days: int = 30) -> str:
        if not self.scalar(
                "SELECT 1 FROM users WHERE username=? AND disabled=0", (username,)):
            raise ValueError("用户不存在或已停用")
        token = new_session_token()
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        # 登录本身已限流且会写库，在此低频清理过期会话。
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        self.execute(
            "INSERT INTO sessions(token, username, created_at, expires_at) VALUES(?,?,?,?)",
            (token, username, _now(), expires))
        return token

    def session_user(self, token: str) -> Optional[dict[str, Any]]:
        """有效会话返回用户；未认证请求保持纯只读。"""
        r = self.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.username=s.username "
            "WHERE s.token=? AND s.expires_at>=? AND u.disabled=0",
            (token, _now())).fetchone()
        return self._user_row(r) if r else None

    def delete_session(self, token: str) -> bool:
        with self._lock:
            if not self.conn.execute(
                    "SELECT 1 FROM sessions WHERE token=?", (token,)).fetchone():
                return False
            cur = self.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self.conn.commit()
            return cur.rowcount > 0

    # ---------- LLM 用量 ----------

    def llm_usage_today(self, user_id: str) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        return int(self.scalar(
            "SELECT count FROM llm_usage WHERE user_id=? AND day=?", (user_id, day)) or 0)

    def bump_llm_usage(self, user_id: str) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "INSERT INTO llm_usage(user_id, day, count) VALUES(?,?,1) "
                    "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
                    (user_id, day))
                row = self.conn.execute(
                    "SELECT count FROM llm_usage WHERE user_id=? AND day=?",
                    (user_id, day)).fetchone()
                self.conn.commit()
                return int(row["count"])
            except Exception:
                self.conn.rollback()
                raise

    def consume_llm_quota(
        self, user_id: str, user_limit: int = 0, global_limit: int = 0,
    ) -> dict[str, Any]:
        """原子检查并消费一次真实 LLM 请求配额。

        返回 ``ok/scope/count/limit``；并发请求不能越过个人或全局上限。
        ``0`` 表示对应维度不限额。
        """
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT count FROM llm_usage WHERE user_id=? AND day=?",
                    (user_id, day)).fetchone()
                user_count = int(row[0]) if row else 0
                global_count = int(self.conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM llm_usage WHERE day=?",
                    (day,)).fetchone()[0])
                if user_limit > 0 and user_count >= user_limit:
                    self.conn.rollback()
                    return {"ok": False, "scope": "user", "count": user_count,
                            "limit": user_limit}
                if global_limit > 0 and global_count >= global_limit:
                    self.conn.rollback()
                    return {"ok": False, "scope": "global", "count": global_count,
                            "limit": global_limit}
                self.conn.execute(
                    "INSERT INTO llm_usage(user_id, day, count) VALUES(?,?,1) "
                    "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
                    (user_id, day))
                self.conn.commit()
                return {"ok": True, "scope": "", "count": user_count + 1,
                        "limit": user_limit}
            except Exception:
                self.conn.rollback()
                raise

    # ---------- sources / cards ----------

    def upsert_source(self, path: str, sha256: str, title: str) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT INTO sources(path, sha256, title, n_cards, ingested_at) "
                "VALUES(?,?,?,0,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
                "title=excluded.title, ingested_at=excluded.ingested_at",
                (path, sha256, title, _now()),
            )
            row = self.conn.execute(
                "SELECT id FROM sources WHERE path=?", (path,)).fetchone()
            self.conn.commit()
            return int(row["id"])

    def replace_source_cards(
        self, path: str, sha256: str, title: str, cards: list[dict[str, Any]],
    ) -> list[str]:
        """原子同步来源题卡；相同 source_ref 复用 ID 与全体用户学习状态。"""
        clean_cards = _validated_cards(cards)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "INSERT INTO sources(path, sha256, title, n_cards, ingested_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                    "sha256=excluded.sha256, title=excluded.title, "
                    "n_cards=excluded.n_cards, ingested_at=excluded.ingested_at",
                    (path, sha256, title, len(clean_cards), _now()))
                source_id = int(self.conn.execute(
                    "SELECT id FROM sources WHERE path=?", (path,)).fetchone()[0])
                old_rows = self.conn.execute(
                    "SELECT id, source_ref, answer_points FROM cards WHERE source_id=?",
                    (source_id,)).fetchall()
                old_by_ref = {row["source_ref"]: row["id"] for row in old_rows}
                card_ids = [old_by_ref.get(card["source_ref"])
                            or "kc-" + uuid.uuid4().hex[:12] for card in clean_cards]
                kept = set(card_ids)
                removed = [row["id"] for row in old_rows if row["id"] not in kept]
                reset_progress = [
                    old_by_ref[card["source_ref"]]
                    for card in clean_cards if card["source_ref"] in old_by_ref
                    and next(row["answer_points"] for row in old_rows
                             if row["id"] == old_by_ref[card["source_ref"]])
                    != json.dumps(card["answer_points"], ensure_ascii=False)
                ]
                states_to_remove = list(dict.fromkeys([*removed, *reset_progress]))
                if states_to_remove:
                    state_marks = ",".join("?" for _ in states_to_remove)
                    self.conn.execute(
                        f"DELETE FROM card_learn_state WHERE card_id IN ({state_marks})",
                        states_to_remove)
                if removed:
                    marks = ",".join("?" for _ in removed)
                    self.conn.execute(
                        f"DELETE FROM cards WHERE id IN ({marks})", removed)
                for cid, card in zip(card_ids, clean_cards):
                    self.conn.execute(
                        "INSERT INTO cards(id, question, answer_points, follow_ups, "
                        "topic_tags, difficulty, source_id, source_ref, created_at, "
                        "content_revision) VALUES(?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "question=excluded.question, answer_points=excluded.answer_points, "
                        "follow_ups=excluded.follow_ups, topic_tags=excluded.topic_tags, "
                        "difficulty=excluded.difficulty, source_id=excluded.source_id, "
                        "source_ref=excluded.source_ref, explanation=CASE WHEN "
                        "cards.content_revision=excluded.content_revision "
                        "THEN cards.explanation ELSE NULL END, "
                        "content_revision=excluded.content_revision",
                        (cid, card["question"],
                         json.dumps(card["answer_points"], ensure_ascii=False),
                         json.dumps(card["follow_ups"], ensure_ascii=False),
                         json.dumps(card["topic_tags"], ensure_ascii=False),
                         card["difficulty"], source_id, card["source_ref"], _now(),
                         card["content_revision"]))
                self.conn.commit()
                return card_ids
            except Exception:
                self.conn.rollback()
                raise

    def source_sha(self, path: str) -> Optional[str]:
        row = self.execute("SELECT sha256 FROM sources WHERE path=?", (path,)).fetchone()
        return row["sha256"] if row else None

    def update_source_count(self, source_id: int) -> None:
        self.execute(
            "UPDATE sources SET n_cards=(SELECT COUNT(*) FROM cards WHERE source_id=?) WHERE id=?",
            (source_id, source_id),
        )

    def delete_source(self, source_id: int) -> bool:
        """原子删除来源及其当前题卡/学习状态；历史作答快照保留。"""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                card_ids = [r[0] for r in self.conn.execute(
                    "SELECT id FROM cards WHERE source_id=?", (source_id,)).fetchall()]
                if card_ids:
                    marks = ",".join("?" for _ in card_ids)
                    self.conn.execute(
                        f"DELETE FROM card_learn_state WHERE card_id IN ({marks})", card_ids)
                self.conn.execute("DELETE FROM cards WHERE source_id=?", (source_id,))
                cur = self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

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
        content_revision = _card_content_revision(question, answer_points, follow_ups)
        self.execute(
            "INSERT INTO cards(id, question, answer_points, follow_ups, topic_tags, difficulty, "
            "source_id, source_ref, created_at, content_revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                content_revision,
            ),
        )
        return cid

    def get_card(self, card_id: str, user_id: str = "local") -> Optional[dict[str, Any]]:
        row = self.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            return None
        state = self.execute(
            "SELECT learned, learned_at FROM card_learn_state WHERE user_id=? AND card_id=?",
            (user_id, card_id)).fetchone()
        return self._card_row(
            row, learned=bool(state and state["learned"]),
            learned_at=state["learned_at"] if state and state["learned"] else None)

    def _card_row(
        self, row: sqlite3.Row, learned: bool = False,
        learned_at: Optional[str] = None,
    ) -> dict[str, Any]:
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
            "learned_at": learned_at,
            "explanation": row["explanation"],
            "content_revision": row["content_revision"],
        }

    def _learned_map(self, user_id: str, card_ids: list[str]) -> dict[str, Optional[str]]:
        if not card_ids:
            return {}
        marks = ",".join("?" for _ in card_ids)
        rows = self.execute(
            f"SELECT card_id, learned_at FROM card_learn_state "
            f"WHERE user_id=? AND card_id IN ({marks}) AND learned=1",
            [user_id, *card_ids]).fetchall()
        return {r["card_id"]: r["learned_at"] for r in rows}

    def pick_cards(
        self, user_id: str = "local", tags: Optional[list[str]] = None, n: int = 10,
        exclude_seen_days: int = 3, difficulty: Optional[int] = None,
        only_learned: bool = False,
    ) -> list[dict[str, Any]]:
        """测验抽题：排除该用户最近 N 天练过的卡；only_learned 时仅从其已学卡抽。"""
        q = (
            "SELECT c.* FROM cards c WHERE c.id NOT IN ("
            "  SELECT card_id FROM quiz_attempts"
            "  WHERE user_id=? AND datetime(asked_at) >= datetime('now', ?)"
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
        return [self._card_row(
            r, r["id"] in learned, learned.get(r["id"])) for r in rows]

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
        return [self._card_row(
            r, r["id"] in learned, learned.get(r["id"])) for r in rows]

    def mark_learned(self, card_id: str, learned: bool, user_id: str = "local") -> bool:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                if not self.conn.execute(
                        "SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone():
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "INSERT INTO card_learn_state(user_id, card_id, learned, learned_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(user_id, card_id) DO UPDATE SET "
                    "learned=excluded.learned, learned_at=excluded.learned_at",
                    (user_id, card_id, 1 if learned else 0, _now() if learned else None))
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def set_explanation(
        self, card_id: str, explanation: str,
        expected_revision: Optional[str] = None,
    ) -> bool:
        if expected_revision is None:
            cur = self.execute(
                "UPDATE cards SET explanation=? WHERE id=?", (explanation, card_id))
        else:
            cur = self.execute(
                "UPDATE cards SET explanation=? WHERE id=? AND content_revision=?",
                (explanation, card_id, expected_revision))
        return cur.rowcount > 0

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
        clean, clean_cases, revision = _validated_problem(p, cases)
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "INSERT INTO coding_problems(id, title, difficulty, tags, statement, time_limit_ms, "
                    "mem_limit_mb, languages, revision, created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET title=excluded.title, difficulty=excluded.difficulty, "
                    "tags=excluded.tags, statement=excluded.statement, "
                    "time_limit_ms=excluded.time_limit_ms, mem_limit_mb=excluded.mem_limit_mb, "
                    "languages=excluded.languages, revision=excluded.revision, "
                    "valid=1, validation_error=NULL",
                    (clean["id"], clean["title"], clean["difficulty"],
                     json.dumps(clean["tags"], ensure_ascii=False), clean["statement"],
                     clean["time_limit_ms"], clean["mem_limit_mb"],
                     json.dumps(clean["languages"]), revision, _now()))
                self.conn.execute(
                    "DELETE FROM test_cases WHERE problem_id=?", (clean["id"],))
                for i, c in enumerate(clean_cases):
                    self.conn.execute(
                        "INSERT INTO test_cases(problem_id, idx, input, expected_output, is_sample) "
                        "VALUES(?,?,?,?,?)",
                        (clean["id"], i, c["input"], c["output"],
                         1 if c.get("sample") else 0))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def list_problems(self, user_id: str = "local") -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM test_cases t WHERE t.problem_id=p.id) AS n_cases "
            "FROM coding_problems p ORDER BY p.id"
        ).fetchall()
        status = self.problem_status_map(user_id)
        return [
            {
                "id": r["id"], "title": r["title"], "difficulty": r["difficulty"],
                "tags": _json_string_list(r["tags"]), "n_cases": r["n_cases"],
                "valid": bool(r["valid"]),
                "validation_error": r["validation_error"],
                **status.get(r["id"], {"ever_ac": False, "attempts": 0}),
            }
            for r in rows
        ]

    def problem_status_map(self, user_id: str = "local") -> dict[str, dict[str, Any]]:
        """当前题目版本的提交状态（按用户）：ever_ac / attempts / last_verdict。"""
        rows = self.execute(
            "SELECT s1.problem_id,"
            " MAX(CASE WHEN verdict='AC' THEN 1 ELSE 0 END) AS ever_ac,"
            " COUNT(*) AS attempts,"
            " (SELECT verdict FROM submissions s2"
            "  WHERE s2.problem_id=s1.problem_id AND s2.user_id=s1.user_id "
            "    AND s2.problem_revision=p.revision"
            "  ORDER BY s2.id DESC LIMIT 1) AS last_verdict"
            " FROM submissions s1 JOIN coding_problems p"
            " ON p.id=s1.problem_id AND p.revision=s1.problem_revision AND p.valid=1"
            " WHERE s1.user_id=? GROUP BY s1.problem_id",
            (user_id,),
        ).fetchall()
        return {r["problem_id"]: {"ever_ac": bool(r["ever_ac"]),
                                  "attempts": r["attempts"],
                                  "last_verdict": r["last_verdict"]}
                for r in rows}

    def wrong_problem_ids(self, user_id: str = "local") -> list[str]:
        """当前题目版本的错题本；旧版本结果不污染更新后的题目。"""
        rows = self.execute(
            "SELECT s.problem_id FROM submissions s JOIN coding_problems p "
            "ON p.id=s.problem_id AND p.revision=s.problem_revision AND p.valid=1 "
            "WHERE s.user_id=? GROUP BY s.problem_id "
            "HAVING MAX(CASE WHEN verdict='AC' THEN 1 ELSE 0 END)=0 "
            "ORDER BY RANDOM()",
            (user_id,),
        ).fetchall()
        return [r["problem_id"] for r in rows]

    @staticmethod
    def _problem_payload(r: sqlite3.Row, cases: list[sqlite3.Row]) -> dict[str, Any]:
        return {
            "id": r["id"], "title": r["title"], "difficulty": r["difficulty"],
            "tags": _json_string_list(r["tags"]), "statement": r["statement"],
            "time_limit_ms": r["time_limit_ms"], "mem_limit_mb": r["mem_limit_mb"],
            "languages": _json_string_list(r["languages"]),
            "revision": r["revision"],
            "valid": bool(r["valid"]),
            "validation_error": r["validation_error"],
            "samples": [
                {"input": c["input"], "output": c["expected_output"]}
                for c in cases if c["is_sample"]
            ],
            "n_cases": len(cases),
        }

    def get_problem_snapshot(
        self, pid: str,
    ) -> Optional[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """在同一把锁内取得题目版本与全部用例，避免更新期间混合两个版本。"""
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM coding_problems WHERE id=?", (pid,)).fetchone()
            if not r:
                return None
            rows = self.conn.execute(
                "SELECT idx, input, expected_output, is_sample FROM test_cases "
                "WHERE problem_id=? ORDER BY idx", (pid,)).fetchall()
            problem = self._problem_payload(r, rows)
            cases = [{"input": row["input"], "output": row["expected_output"],
                      "sample": bool(row["is_sample"])} for row in rows]
            return problem, cases

    def get_problem(self, pid: str) -> Optional[dict[str, Any]]:
        snapshot = self.get_problem_snapshot(pid)
        return snapshot[0] if snapshot else None

    def delete_problem(self, pid: str) -> bool:
        """原子删除共享题面和用例；个人提交历史保留用于审计。"""
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("DELETE FROM test_cases WHERE problem_id=?", (pid,))
                cur = self.conn.execute("DELETE FROM coding_problems WHERE id=?", (pid,))
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def record_ai_judgement(
        self, problem_id: str, language: str, code: str,
        verdict: str, detail: dict, user_id: str = "local",
        problem_revision: Optional[str] = None,
    ) -> int:
        with self._lock:
            try:
                if problem_revision is None:
                    row = self.conn.execute(
                        "SELECT revision FROM coding_problems WHERE id=?", (problem_id,)).fetchone()
                    if not row:
                        raise ValueError(f"代码题不存在: {problem_id}")
                    problem_revision = row["revision"]
                cur = self.conn.execute(
                    "INSERT INTO ai_judgements(user_id, problem_id, problem_revision, language, "
                    "code, verdict, detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id, problem_id, problem_revision, language, code, verdict,
                     json.dumps(detail, ensure_ascii=False), _now()))
                self.conn.commit()
                return int(cur.lastrowid)
            except Exception:
                self.conn.rollback()
                raise

    # ---------- submissions ----------

    def record_submission(
        self, problem_id: str, language: str, code: str, verdict: str,
        detail: dict, runtime_ms: int, user_id: str = "local",
        problem_revision: Optional[str] = None,
    ) -> int:
        with self._lock:
            try:
                if problem_revision is None:
                    row = self.conn.execute(
                        "SELECT revision FROM coding_problems WHERE id=?",
                        (problem_id,)).fetchone()
                    if not row:
                        raise ValueError(f"代码题不存在: {problem_id}")
                    problem_revision = row["revision"]
                cur = self.conn.execute(
                    "INSERT INTO submissions(user_id, problem_id, problem_revision, language, "
                    "code, verdict, detail, runtime_ms, submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (user_id, problem_id, problem_revision, language, code, verdict,
                     json.dumps(detail, ensure_ascii=False), runtime_ms, _now()))
                self.conn.commit()
                return int(cur.lastrowid)
            except Exception:
                self.conn.rollback()
                raise

    def get_submission(self, sid: int, user_id: str = "local") -> Optional[dict[str, Any]]:
        """按属主取提交：非本人提交返回 None（防越权枚举）。"""
        r = self.execute(
            "SELECT * FROM submissions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        if not r:
            return None
        return {
            "id": r["id"], "problem_id": r["problem_id"], "language": r["language"],
            "problem_revision": r["problem_revision"],
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
                "SELECT s.code, s.language FROM submissions s JOIN coding_problems p "
                "ON p.id=s.problem_id AND p.revision=s.problem_revision AND p.valid=1 "
                "WHERE s.problem_id=? AND s.language=? AND s.user_id=? "
                "ORDER BY s.id DESC LIMIT 1", (problem_id, language, user_id)).fetchone()
        else:
            r = self.execute(
                "SELECT s.code, s.language FROM submissions s JOIN coding_problems p "
                "ON p.id=s.problem_id AND p.revision=s.problem_revision AND p.valid=1 "
                "WHERE s.problem_id=? AND s.user_id=? "
                "ORDER BY s.id DESC LIMIT 1", (problem_id, user_id)).fetchone()
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
