"""Password hashing (stdlib pbkdf2, no extra dependency) and session-cookie helpers."""

import hashlib
import hmac
import os
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer

PBKDF2_ITERATIONS = 260_000
SESSION_COOKIE = "sb_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
_SECRET = os.getenv("STARTUP_BANK_SECRET", "dev-only-secret-change-me")
_serializer = URLSafeTimedSerializer(_SECRET, salt="startup-bank-session")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(computed, digest)
    except (ValueError, AttributeError):
        return False


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"user_id": user_id})


def read_session_token(token: str) -> int | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data.get("user_id")
