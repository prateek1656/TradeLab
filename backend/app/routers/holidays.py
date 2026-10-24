from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from ..db import Session, get_db
from ..deps import current_user
from ..models import Holiday, User
from ..schemas import HolidayIn
from ..services import calendar, trading

# Exchange holidays are shared by everyone using this server: on these dates
# no automatic entry or square-off runs (weekends are skipped automatically).
router = APIRouter(prefix="/api/holidays", tags=["holidays"])


def owner(user: User = Depends(current_user)) -> User:
    """The holiday list is shared by everyone, so only the server's owner may change it."""
    if not user.is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Only the owner of this TradeLab (the first account created) can change market holidays.")
    return user


@router.get("")
def list_holidays(_: User = Depends(current_user), db: Session = Depends(get_db)):
    """Upcoming holidays (from NSE's calendar, detected live, or added by hand) and NSE sync status."""
    today = trading.now_ist().date()
    rows = db.find(Holiday, {"day": {"$gte": today}, "source": {"$ne": "removed"}}, sort=[("day", 1)])
    return {
        "items": [{"day": h.day.isoformat(), "description": h.description, "source": h.source} for h in rows],
        "nse": calendar.sync_status(),
        "clearing": calendar.clearing_sync_status(),
    }


@router.post("/sync")
async def sync_holidays(_: User = Depends(owner)):
    """Download NSE's holiday calendar now."""
    return await run_in_threadpool(calendar.sync_nse_holidays)


@router.post("", status_code=201)
def add_holiday(body: HolidayIn, _: User = Depends(owner), db: Session = Depends(get_db)):
    if body.day < trading.now_ist().date():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That date has already passed")
    h = db.get(Holiday, body.day)
    if h is None:
        db.add(Holiday(day=body.day, description=body.description, source="manual"))
    else:
        h.description = body.description
        if h.source == "removed":
            h.source = "manual"
    db.commit()
    return {"day": body.day.isoformat(), "description": body.description, "source": "manual"}


@router.delete("/{day}", status_code=204)
def delete_holiday(day: date, _: User = Depends(owner), db: Session = Depends(get_db)):
    h = db.get(Holiday, day)
    if h and h.source == "nse":
        # Kept as "removed", so the next NSE download doesn't bring it back.
        h.source, h.description = "removed", f"Removed by the owner ({h.description})"[:200]
        db.commit()
    elif h and h.source != "removed":
        db.delete(h)
        db.commit()
