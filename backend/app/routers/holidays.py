from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import Holiday, User
from ..schemas import HolidayIn
from ..services import trading

# Exchange holidays are shared by everyone using this server: on these dates
# no automatic entry or square-off runs (weekends are skipped automatically).
router = APIRouter(prefix="/api/holidays", tags=["holidays"])


@router.get("")
def list_holidays(_: User = Depends(current_user), db: Session = Depends(get_db)):
    today = trading.now_ist().date()
    rows = db.scalars(select(Holiday).where(Holiday.day >= date(today.year - 1, 1, 1)).order_by(Holiday.day)).all()
    return [{"day": h.day.isoformat(), "description": h.description} for h in rows]


@router.post("", status_code=201)
def add_holiday(body: HolidayIn, _: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.day < trading.now_ist().date():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That date has already passed")
    h = db.get(Holiday, body.day)
    if h is None:
        db.add(Holiday(day=body.day, description=body.description))
    else:
        h.description = body.description
    db.commit()
    return {"day": body.day.isoformat(), "description": body.description}


@router.delete("/{day}", status_code=204)
def delete_holiday(day: date, _: User = Depends(current_user), db: Session = Depends(get_db)):
    h = db.get(Holiday, day)
    if h:
        db.delete(h)
        db.commit()
