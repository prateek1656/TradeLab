from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
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
    if user.status != "approved":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your access has been revoked")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user
