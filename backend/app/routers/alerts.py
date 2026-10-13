from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from ..db import Session, get_db
from ..deps import current_user
from ..models import ExecutionRun, OrderLog, User
from ..services import trading

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

ALERT_DAYS = 2  # pop-ups for runs older than this are no longer shown


@router.get("")
def list_alerts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Pop-ups not dismissed yet: runs where some accounts couldn't trade for lack of money (kind "funds"),
    and square-offs or exits that failed or partly failed, so positions may still be open (kind "exit")."""
    since = trading.now_ist().date() - timedelta(days=ALERT_DAYS - 1)
    runs = db.find(
        ExecutionRun,
        {"user_id": user.id, "trading_date": {"$gte": since}, "alert_seen": False,
         "$or": [{"funds_short": {"$ne": None}}, {"kind": "exit", "status": {"$in": ["failed", "partial"]}}]},
        sort=[("started_at", -1), ("id", -1)],
    )
    out = []
    for r in runs:
        if r.kind == "exit":
            names = db.distinct(OrderLog, "account_name", {"run_id": r.id, "account_id": {"$ne": None},
                                                            "status": {"$in": ["failed", "unknown"]}})
            kind, accounts = "exit", sorted(names)
        elif r.funds_short:
            kind, accounts = "funds", r.funds_short
        else:
            continue
        out.append({
            "run_id": r.id,
            "kind": kind,
            "section": getattr(r, "section", "intraday"),
            "trading_date": r.trading_date.isoformat(),
            "started_at": trading._aware(r.started_at).isoformat(),
            "accounts": accounts,
        })
    return out


@router.post("/{run_id}/seen", status_code=204)
def dismiss(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(ExecutionRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    run.alert_seen = True
    db.commit()
