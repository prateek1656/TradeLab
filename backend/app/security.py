from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_settings = get_settings()
_fernet = Fernet(_settings.fernet_key())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


def hash_admin_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt:{salt.hex()}:{digest.hex()}"


def verify_admin_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split(":")
        if kind != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int, token_version: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "ver": token_version,
        "iat": now,
        "exp": now + timedelta(hours=_settings.token_ttl_hours),
    }
    return jwt.encode(payload, _settings.jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, _settings.jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def encrypt_json(data: dict[str, Any]) -> str:
    return _fernet.encrypt(json.dumps(data).encode()).decode()


def decrypt_json(blob: str) -> dict[str, Any]:
    try:
        return json.loads(_fernet.decrypt(blob.encode()))
    except InvalidToken as exc:  # secret key changed or data tampered with
        raise ValueError("Stored credentials cannot be decrypted with the current secret key") from exc


def mask(value: str, keep: int = 3) -> str:
    value = value.strip()
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]
