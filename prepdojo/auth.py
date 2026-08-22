"""多用户认证：密码哈希（PBKDF2）+ 服务端会话。

单机模式（multiuser=False）不经过这里：所有请求视为内置 local 用户。
会话 token 存 SQLite（sessions 表），可随时吊销，无需签名密钥。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 120_000
_HASH_PREFIX = "pbkdf2_sha256"
SESSION_COOKIE = "prepdojo_session"
SESSION_DAYS = 30

# 单机模式的虚拟用户：不进 users 表，仅作为数据归属标签与放行标志
LOCAL_USER = {"username": "local", "is_admin": True, "api_key": None}


def hash_password(password: str) -> tuple[str, str]:
    """返回带算法/工作因子的 ``(hash, salt_hex)``。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"{_HASH_PREFIX}${PBKDF2_ITERATIONS}${digest.hex()}", salt


def _password_hash_parts(stored_hash: str) -> tuple[int, str] | None:
    """解析当前格式，并兼容旧版仅保存 64 位十六进制摘要的记录。"""
    if not stored_hash:
        return None
    if "$" not in stored_hash:
        if len(stored_hash) == 64:
            try:
                bytes.fromhex(stored_hash)
            except ValueError:
                return None
            return LEGACY_PBKDF2_ITERATIONS, stored_hash
        return None
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != _HASH_PREFIX:
        return None
    try:
        iterations = int(parts[1])
        bytes.fromhex(parts[2])
    except ValueError:
        return None
    # 数据库内容损坏时也不能让一次登录触发近乎无限的 CPU 消耗。
    if not 1 <= iterations <= 2_000_000 or len(parts[2]) != 64:
        return None
    return iterations, parts[2]


def password_needs_rehash(stored_hash: str) -> bool:
    parts = _password_hash_parts(stored_hash)
    return parts is None or parts[0] < PBKDF2_ITERATIONS


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    parts = _password_hash_parts(hash_hex)
    if parts is None or not salt_hex:
        return False
    iterations, expected_hex = parts
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), iterations)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
