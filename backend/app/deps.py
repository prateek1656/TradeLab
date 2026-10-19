from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from .db import Session, get_db
from .models import User
from .security import decode_access_token


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    payload = decode_access_token(token) if token else None
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in again")
    user = db.get(User, int(payload["sub"]))
    if user is None or payload.get("ver") != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session ended, please log in again")
    if user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your access to TradeLab has been removed")
    return user
