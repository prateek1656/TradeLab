from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from ..db import Session, get_db
from ..deps import current_user
from ..models import ExecutionRun, OrderLog, User
from ..services import trading
from ..services.sections import SECTIONS

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

ALERT_DAYS = 2  # funds and entry pop-ups for runs older than this are no longer shown
EXIT_ALERT_DAYS = 4  # a failed exit stays long enough to be seen on the Monday after a Friday


@router.get("")
def list_alerts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Pop-ups not dismissed yet: runs where some accounts couldn't trade for lack of money (kind "funds"),
    and square-offs or exits that failed or partly failed, so positions may still be open (kind "exit"), and Buys of
    F&O / Commodity that failed or partly failed, so a pair may be missing its hedge (kind "entry")."""
    today = trading.now_ist().date()
    since = today - timedelta(days=ALERT_DAYS - 1)
    exit_since = today - timedelta(days=EXIT_ALERT_DAYS - 1)
    runs = db.find(
        ExecutionRun,
        {"user_id": user.id, "alert_seen": False,
         "$or": [{"trading_date": {"$gte": since}, "funds_short": {"$ne": None}},
                 {"trading_date": {"$gte": exit_since}, "kind": "exit", "status": {"$in": ["failed", "partial"]}},
                 {"trading_date": {"$gte": since}, "kind": "entry",
                  "section": {"$in": [k for k, sp in SECTIONS.items() if sp.lots]},
                  "status": {"$in": ["failed", "partial"]}}]},
        sort=[("started_at", -1), ("id", -1)],
    )
    # An account's failure in an exit no longer matters once a later exit of the same section and day that
    # covered that account (and contract) finished without a failure for it; runs of older versions, which
    # recorded no scope, cover nothing. Repeated failed attempts (the 60-second retries) show once, with the
    # accounts of all of them that are still open.
    exits = db.find(ExecutionRun, {"user_id": user.id, "trading_date": {"$gte": exit_since}, "kind": "exit",
                                   "status": {"$in": ["done", "partial", "failed"]}}, track=False)
    bad: dict[int, set] = {}
    for l in db.find(OrderLog, {"run_id": {"$in": [e.id for e in exits]}, "account_id": {"$ne": None},
                                "status": {"$in": ["failed", "unknown"]}}, track=False):
        bad.setdefault(l.run_id, set()).add(l.account_id)
    later: dict = {}
    for e in exits:
        later.setdefault((e.section, e.trading_date), []).append(e)
    shown: dict = {}
    out = []
    for r in runs:
        if r.kind == "exit":
            key = (r.section, r.trading_date)
            newer = [e for e in later.get(key, []) if e.id > r.id and e.scope_accounts is not None]
            rows = db.find(OrderLog, {"run_id": r.id, "account_id": {"$ne": None},
                                      "status": {"$in": ["failed", "unknown"]}}, track=False)
            names = {l.account_name for l in rows
                     if not any(l.account_id in e.scope_accounts and l.account_id not in bad.get(e.id, ())
                                and (e.scope_symbols is None or (l.symbol or "") in e.scope_symbols)
                                for e in newer)}
            if not rows and any(e.status == "done" for e in newer):
                continue
            if rows and not names:
                continue
            if key in shown:
                shown[key]["accounts"] = sorted({*shown[key]["accounts"], *names})
                continue
            item = {
                "run_id": r.id,
                "kind": "exit",
                "section": getattr(r, "section", "intraday"),
                "trading_date": r.trading_date.isoformat(),
                "started_at": trading._aware(r.started_at).isoformat(),
                "accounts": sorted(names),
            }
            shown[key] = item
            out.append(item)
            continue
        found, entry_names = [], []
        if r.status in ("failed", "partial") and getattr(SECTIONS.get(r.section), "lots", False):
            # Rows that failed for another reason than money (a funds skip has its own pop-up).
            names = sorted({l.account_name for l in db.find(
                OrderLog, {"run_id": r.id, "account_id": {"$ne": None}, "status": {"$in": ["failed", "unknown"]}},
                track=False) if not trading.is_funds_rejection(l.message)})
            if names:
                found.append(("entry", names))
                entry_names = names
        # (An account already named in the entry pop-up isn't named twice.)
        short = [n for n in r.funds_short or [] if n not in entry_names]
        if short and r.trading_date >= since:
            found.append(("funds", short))
        for kind, accounts in found:
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
    if run.kind == "exit":  # the pop-up also stood for this day's earlier failed attempts of the section
        db.update_many(ExecutionRun, {"user_id": user.id, "kind": "exit", "section": run.section,
                                      "trading_date": run.trading_date, "id": {"$lt": run.id}}, {"alert_seen": True})
    db.commit()
