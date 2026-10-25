from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..db import Session, get_db
from ..deps import current_user
from ..models import ExecutionRun, OrderLog, User
from ..schemas import ArmIn, AutomationIn, AutomationOut, CancelIn, LogOut, RunOut
from ..services import scheduler, trading
from ..services.sections import ORDER, SECTIONS

router = APIRouter(prefix="/api/automation", tags=["automation"])

MARKET_OPEN = "09:15:00"
MARKET_CLOSE = "15:30:00"
# Intraday's square-off must come before Kotak's own (15:05): 15:00 at the latest.
LAST_SQUARE_OFF = SECTIONS["intraday"].sell_until.strftime("%H:%M:%S")
LAST_BUY = SECTIONS["intraday"].buy_until.strftime("%H:%M:%S")
PREOPEN = "09:00:00"


def _section(section: str) -> str:
    if section not in SECTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section {section!r}")
    return section


def _legacy_out(db: Session, user_id: int) -> AutomationOut:
    s = trading.get_section(db, user_id, "intraday")
    return AutomationOut(entry_time=s.entry_time, exit_time=s.exit_time or "14:30:00", auto_exit=s.auto_exit,
                         squareoff_scope="intraday", sizing_mode=s.sizing_mode, reserve_pct=s.reserve_pct or 0.0)


@router.get("/settings", response_model=AutomationOut)
def get_settings_(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Intraday settings in the shape used before sections existed (kept for older app versions)."""
    out = _legacy_out(db, user.id)
    db.commit()
    return out


@router.put("/settings", response_model=AutomationOut)
def put_settings(body: AutomationIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = trading.get_section(db, user.id, "intraday")
    data = body.model_dump(exclude_none=True)
    entry = data.get("entry_time", s.entry_time)
    exit_ = data.get("exit_time", s.exit_time)
    if not (MARKET_OPEN <= entry <= LAST_BUY and exit_ <= LAST_SQUARE_OFF and trading.times_ok(entry, exit_)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Buy between 09:15 and {LAST_BUY[:5]}, and square off at least 5 minutes after the buy and by "
            f"{LAST_SQUARE_OFF[:5]} (Kotak squares intraday positions off itself at 15:05)",
        )
    # (squareoff_scope is ignored: a square-off never sells delivery holdings any more.)
    for key in ("entry_time", "exit_time", "auto_exit", "sizing_mode", "reserve_pct"):
        if key in data:
            setattr(s, key, data[key])
    db.commit()
    scheduler.settings_changed(user.id)
    return _legacy_out(db, user.id)


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
    legacy = _legacy_out(db, user.id)
    sections = {key: trading.section_info(db, user.id, key) for key in ORDER}
    db.commit()
    runs = db.find(ExecutionRun, {"user_id": user.id, "trading_date": today}, sort=[("started_at", -1), ("id", -1)])
    states = {}
    for key in ORDER:
        if SECTIONS[key].scheduled:
            state = trading.session_state(user.id, key)
        else:
            live = trading.live_items(user.id, key)
            state = {"section": key, "label": SECTIONS[key].label, "stocks": [i.symbol for i in live.items],
                     "account_ids": live.accounts, "expired": trading._expired(live, today)}
        state["settings"] = sections[key]
        state["traded_today"] = bool(trading.accounts_traded_today(user.id, today, key))
        if not SECTIONS[key].scheduled or SECTIONS[key].can_sell:
            state["buy_blocked"] = trading.trade_window_error(key, "buy", now)
            state["sell_blocked"] = trading.trade_window_error(key, "sell", now)
        states[key] = state
    return {
        "server_time": now.isoformat(),
        "trading_day": trading_day,
        "holiday": trading.holiday_name(today),
        "phase": _phase(now, trading_day),
        "next": scheduler.next_run_times(user.id),
        "settings": legacy,
        "today_runs": [RunOut.model_validate(r) for r in runs],
        # Whether TradeLab's intraday section bought anything today (its automatic square-off
        # only runs where it did), and what "Let's Trade" would do for intraday.
        "traded_today": states["intraday"]["traded_today"],
        "lets_trade": states["intraday"],
        "sections": states,
    }


@router.get("/preview")
def preview(section: str = "intraday", user: User = Depends(current_user)):
    """The orders "Let's Trade" would place for a section's current list right now (nothing is sent)."""
    live = trading.live_items(user.id, _section(section))
    plan = trading.build_entry_plan(user.id, live.items, live.sizing_mode, live.reserve_pct, all_prices=True,
                                    account_ids=live.accounts, section=section, product=live.product,
                                    max_lots=live.max_lots, preview=True)
    return [
        {
            "account_id": p.account_id,
            "account_name": p.account_name,
            "symbol": p.symbol,
            "side": p.side,
            "quantity": p.quantity,
            "lots": p.lots,
            "price": p.price,
            "note": p.note,
            "skip": p.skip,
            "funds_short": p.funds_short,
        }
        for p in plan
    ]


@router.post("/lets-trade")
def lets_trade(body: ArmIn, user: User = Depends(current_user)):
    """The user said "Yes" to "Are you sure?": allow a section's next trading day (or trade now in-session)."""
    try:
        return trading.arm(user.id, body.trade_date, body.fingerprint, _section(body.section))
    except (trading.SessionError, trading.RunInProgress) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/lets-trade/cancel")
def cancel_lets_trade(body: CancelIn | None = None, user: User = Depends(current_user)):
    section = _section(body.section if body else "intraday")
    try:
        return trading.cancel_session(user.id, body.trade_date if body else None, section)
    except trading.SessionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/square-off-all")
def square_off_all(user: User = Depends(current_user)):
    """Panic button, in every account: close every intraday (MIS) position and the carry-forward
    contracts TradeLab bought, and cancel today's Let's Trade. Delivery holdings are never sold."""
    try:
        return {"run_id": trading.launch("exit", user.id, section=trading.ALL)}
    except trading.RunInProgress as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/square-off")
def square_off_section(section: str, user: User = Depends(current_user)):
    """Square off Intraday now (in every account). Other sections sell with their Sell button."""
    sp = SECTIONS[_section(section)]
    if not sp.has_exit:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{sp.label} isn't squared off; use its Sell button")
    try:
        return {"run_id": trading.launch("exit", user.id, section=section)}
    except trading.RunInProgress as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/runs", response_model=list[RunOut])
def runs(limit: int = Query(30, ge=1, le=200), section: str | None = None, user: User = Depends(current_user),
         db: Session = Depends(get_db)):
    q: dict = {"user_id": user.id}
    if section is not None:
        q["section"] = section if section == trading.ALL else _section(section)
    return db.find(ExecutionRun, q, sort=[("started_at", -1), ("id", -1)], limit=limit)


@router.get("/runs/{run_id}", response_model=RunOut)
def run_detail(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(ExecutionRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.get("/logs", response_model=list[LogOut])
def logs(
    run_id: int | None = None,
    section: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    q: dict = {"user_id": user.id}
    if run_id is not None:
        q["run_id"] = run_id
    if section is not None:
        q["section"] = _section(section)
    return db.find(OrderLog, q, sort=[("created_at", -1), ("id", -1)], limit=limit)
