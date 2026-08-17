"""多用户认证：密码哈希（PBKDF2）+ 服务端会话。

单机模式（multiuser=False）不经过这里：所有请求视为内置 local 用户。
会话 token 存 SQLite（sessions 表），可随时吊销，无需签名密钥。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 120_000
SESSION_COOKIE = "prepdojo_session"
SESSION_DAYS = 30

# 单机模式的虚拟用户：不进 users 表，仅作为数据归属标签与放行标志
LOCAL_USER = {"username": "local", "is_admin": True, "api_key": None}


def hash_password(password: str) -> tuple[str, str]:
    """返回 (hash_hex, salt_hex)。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    if not hash_hex or not salt_hex:
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
