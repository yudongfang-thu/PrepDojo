"""密码哈希升级与登录会话原子性。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prepdojo import db as db_module  # noqa: E402
from prepdojo.auth import (  # noqa: E402
    LEGACY_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from prepdojo.db import DB  # noqa: E402


def test_versioned_password_hash_and_legacy_compatibility():
    current, salt = hash_password("correct horse battery staple")
    assert current.startswith(f"pbkdf2_sha256${PBKDF2_ITERATIONS}$")
    assert verify_password("correct horse battery staple", current, salt)
    assert not password_needs_rehash(current)

    legacy_salt = "11" * 16
    legacy = hashlib.pbkdf2_hmac(
        "sha256", b"old-password", bytes.fromhex(legacy_salt),
        LEGACY_PBKDF2_ITERATIONS).hex()
    assert verify_password("old-password", legacy, legacy_salt)
    assert password_needs_rehash(legacy)


def test_legacy_hash_is_upgraded_on_login(tmp_path):
    db = DB(tmp_path / "auth.db")
    salt = "22" * 16
    legacy = hashlib.pbkdf2_hmac(
        "sha256", b"old-password", bytes.fromhex(salt),
        LEGACY_PBKDF2_ITERATIONS).hex()
    db.execute(
        "INSERT INTO users(username, password_hash, salt, created_at) VALUES(?,?,?,?)",
        ("legacy", legacy, salt, "2026-01-01T00:00:00+00:00"))

    authenticated = db.authenticate_and_create_session("legacy", "old-password")
    assert authenticated is not None
    user, token = authenticated
    assert user["username"] == "legacy"
    assert db.session_user(token)["username"] == "legacy"
    stored = db.execute(
        "SELECT password_hash FROM users WHERE username='legacy'").fetchone()[0]
    assert stored.startswith(f"pbkdf2_sha256${PBKDF2_ITERATIONS}$")


def test_password_reset_race_cannot_create_fresh_session(tmp_path, monkeypatch):
    db = DB(tmp_path / "race.db")
    assert db.create_user("alice", "old-password")
    original_verify = db_module.verify_password

    def reset_after_verification(password, stored_hash, salt):
        result = original_verify(password, stored_hash, salt)
        db.set_user_password("alice", "new-password")
        return result

    monkeypatch.setattr(db_module, "verify_password", reset_after_verification)
    assert db.authenticate_and_create_session("alice", "old-password") is None
    assert db.scalar("SELECT COUNT(*) FROM sessions WHERE username='alice'") == 0
