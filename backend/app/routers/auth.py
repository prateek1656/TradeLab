from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..models import AutomationSettings, User, utcnow
from ..schemas import ChangePasswordIn, LoginIn, SignupIn, UserOut
from ..security import create_access_token, hash_password, verify_password
from ..services import scheduler

router = APIRouter(prefix="/api/auth", tags=["auth"])

_attempts: dict[str, deque[float]] = defaultdict(deque)
_attempts_lock = threading.Lock()
MAX_ATTEMPTS = 8
WINDOW = 300.0


def _throttle(key: str) -> None:
    now = time.monotonic()
    with _attempts_lock:
        q = _attempts[key]
        while q and now - q[0] > WINDOW:
            q.popleft()
        if len(q) >= MAX_ATTEMPTS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
        q.append(now)


def _client_ip(request: Request) -> str:
    # Behind the bundled reverse proxy, uvicorn's --proxy-headers already resolves the real client IP.
    return request.client.host if request.client else "?"


def _session(user: User) -> dict:
    return {"token": create_access_token(user.id, user.token_version), "user": UserOut.model_validate(user)}


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)):
    _throttle(f"signup:{_client_ip(request)}")
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    settings = get_settings()
    no_admin_yet = db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) == 0
    bootstrap = no_admin_yet and (not settings.admin_email or settings.admin_email.lower() == email)
    user = User(
        name=body.name.strip(),
        email=email,
        password_hash=hash_password(body.password),
        role="admin" if bootstrap else "member",
        status="approved" if bootstrap else "pending",
    )
    db.add(user)
    db.flush()
    db.add(AutomationSettings(user_id=user.id))
    db.commit()
    if bootstrap:
        user.last_login_at = utcnow()
        db.commit()
        scheduler.sync_jobs()
        return {"status": "approved", **_session(user)}
    return {"status": "pending", "message": "Request sent. An admin must approve your access before you can log in."}


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower()
    _throttle(f"login:{_client_ip(request)}:{email}")
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if user.status == "pending":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your access request is waiting for admin approval")
    if user.status != "approved":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your access to TradeLab has been revoked")
    user.last_login_at = utcnow()
    db.commit()
    return _session(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/change-password")
def change_password(body: ChangePasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # signs out every other device
    db.commit()
    return _session(user)
