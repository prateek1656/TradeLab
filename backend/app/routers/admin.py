from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import IST
from ..db import get_db
from ..deps import admin_user
from ..models import BrokerAccount, Holiday, User
from ..schemas import AdminUserOut, AdminUserPatch, HolidayIn
from ..services import scheduler
from ..services.scrips import scrip_master
from ..services.trading import now_ist

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserOut])
def list_users(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    counts = dict(db.execute(select(BrokerAccount.user_id, func.count()).group_by(BrokerAccount.user_id)).all())
    users = db.scalars(select(User).order_by(User.status != "pending", User.created_at.desc())).all()
    out = []
    for u in users:
        item = AdminUserOut.model_validate(u)
        item.account_count = counts.get(u.id, 0)
        out.append(item)
    return out


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(user_id: int, body: AdminUserPatch, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and (body.status not in (None, "approved") or body.role not in (None, "admin")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't remove your own admin access")
    if body.status is not None and body.status != user.status:
        user.status = body.status
        if body.status != "approved":
            user.token_version += 1  # kick them out immediately
    if body.role is not None:
        user.role = body.role
    db.commit()
    scheduler.sync_jobs()
    out = AdminUserOut.model_validate(user)
    out.account_count = db.scalar(select(func.count()).where(BrokerAccount.user_id == user.id)) or 0
    return out


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't delete yourself")
    if db.scalar(select(func.count()).where(BrokerAccount.user_id == user.id)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This user still has trading accounts. Revoke their access instead, or have them delete their accounts first.",
        )
    db.delete(user)
    db.commit()
    scheduler.sync_jobs()


@router.get("/holidays")
def list_holidays(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    today = now_ist().date()
    rows = db.scalars(select(Holiday).where(Holiday.day >= date(today.year - 1, 1, 1)).order_by(Holiday.day)).all()
    return [{"day": h.day.isoformat(), "description": h.description} for h in rows]


@router.post("/holidays", status_code=201)
def add_holiday(body: HolidayIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    h = db.get(Holiday, body.day)
    if h is None:
        db.add(Holiday(day=body.day, description=body.description))
    else:
        h.description = body.description
    db.commit()
    return {"day": body.day.isoformat(), "description": body.description}


@router.delete("/holidays/{day}", status_code=204)
def delete_holiday(day: date, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    h = db.get(Holiday, day)
    if h:
        db.delete(h)
        db.commit()


@router.get("/system")
def system(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {
        "server_time": now_ist().isoformat(),
        "timezone": str(IST),
        "jobs": scheduler.jobs_overview(),
        "stocks": scrip_master.status(),
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "accounts": db.scalar(select(func.count()).select_from(BrokerAccount)) or 0,
    }


@router.post("/stocks/refresh")
def refresh_stocks(_: User = Depends(admin_user)):
    scrip_master.ensure_loaded(force=True)
    return scrip_master.status()
