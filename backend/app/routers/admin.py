"""The admin page's API: who may use TradeLab.

Every route sits under the secret part of the admin page's address (TRADELAB_ADMIN_PATH): with a wrong one,
or with no admin configured, it answers exactly like any unknown address, so nothing shows that an admin
page exists. The admin's credentials live only in the server's .env (the password as a scrypt hash), never
in the code. Admin sessions use their own signing key, so a user's token is never an admin's and vice versa.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..config import get_settings
from ..db import Session, database, get_db
from ..models import BrokerAccount, TradeSession, User
from ..schemas import AdminLoginIn, AdminSettingsIn, AdminStatusIn
from ..security import verify_admin_password
from ..services import trading
from .auth import _client_ip, _throttle, signups_open

router = APIRouter(prefix="/api/admin/{key}", include_in_schema=False)

ADMIN_TTL = timedelta(hours=8)
# Checked when the username is wrong too, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = "scrypt:00000000000000000000000000000000:" + "0" * 64


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not found")  # same as any unknown /api address


def gate(key: str) -> None:
    s = get_settings()
    if not s.admin_enabled or not hmac.compare_digest(key.encode(), (s.admin_path or "").encode()):
        raise _not_found()


def _secret() -> str:
    return hashlib.sha256(("admin-session:" + get_settings().master_secret()).encode()).hexdigest()


def _fingerprint() -> str:
    """Changes when the admin's username or password changes, which ends every admin session."""
    s = get_settings()
    return hashlib.sha256(f"{s.admin_username}\n{s.admin_password_hash}".encode()).hexdigest()[:24]


def require_admin(request: Request, key: str) -> None:
    gate(key)
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        payload = None
    if not payload or payload.get("adm") != _fingerprint():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin session ended, please log in again")


@router.get("/check")
def check(key: str):
    gate(key)
    return {"ok": True}


@router.post("/login")
def login(key: str, body: AdminLoginIn, request: Request):
    gate(key)
    _throttle(f"admin:{_client_ip(request)}")
    s = get_settings()
    name_ok = hmac.compare_digest(body.username.encode(), (s.admin_username or "").encode())
    password_ok = verify_admin_password(body.password, s.admin_password_hash if name_ok else _DUMMY_HASH)
    if not (name_ok and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    now = datetime.now(timezone.utc)
    token = jwt.encode({"adm": _fingerprint(), "iat": now, "exp": now + ADMIN_TTL}, _secret(), algorithm="HS256")
    return {"token": token}


@router.get("/users", dependencies=[Depends(require_admin)])
def users(db: Session = Depends(get_db)):
    counts: dict[int, int] = {}
    for acc in db.find(BrokerAccount, {}):
        counts[acc.user_id] = counts.get(acc.user_id, 0) + 1
    first = database()["users"].find_one({}, {"_id": 1}, sort=[("_id", 1)])
    return [
        {"id": u.id, "name": u.name, "email": u.email, "status": u.status, "created_at": u.created_at,
         "last_login_at": u.last_login_at, "accounts": counts.get(u.id, 0),
         "is_owner": first is not None and first["_id"] == u.id}
        for u in db.find(User, {}, sort=[("_id", 1)])
    ]


@router.post("/users/{user_id}/status", dependencies=[Depends(require_admin)])
def set_status(user_id: int, body: AdminStatusIn, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    cancelled = 0
    if body.status == "blocked" and user.status != "blocked":
        user.token_version += 1  # signs them out on every device at once
        # No new buys for them. Automatic square-offs still run, so nothing they hold is left open.
        cancelled = db.update_many(
            TradeSession,
            {"user_id": user_id, "trade_date": {"$gte": trading.now_ist().date()}, "status": {"$in": ["armed", "running"]}},
            {"status": "cancelled", "note": "Cancelled: access removed by the administrator"},
        )
    user.status = body.status
    db.commit()
    return {"id": user.id, "status": user.status, "cancelled_confirmations": cancelled}


@router.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
def reject(user_id: int, db: Session = Depends(get_db)):
    """Turn down a sign-up request. Only pending requests can be deleted: anyone who has used TradeLab may
    hold positions, so they are blocked instead, never deleted."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only sign-up requests can be deleted; block this user instead")
    db.delete(user)
    db.commit()
    return {"deleted": user_id}


@router.get("/settings", dependencies=[Depends(require_admin)])
def get_access_settings():
    return {"signups_open": signups_open()}


@router.put("/settings", dependencies=[Depends(require_admin)])
def put_access_settings(body: AdminSettingsIn):
    database()["app_settings"].update_one({"_id": "access"}, {"$set": {"signups_open": body.signups_open}}, upsert=True)
    return {"signups_open": body.signups_open}
