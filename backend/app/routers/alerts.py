from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from ..db import Session, get_db
from ..deps import current_user
from ..models import ExecutionRun, OrderLog, User
from ..services import trading
from ..services.sections import SECTIONS

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

ALERT_DAYS = 2  # pop-ups for runs older than this are no longer shown


@router.get("")
def list_alerts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Pop-ups not dismissed yet: runs where some accounts couldn't trade for lack of money (kind "funds"),
    and square-offs or exits that failed or partly failed, so positions may still be open (kind "exit"), and Buys of
    F&O / Commodity that failed or partly failed, so a pair may be missing its hedge (kind "entry")."""
    since = trading.now_ist().date() - timedelta(days=ALERT_DAYS - 1)
    runs = db.find(
        ExecutionRun,
        {"user_id": user.id, "trading_date": {"$gte": since}, "alert_seen": False,
         "$or": [{"funds_short": {"$ne": None}}, {"kind": "exit", "status": {"$in": ["failed", "partial"]}},
                 {"kind": "entry", "section": {"$in": [k for k, sp in SECTIONS.items() if sp.lots]},
                  "status": {"$in": ["failed", "partial"]}}]},
        sort=[("started_at", -1), ("id", -1)],
    )
    # An exit that failed or partly failed no longer matters once a later exit of the same section and day is
    # "done"; repeated failed attempts (the 60-second retries) show once, the newest.
    done_ids: dict = {}
    for d in db.find(ExecutionRun, {"user_id": user.id, "trading_date": {"$gte": since}, "kind": "exit",
                                    "status": "done"}, track=False):
        key = (d.section, d.trading_date)
        done_ids[key] = max(done_ids.get(key, 0), d.id)
    newest: set = set()
    out = []
    for r in runs:
        if r.kind == "exit":
            key = (r.section, r.trading_date)
            if done_ids.get(key, 0) > r.id or key in newest:
                continue
            newest.add(key)
            names = db.distinct(OrderLog, "account_name", {"run_id": r.id, "account_id": {"$ne": None},
                                                            "status": {"$in": ["failed", "unknown"]}})
            kind, accounts = "exit", sorted(names)
        else:
            names = []
            if r.status in ("failed", "partial") and getattr(SECTIONS.get(r.section), "lots", False):
                # Rows that failed for another reason than money (a funds skip has its own pop-up).
                names = sorted({l.account_name for l in db.find(
                    OrderLog, {"run_id": r.id, "account_id": {"$ne": None}, "status": {"$in": ["failed", "unknown"]}},
                    track=False) if not trading.is_funds_rejection(l.message)})
            if names:
                kind, accounts = "entry", names
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
