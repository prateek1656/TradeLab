from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import ExecutionRun, OrderLog, User
from ..schemas import ArmIn, AutomationIn, CancelIn, AutomationOut, LogOut, RunOut
from ..services import scheduler, trading

router = APIRouter(prefix="/api/automation", tags=["automation"])

MARKET_OPEN = "09:15:00"
MARKET_CLOSE = "15:30:00"
PREOPEN = "09:00:00"


@router.get("/settings", response_model=AutomationOut)
def get_settings_(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = trading.get_automation(db, user.id)
    db.commit()
    return s


@router.put("/settings", response_model=AutomationOut)
def put_settings(body: AutomationIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = trading.get_automation(db, user.id)
    data = body.model_dump(exclude_none=True)
    entry = data.get("entry_time", s.entry_time)
    exit_ = data.get("exit_time", s.exit_time)
    if not (MARKET_OPEN <= entry < exit_ <= MARKET_CLOSE):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Entry must be at or after 09:15, and square-off must be after entry and before 15:30",
        )
    for k, v in data.items():
        setattr(s, k, v)
    db.commit()
    scheduler.sync_jobs()
    return s


def _phase(now: datetime, trading_day: bool) -> str:
    t = now.strftime("%H:%M:%S")
    if not trading_day:
        return "holiday" if now.weekday() < 5 else "weekend"
    if t < PREOPEN:
        return "closed"
    if t < MARKET_OPEN:
        return "preopen"
    if t < MARKET_CLOSE:
        return "open"
    return "closed"


@router.get("/status")
def automation_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = trading.now_ist()
    today = now.date()
    trading_day = trading.is_trading_day(today)
    s = trading.get_automation(db, user.id)
    db.commit()
    runs = db.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.user_id == user.id, ExecutionRun.trading_date == today)
        .order_by(ExecutionRun.started_at.desc())
    ).all()
    return {
        "server_time": now.isoformat(),
        "trading_day": trading_day,
        "holiday": trading.holiday_name(today),
        "phase": _phase(now, trading_day),
        "next": scheduler.next_run_times(user.id),
        "settings": AutomationOut.model_validate(s),
        "today_runs": [RunOut.model_validate(r) for r in runs],
        # Whether TradeLab bought anything today (the automatic square-off only runs where it did),
        # and what "Let's Trade" would do / has been confirmed for the next trading day.
        "traded_today": bool(trading.accounts_traded_today(user.id, today)),
        "lets_trade": trading.session_state(user.id),
    }


@router.get("/preview")
def preview(user: User = Depends(current_user)):
    """The orders "Let's Trade" would place for the current list at today's prices (nothing is sent)."""
    items, mode, reserve = trading.live_items(user.id)
    plan = trading.build_entry_plan(user.id, items, mode, reserve, all_prices=True)
    return [
        {
            "account_id": p.account_id,
            "account_name": p.account_name,
            "symbol": p.symbol,
            "side": p.side,
            "quantity": p.quantity,
            "price": p.price,
            "note": p.note,
            "skip": p.skip,
        }
        for p in plan
    ]


@router.post("/lets-trade")
def lets_trade(body: ArmIn, user: User = Depends(current_user)):
    """The user said "Yes" to "Are you sure?": allow the next trading day's buy (or buy now in-session)."""
    try:
        return trading.arm(user.id, body.trade_date, body.fingerprint)
    except (trading.SessionError, trading.RunInProgress) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/lets-trade/cancel")
def cancel_lets_trade(body: CancelIn | None = None, user: User = Depends(current_user)):
    try:
        return trading.cancel_session(user.id, body.trade_date if body else None)
    except trading.SessionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/square-off-all")
def square_off_all(user: User = Depends(current_user)):
    try:
        return {"run_id": trading.launch("exit", user.id)}
    except trading.RunInProgress as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/runs", response_model=list[RunOut])
def runs(limit: int = Query(30, ge=1, le=200), user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(ExecutionRun).where(ExecutionRun.user_id == user.id).order_by(ExecutionRun.started_at.desc()).limit(limit)
    ).all()


@router.get("/runs/{run_id}", response_model=RunOut)
def run_detail(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(ExecutionRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.get("/logs", response_model=list[LogOut])
def logs(
    run_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    q = select(OrderLog).where(OrderLog.user_id == user.id)
    if run_id is not None:
        q = q.where(OrderLog.run_id == run_id)
    return db.scalars(q.order_by(OrderLog.created_at.desc(), OrderLog.id.desc()).limit(limit)).all()

