"""Password hashing and JWT issuing/verification."""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    MAX_PASSWORD_LEN,
    MIN_PASSWORD_LEN,
    SECRET,
)

log = logging.getLogger("kedr.security")

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# bcrypt only consumes the first 72 bytes of a password: reject longer input
# instead of silently truncating it.
BCRYPT_MAX_BYTES = 72


class PasswordPolicyError(ValueError):
    """Raised with a user-facing (Russian) explanation."""


def validate_password(password: str) -> None:
    """Enforce the password policy shared by registration and password change."""
    if not isinstance(password, str):
        raise PasswordPolicyError("Пароль должен быть строкой")
    if len(password) < MIN_PASSWORD_LEN:
        raise PasswordPolicyError(f"Пароль: минимум {MIN_PASSWORD_LEN} символов")
    if len(password) > MAX_PASSWORD_LEN:
        raise PasswordPolicyError(f"Пароль: максимум {MAX_PASSWORD_LEN} символов")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise PasswordPolicyError(f"Пароль слишком длинный: не более {BCRYPT_MAX_BYTES} байт")
    if not re.search(r"[A-Za-zА-Яа-яЁё]", password):
        raise PasswordPolicyError("Пароль должен содержать хотя бы одну букву")
    if not re.search(r"\d", password):
        raise PasswordPolicyError("Пароль должен содержать хотя бы одну цифру")


def hash_password(password: str) -> str:
    validate_password(password)
    return pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return pwd.verify(plain, hashed)
    except Exception as exc:  # malformed hash in the DB must not crash login
        log.warning("password verification failed: %s", exc)
        return False


# ------------------------------------------------------------------- tokens
def create_access_token(user_id: int, token_version: int = 0, expires_minutes: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    minutes = ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    payload = {
        "sub": str(user_id),
        "ver": int(token_version),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": now + timedelta(minutes=minutes),
        "jti": secrets.token_urlsafe(12),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Return the claims, or None when the token is invalid/expired/wrong audience."""
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGORITHM], audience=JWT_AUDIENCE, issuer=JWT_ISSUER)
    except JWTError:
        return None


def user_id_from_token(token: str) -> tuple[int | None, int]:
    claims = decode_token(token)
    if not claims:
        return None, -1
    try:
        return int(claims["sub"]), int(claims.get("ver", 0))
    except (KeyError, TypeError, ValueError):
        return None, -1


# --------------------------------------------------------------- media URLs
def _sign(raw: str) -> str:
    import hashlib
    import hmac

    return hmac.new(SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def sign_media_token(rel_path: str, ttl_seconds: int = 3600) -> str:
    """Signed, expiring token for a media path (keeps uploads private)."""
    exp = int(time.time()) + max(30, int(ttl_seconds))
    return f"{exp}.{_sign(f'{rel_path}|{exp}')}"


def verify_media_token(rel_path: str, token: str) -> bool:
    import hmac

    if not token or "." not in token:
        return False
    exp_raw, sig = token.split(".", 1)
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sign(f"{rel_path}|{exp}"))
