from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db import Session, database, get_db
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


PENDING = "Your account is waiting for the administrator's approval. You can log in once it's approved."
BLOCKED = "Your access to TradeLab has been removed. Contact the administrator."
CLOSED = "New sign-ups are closed. Ask the administrator for access."


def signups_open() -> bool:
    doc = database()["app_settings"].find_one({"_id": "access"})
    return True if doc is None else bool(doc.get("signups_open", True))


def _session(user: User) -> dict:
    return {"token": create_access_token(user.id, user.token_version), "user": UserOut.model_validate(user)}


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)):
    _throttle(f"signup:{_client_ip(request)}")
    email = body.email.lower()
    approval = get_settings().admin_enabled  # with an admin, every new account waits for approval
    if approval and not signups_open():
        raise HTTPException(status.HTTP_403_FORBIDDEN, CLOSED)
    if db.count(User, {"email": email}):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    user = User(name=body.name.strip(), email=email, password_hash=hash_password(body.password),
                status="pending" if approval else "active", last_login_at=None if approval else utcnow())
    db.add(user)  # gets its id here, so both records are saved together below
    db.add(AutomationSettings(user_id=user.id))
    db.commit()
    scheduler.sync_jobs()
    if approval:
        return JSONResponse({"pending": True, "message": PENDING}, status_code=202)
    return _session(user)


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower()
    _throttle(f"login:{_client_ip(request)}:{email}")
    user = db.find_one(User, {"email": email})
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, PENDING if user.status == "pending" else BLOCKED)
    user.last_login_at = utcnow()
    db.commit()
    return _session(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    # Validated here: FastAPI would turn the record into a dict of its fields and drop `is_owner`.
    return UserOut.model_validate(user)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # signs out every other device
    db.commit()
    return _session(user)
