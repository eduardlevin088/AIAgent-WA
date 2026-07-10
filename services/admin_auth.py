import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


PASSWORD_ITERATIONS = 210_000
SESSION_MAX_AGE_SECONDS = 60 * 60 * 12


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations_raw),
        ).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_session(admin_id: int, secret: str, max_age_seconds: int = SESSION_MAX_AGE_SECONDS) -> str:
    payload = {
        "admin_id": admin_id,
        "exp": int(time.time()) + max_age_seconds,
    }
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64encode(signature)}"


def verify_session(token: str | None, secret: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload_b64, signature_b64 = token.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64decode(signature_b64)
        if not hmac.compare_digest(actual, expected):
            return None
        payload = json.loads(_b64decode(payload_b64))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload
