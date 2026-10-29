"""安全：密码哈希 + JWT 签发/校验。

密码：PBKDF2-HMAC-SHA256（标准库自带，每用户随机盐 + 12 万次迭代），
      存储格式 pbkdf2_sha256$迭代$salt$hash。生产可换 bcrypt/argon2，接口形状不变。
JWT ：无状态票据，claim 里带 sub(user_id) / username / role，
      角色放 token 里可以减少查库，但**授权判断仍以后端当前用户查询结果为准**
      （因为角色可能被改，见 dependencies.get_current_user）。
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 120_000
SALT_BYTES = 16


# ======================================================================
# 密码
# ======================================================================
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    """恒定时间比较，避免时序攻击。"""
    if not stored_hash:
        return False

    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$")
    except ValueError:
        logger.warning("密码哈希格式非法")
        return False

    if algorithm != PBKDF2_ALGORITHM:
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


# ======================================================================
# JWT
# ======================================================================
def create_access_token(*, user_id: int, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """校验并解析；失败抛 jwt 异常，由上层翻译成 401。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
