"""Morning entry (market orders at the entry time) and the afternoon hard square-off."""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import select, update

from ..broker.base import Broker, Position
from ..broker.manager import broker_manager
from ..broker.paper import synthetic_price
from ..config import IST, get_settings
from ..db import session_scope
from ..models import (
    AutomationSettings,
    BasketItem,
    BrokerAccount,
    ExecutionRun,
    Holiday,
    OrderLog,
    TradeSession,
    utcnow,
)
from ..money import inr
from . import calendar
from .scrips import scrip_master

log = logging.getLogger(__name__)

# Seconds to wait before checking that square-off orders went through (patched in tests).
VERIFY_DELAY = 2.0
CANCEL_DELAY = 1.0
SQUAREOFF_ATTEMPTS = 3
# An entry that could not start within this many seconds of its time is skipped.
ENTRY_LATE_TOLERANCE = timedelta(seconds=30)
# The entry job starts this early to log in and size orders, then fires on the dot.
ENTRY_LEAD = timedelta(seconds=60)
# If the market looks closed before the open, look again this long after it.
MARKET_RECHECK = timedelta(seconds=20)
# No new same-day buys this close to the square-off.
CLOSE_TO_EXIT = timedelta(minutes=5)
# How long an automatic square-off waits for a buy that's still being sent.
ENTRY_WAIT = timedelta(seconds=120)
INTRADAY_PRODUCTS = {"MIS", "INTRADAY", "CO", "BO", "I"}


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hms(value: str) -> dtime:
    parts = [int(p) for p in value.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dtime(parts[0], parts[1], parts[2])


def holiday_name(day: date) -> str | None:
    with session_scope() as db:
        h = db.get(Holiday, day)
        return h.description if h else None


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and holiday_name(day) is None


def get_automation(db, user_id: int) -> AutomationSettings:
    s = db.get(AutomationSettings, user_id)
    if s is None:
        s = AutomationSettings(user_id=user_id)
        db.add(s)
        db.flush()
    return s


def next_trading_day(day: date) -> date:
    for _ in range(60):
        day += timedelta(days=1)
        if is_trading_day(day):
            return day
    return day


def plan_date(user_id: int) -> date:
    """The trading day "Let's Trade" confirms right now.

    Today until today's square-off time (so confirming during the session buys
    straight away); after that, the next trading day (weekends and holidays skipped).
    """
    now = now_ist()
    with session_scope() as db:
        exit_time = parse_hms(get_automation(db, user_id).exit_time)
    if is_trading_day(now.date()) and now.time().replace(tzinfo=None) < exit_time:
        return now.date()
    return next_trading_day(now.date())


@dataclass
class _Acc:
    id: int
    name: str
    broker: str


@dataclass
class PlannedOrder:
    account_id: int
    account_name: str
    symbol: str
    trading_symbol: str
    exchange_segment: str
    side: str
    quantity: int
    skip: str | None = None
    note: str | None = None
    price: float | None = None
    # A skip caused by a problem (login, prices, funds) rather than a sizing decision.
    error: bool = False


@dataclass
class LogEntry:
    account_id: int | None
    account_name: str
    action: str
    status: str
    symbol: str | None = None
    quantity: int | None = None
    order_id: str | None = None
    message: str | None = None


def _write_logs(user_id: int, run_id: int | None, entries: list[LogEntry]) -> None:
    if not entries:
        return
    with session_scope() as db:
        for e in entries:
            db.add(
                OrderLog(
                    user_id=user_id,
                    run_id=run_id,
                    account_id=e.account_id,
                    account_name=e.account_name,
                    action=e.action,
                    symbol=e.symbol,
                    quantity=e.quantity,
                    status=e.status,
                    order_id=e.order_id,
                    message=(e.message or "")[:1000] or None,
                )
            )


def _start_run(user_id: int, kind: str, trigger: str) -> int:
    with session_scope() as db:
        run = ExecutionRun(user_id=user_id, kind=kind, trigger=trigger, trading_date=now_ist().date())
        db.add(run)
        db.flush()
        return run.id


def _finish_run(run_id: int, entries: list[LogEntry], note: str | None = None) -> None:
    orders = [e for e in entries if e.action in ("buy", "sell")]
    ok = sum(1 for e in orders if e.status == "success")
    failed = sum(1 for e in entries if e.status in ("failed", "unknown"))
    skipped = sum(1 for e in entries if e.status == "skipped")
    if failed and not ok:
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "done"
    parts = [f"{ok} order{'s' if ok != 1 else ''} placed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    if note:
        parts.insert(0, note)
    with session_scope() as db:
        run = db.get(ExecutionRun, run_id)
        if run:
            run.status = status
            run.summary = " · ".join(parts)
            run.finished_at = utcnow()


def _already_ran(user_id: int, kind: str, day: date) -> bool:
    with session_scope() as db:
        return (
            db.scalar(
                select(ExecutionRun.id).where(
                    ExecutionRun.user_id == user_id,
                    ExecutionRun.kind == kind,
                    ExecutionRun.trading_date == day,
                    ExecutionRun.trigger.in_(("scheduled", "catchup")),
                )
            )
            is not None
        )


def _pool(n: int) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=max(1, min(get_settings().max_parallel_orders, n)))


def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


# ----------------------------------------------------------------------------- entry


NO_PRICE = "No live price available to size this order"
NO_FUNDS = "Could not read available funds"
PRICE_ATTEMPTS = 3
PRICE_RETRY_DELAY = 2.0


def _qty_for_budget(budget: float, price: float | None) -> tuple[int, str | None]:
    if not price or price <= 0:
        return 0, NO_PRICE
    # Tiny epsilon so float noise (e.g. 39.9999999) doesn't cost a whole share.
    qty = int(math.floor(budget / price + 1e-9))
    if qty < 1:
        return 0, f"{inr(budget, 0)} is less than the price of one share ({inr(price)})"
    return qty, None


@dataclass
class PlanItem:
    """One stock to trade, as confirmed with "Let's Trade" (or the live list, for previews)."""

    symbol: str
    trading_symbol: str
    exchange_segment: str
    side: str
    sizing_mode: str
    sizing_value: float
    token: str | None = None
    name: str | None = None
    account_ids: list[int] | None = None

    @classmethod
    def from_item(cls, it: BasketItem) -> PlanItem:
        return cls(
            symbol=it.symbol, trading_symbol=it.trading_symbol, exchange_segment=it.exchange_segment,
            side=it.side, sizing_mode=it.sizing_mode, sizing_value=it.sizing_value, token=it.token,
            name=it.name, account_ids=list(it.account_ids) if it.account_ids else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _size_custom(item: PlanItem, price: float | None, funds: float | None) -> tuple[int, str | None, str | None]:
    """Size from the stock's own setting: fixed shares, a rupee amount or a % of funds."""
    if item.sizing_mode == "qty":
        qty = int(item.sizing_value)
        return (qty, None, None) if qty >= 1 else (0, "Quantity must be at least 1", None)
    if item.sizing_mode == "amount":
        budget = item.sizing_value
    else:
        if funds is None:
            return 0, NO_FUNDS, None
        budget = funds * item.sizing_value / 100
    qty, reason = _qty_for_budget(budget, price)
    return qty, reason, None if reason else f"{inr(budget, 0)} budget at ≈{inr(price or 0)}"


def _size_equal(
    funds: float | None, parts: int, reserve_pct: float, price: float | None
) -> tuple[int, str | None, str | None]:
    """Split the account's available funds equally across its stocks; buy as many shares as fit."""
    if funds is None:
        return 0, NO_FUNDS, None
    usable = funds * (1 - reserve_pct / 100)
    if usable <= 0:
        return 0, f"No money available in this account ({inr(funds)})", None
    share = usable / parts
    qty, reason = _qty_for_budget(share, price)
    note = f"{inr(share, 0)} allotted (1/{parts} of {inr(usable, 0)}) at ≈{inr(price or 0)}"
    return qty, reason, None if reason else note


def _prices(user_id: int, items: list[PlanItem], everything: bool) -> dict[str, float]:
    """Latest prices by trading symbol, retrying and falling back to other Kotak accounts."""
    wanted: dict[str, tuple[str, str]] = {}
    for it in items:
        if everything or it.sizing_mode in ("amount", "percent"):
            tok = it.token or scrip_master.token_for(it.trading_symbol)
            if tok:
                wanted[it.trading_symbol] = (it.exchange_segment, tok)
    prices: dict[str, float] = {}
    if not wanted:
        return prices
    clients = broker_manager.market_data_clients(user_id)
    if not clients:
        return prices
    for attempt in range(PRICE_ATTEMPTS):
        for client in clients:
            missing = {ts: v for ts, v in wanted.items() if ts not in prices}
            if not missing:
                return prices
            try:
                by_token = client.ltp(list(set(missing.values())))
            except Exception as exc:  # noqa: BLE001
                log.warning("Price fetch failed (attempt %s): %s", attempt + 1, exc)
                continue
            for ts, (_, tok) in missing.items():
                if tok in by_token:
                    prices[ts] = by_token[tok]
        if len(prices) == len(wanted):
            break
        if attempt < PRICE_ATTEMPTS - 1:
            time.sleep(PRICE_RETRY_DELAY)
    return prices


def build_entry_plan(
    user_id: int, items: list[PlanItem], sizing_mode: str, reserve_pct: float, all_prices: bool = False
) -> list[PlannedOrder]:
    """Orders for `items` in every autopilot account. Nothing is sent."""
    with session_scope() as db:
        accounts = [
            _Acc(a.id, a.name, a.broker)
            for a in db.scalars(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == user_id, BrokerAccount.automation_enabled.is_(True)
                )
            )
        ]
    if not accounts or not items:
        return []
    equal = sizing_mode == "equal"
    reserve_pct = reserve_pct or 0.0
    prices = _prices(user_id, items, everything=equal or all_prices)

    def applies(item: PlanItem, acc: _Acc) -> bool:
        return not item.account_ids or acc.id in item.account_ids

    def prepare(acc: _Acc) -> tuple[_Acc, float | None, str | None]:
        mine = [i for i in items if applies(i, acc)]
        if not mine:
            return acc, None, None
        try:
            broker_manager.ensure_session(acc.id)
        except Exception as exc:  # noqa: BLE001
            return acc, None, f"Login failed: {exc}"
        funds = None
        if equal or any(i.sizing_mode == "percent" for i in mine):
            try:
                funds = broker_manager.call(acc.id, lambda b: b.funds()).available
            except Exception as exc:  # noqa: BLE001
                log.warning("Funds unavailable for %s: %s", acc.name, exc)
        return acc, funds, None

    with _pool(len(accounts)) as ex:
        prepared = list(ex.map(prepare, accounts))

    plan: list[PlannedOrder] = []
    for acc, funds, err in prepared:
        mine = [it for it in items if applies(it, acc)]
        for it in mine:
            price = prices.get(it.trading_symbol)
            if price is None and acc.broker == "paper":
                price = synthetic_price(it.trading_symbol)
            if equal:
                qty, reason, note = _size_equal(funds, len(mine), reserve_pct, price)
            else:
                qty, reason, note = _size_custom(it, price, funds)
            plan.append(
                PlannedOrder(
                    account_id=acc.id,
                    account_name=acc.name,
                    symbol=it.symbol,
                    trading_symbol=it.trading_symbol,
                    exchange_segment=it.exchange_segment,
                    side=it.side,
                    quantity=qty,
                    skip=err or reason,
                    note=note,
                    price=price,
                    error=bool(err) or reason in (NO_PRICE, NO_FUNDS),
                )
            )
    return plan


UNKNOWN = "unknown"  # the broker may or may not have the order (e.g. the reply timed out)


def _skip_entries(plan: list[PlannedOrder]) -> list[LogEntry]:
    return [
        LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell",
                 "failed" if p.error else "skipped", p.trading_symbol, p.quantity or None, message=p.skip)
        for p in plan
        if p.skip
    ]


def _reserve_logs(user_id: int, run_id: int, tasks: list[PlannedOrder]) -> list[int]:
    """Record every order as "pending" *before* sending it, so a crash or lost reply
    can never hide an order from the square-off."""
    with session_scope() as db:
        rows = [
            OrderLog(user_id=user_id, run_id=run_id, account_id=p.account_id, account_name=p.account_name,
                     action="buy" if p.side == "B" else "sell", symbol=p.trading_symbol, quantity=p.quantity,
                     status="pending", message="Sending…")
            for p in tasks
        ]
        db.add_all(rows)
        db.flush()
        return [r.id for r in rows]


def _settle_logs(ids: list[int], results: list[LogEntry]) -> None:
    with session_scope() as db:
        for log_id, e in zip(ids, results):
            row = db.get(OrderLog, log_id)
            if row is not None:
                row.status, row.order_id, row.message = e.status, e.order_id, (e.message or "")[:1000] or None


def fire_plan(plan: list[PlannedOrder], user_id: int | None = None, run_id: int | None = None) -> list[LogEntry]:
    """Send every planned order at once. Orders are logged as pending first when a run is given."""
    entries = _skip_entries(plan)
    tasks = [p for p in plan if not p.skip]
    if not tasks:
        return entries
    ids = _reserve_logs(user_id, run_id, tasks) if run_id is not None else []

    def place(p: PlannedOrder) -> LogEntry:
        action = "buy" if p.side == "B" else "sell"
        try:
            res = broker_manager.call(
                p.account_id,
                lambda b: b.place_market_order(p.exchange_segment, p.trading_symbol, p.side, p.quantity, "MIS"),
            )
            message = f"{res.message} · {p.note}" if p.note and res.ok else res.message
            return LogEntry(p.account_id, p.account_name, action, "success" if res.ok else "failed",
                            p.trading_symbol, p.quantity, res.order_id, message)
        except Exception as exc:  # noqa: BLE001
            return LogEntry(p.account_id, p.account_name, action, UNKNOWN, p.trading_symbol, p.quantity,
                            message=f"No reply from the broker ({exc}). The order may have gone through — "
                                    "check the Kotak app. This account is still squared off.")

    with _pool(len(tasks)) as ex:
        results = list(ex.map(place, tasks))
    if ids:
        _settle_logs(ids, results)
    return entries + results


# ----------------------------------------------------------------------------- Let's Trade


class SessionError(Exception):
    """A "Let's Trade" request that can't be accepted (message is shown to the user)."""


def live_items(user_id: int) -> tuple[list[PlanItem], str, float]:
    """The current stock list (switched-on stocks) and money settings."""
    with session_scope() as db:
        s = get_automation(db, user_id)
        items = [
            PlanItem.from_item(it)
            for it in db.scalars(
                select(BasketItem)
                .where(BasketItem.user_id == user_id, BasketItem.active.is_(True))
                .order_by(BasketItem.created_at, BasketItem.id)
            )
        ]
        return items, s.sizing_mode, s.reserve_pct or 0.0


def _entry_moment(user_id: int, day: date) -> datetime:
    with session_scope() as db:
        return datetime.combine(day, parse_hms(get_automation(db, user_id).entry_time), tzinfo=IST)


def _exit_moment(user_id: int, day: date) -> datetime:
    with session_scope() as db:
        return datetime.combine(day, parse_hms(get_automation(db, user_id).exit_time), tzinfo=IST)


def _automation_account_count(user_id: int) -> int:
    with session_scope() as db:
        return len(list(db.scalars(select(BrokerAccount.id).where(
            BrokerAccount.user_id == user_id, BrokerAccount.automation_enabled.is_(True)))))


def session_state(user_id: int) -> dict:
    """What "Let's Trade" would do right now, and whether the next trading day is confirmed."""
    day = plan_date(user_id)
    now = now_ist()
    items, mode, reserve = live_items(user_id)
    entry_at = _entry_moment(user_id, day)
    with session_scope() as db:
        sess = db.scalar(select(TradeSession).where(TradeSession.user_id == user_id, TradeSession.trade_date == day))
        current = None
        if sess is not None:
            live = sorted((i.to_dict() for i in items), key=lambda d: d["trading_symbol"])
            confirmed = sorted(sess.items, key=lambda d: d["trading_symbol"])
            current = {
                "status": sess.status,
                "confirmed_at": (sess.confirmed_at if sess.confirmed_at.tzinfo
                                 else sess.confirmed_at.replace(tzinfo=timezone.utc)).isoformat(),
                "stocks": [i["symbol"] for i in sess.items],
                "sizing_mode": sess.sizing_mode,
                "reserve_pct": sess.reserve_pct,
                "changed": live != confirmed or mode != sess.sizing_mode or reserve != sess.reserve_pct,
                "note": sess.note,
            }
    with session_scope() as db:
        others = [
            d.isoformat()
            for d in db.scalars(
                select(TradeSession.trade_date).where(
                    TradeSession.user_id == user_id,
                    TradeSession.status == "armed",
                    TradeSession.trade_date != day,
                    TradeSession.trade_date >= now.date(),
                ).order_by(TradeSession.trade_date)
            )
        ]
    return {
        "trade_date": day.isoformat(),
        "entry_at": entry_at.isoformat(),
        # Other days still confirmed (e.g. after a holiday was removed); they trade on their date.
        "other_confirmed": others,
        "closing_soon": day == now.date() and now >= _exit_moment(user_id, day) - CLOSE_TO_EXIT,
        # Confirming now places orders straight away (the 09:15 moment has passed or is a minute away).
        "buys_now": day == now.date() and now >= entry_at - ENTRY_LEAD,
        "stocks": [i.symbol for i in items],
        "sizing_mode": mode,
        "reserve_pct": reserve,
        "autopilot_accounts": _automation_account_count(user_id),
        "session": current,
    }


def arm(user_id: int, trade_date: date) -> dict:
    """Confirm ("Let's Trade") the next trading day with a snapshot of the current list."""
    day = plan_date(user_id)
    if trade_date != day:
        raise SessionError(f"The trading day is now {day:%a, %d %b}. Check the details and confirm again.")
    items, mode, reserve = live_items(user_id)
    if not items:
        raise SessionError("Add at least one stock (and switch it on) before you trade.")
    if not _automation_account_count(user_id):
        raise SessionError("Switch on autopilot for at least one account before you trade.")
    now = now_ist()
    if day == now.date() and now >= _exit_moment(user_id, day) - CLOSE_TO_EXIT:
        raise SessionError("Too close to today's square-off. Confirm again after it, for the next trading day.")
    with session_scope() as db:
        sess = db.scalar(select(TradeSession).where(TradeSession.user_id == user_id, TradeSession.trade_date == day))
        if sess is not None and sess.status in ("running", "firing", "done"):
            raise SessionError("TradeLab already traded this day. You can confirm the next day after the square-off.")
        if sess is None:
            sess = TradeSession(user_id=user_id, trade_date=day)
            db.add(sess)
        sess.status = "armed"
        sess.items = [i.to_dict() for i in items]
        sess.sizing_mode = mode
        sess.reserve_pct = reserve
        sess.confirmed_at = utcnow()
        sess.note = None
    entry_at = _entry_moment(user_id, day)
    run_id = None
    if day == now.date() and now >= entry_at - ENTRY_LEAD:
        if now >= _exit_moment(user_id, day) - CLOSE_TO_EXIT:
            with session_scope() as db:
                db.execute(update(TradeSession).where(TradeSession.user_id == user_id, TradeSession.trade_date == day)
                           .values(status="cancelled", note="Too close to the square-off"))
            raise SessionError("Too close to today's square-off. Confirm again after it, for the next trading day.")
        # The scheduled job has already started (or passed): trade from here.
        run_id = launch("entry", user_id, fire_at=max(entry_at, now))
    return {"run_id": run_id, **session_state(user_id)}


def cancel_session(user_id: int, trade_date: date | None = None) -> dict:
    """Withdraw a "Let's Trade" confirmation. Works until the orders actually go out."""
    day = trade_date or plan_date(user_id)
    with session_scope() as db:
        cancelled = db.execute(
            update(TradeSession)
            .where(
                TradeSession.user_id == user_id,
                TradeSession.trade_date == day,
                TradeSession.status.in_(("armed", "running")),
            )
            .values(status="cancelled", note="Cancelled by you")
        ).rowcount
        if not cancelled:
            status = db.scalar(select(TradeSession.status).where(
                TradeSession.user_id == user_id, TradeSession.trade_date == day))
            raise SessionError("Nothing to cancel." if status in (None, "cancelled", "skipped")
                               else "Too late: the orders have already been sent.")
    return session_state(user_id)


def _claim_session(user_id: int, day: date) -> TradeSession | None:
    """Atomically take the confirmed session for `day` so it can only be traded once."""
    with session_scope() as db:
        sess = db.scalar(select(TradeSession).where(TradeSession.user_id == user_id, TradeSession.trade_date == day))
        if sess is None:
            return None
        claimed = db.execute(
            update(TradeSession)
            .where(TradeSession.id == sess.id, TradeSession.status == "armed")
            .values(status="running")
        ).rowcount
        if not claimed:
            return None
        db.expunge(sess)
        return sess


def _begin_firing(session_id: int) -> bool:
    """Atomically move a prepared session to "firing"; False if it was cancelled meanwhile."""
    with session_scope() as db:
        return bool(db.execute(
            update(TradeSession)
            .where(TradeSession.id == session_id, TradeSession.status == "running")
            .values(status="firing")
        ).rowcount)


def _still_on_autopilot(user_id: int, plan: list[PlannedOrder]) -> list[PlannedOrder]:
    with session_scope() as db:
        on = set(db.scalars(select(BrokerAccount.id).where(
            BrokerAccount.user_id == user_id, BrokerAccount.automation_enabled.is_(True))))
    return [p for p in plan if p.account_id in on]


def _finish_session(session_id: int, status: str, note: str | None = None) -> None:
    with session_scope() as db:
        sess = db.get(TradeSession, session_id)
        if sess is not None:
            sess.status = status
            sess.note = note


# ----------------------------------------------------------------------------- entry


def _market_check(user_id: int, fire_at: datetime | None) -> tuple[bool, bool]:
    """Live holiday check. Returns (closed, rechecked).

    `closed` is True only if the quotes clearly show NSE hasn't traded today. If
    the first look (a minute before entry) says closed, look again just after the
    open, so an ordinary trading day is never skipped by mistake.
    """
    clients = broker_manager.market_data_clients(user_id)
    if not clients or calendar.market_open_today(clients, now_ist()) is not False:
        return False, False
    if fire_at is not None:
        sleep_until(fire_at + MARKET_RECHECK)
    return calendar.market_open_today(clients, now_ist()) is False, True


def run_entry(
    user_id: int, trigger: str = "scheduled", fire_at: datetime | None = None, run_id: int | None = None
) -> int | None:
    """Buy the stocks confirmed with "Let's Trade" for today. Without a confirmation nothing happens."""
    today = now_ist().date()
    if trigger != "manual":
        if not is_trading_day(today):
            log.info("Entry skipped for user %s: not a trading day", user_id)
            return None
        if _already_ran(user_id, "entry", today):
            log.info("Entry already ran today for user %s", user_id)
            return None
    sess = _claim_session(user_id, today)
    if sess is None:
        if run_id is not None:
            _finish_run(run_id, [], note="Nothing was confirmed with Let's Trade for today")
        return run_id
    run_id = run_id or _start_run(user_id, "entry", trigger)
    try:
        items = [PlanItem(**d) for d in sess.items]
        plan = build_entry_plan(user_id, items, sess.sizing_mode, sess.reserve_pct)
        if not plan:
            _finish_run(run_id, [], note="Nothing to trade (no autopilot accounts)")
            _finish_session(sess.id, "done", "No autopilot accounts")
            return run_id
        closed, rechecked = _market_check(user_id, fire_at) if trigger == "scheduled" else (False, False)
        if closed:
            note = "Market closed today (detected automatically), nothing was traded"
            with session_scope() as db:
                if db.get(Holiday, today) is None:
                    db.add(Holiday(day=today, description="Market closed (detected)", source="detected"))
            _finish_run(run_id, [], note=note)
            _finish_session(sess.id, "skipped", note)
            return run_id
        if fire_at is not None:
            # After a market re-check the orders go out a little after the open.
            tolerance = ENTRY_LATE_TOLERANCE + (MARKET_RECHECK if rechecked else timedelta(0))
            if now_ist() > fire_at + tolerance:
                entries = [
                    LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell", "skipped",
                             p.trading_symbol, p.quantity or None, message="Entry window missed")
                    for p in plan
                ]
                _write_logs(user_id, run_id, entries)
                _finish_run(run_id, entries, note="Entry window missed")
                _finish_session(sess.id, "skipped", "Entry window missed")
                return run_id
            sleep_until(fire_at)
        # Last moment: was it cancelled, or were accounts switched off, during preparation?
        if not _begin_firing(sess.id):
            _finish_run(run_id, [], note="Cancelled before the buy, nothing was sent")
            return run_id
        plan = _still_on_autopilot(user_id, plan)
        entries = fire_plan(plan, user_id, run_id)
        _write_logs(user_id, run_id, _skip_entries(plan))  # sent orders were logged by fire_plan
        _finish_run(run_id, entries)
        _finish_session(sess.id, "done")
    except Exception as exc:  # noqa: BLE001
        log.exception("Entry run failed")
        entry = LogEntry(None, "—", "buy", "failed", message=f"Entry run crashed: {exc}")
        _write_logs(user_id, run_id, [entry])
        _finish_run(run_id, [entry])
        _finish_session(sess.id, "done", "Entry run crashed")
    return run_id


# ----------------------------------------------------------------------------- exit


def _in_scope(product: str, scope: str) -> bool:
    return scope != "intraday" or (product or "").upper() in INTRADAY_PRODUCTS


def square_off_account(acc: _Acc, scope: str) -> list[LogEntry]:
    entries: list[LogEntry] = []

    def call(fn):
        return broker_manager.call(acc.id, fn)

    for attempt in range(SQUAREOFF_ATTEMPTS):
        # 1. Cancel pending orders so nothing new opens and no exit is duplicated.
        try:
            orders = call(lambda b: b.orders())
        except Exception as exc:  # noqa: BLE001
            entries.append(LogEntry(acc.id, acc.name, "cancel", "failed", message=f"Could not read orders: {exc}"))
            orders = []
        pending = [o for o in orders if o.is_open and _in_scope(o.product, scope)]
        for o in pending:
            try:
                res = call(lambda b, oid=o.order_id: b.cancel_order(oid))
                entries.append(LogEntry(acc.id, acc.name, "cancel", "success" if res.ok else "failed",
                                        o.trading_symbol, o.quantity, o.order_id, res.message))
            except Exception as exc:  # noqa: BLE001
                entries.append(LogEntry(acc.id, acc.name, "cancel", "failed", o.trading_symbol, o.quantity,
                                        o.order_id, str(exc)))
        if pending:
            time.sleep(CANCEL_DELAY)

        # 2. Close every open position at market.
        try:
            positions: list[Position] = call(lambda b: b.positions())
        except Exception as exc:  # noqa: BLE001
            entries.append(LogEntry(acc.id, acc.name, "sell", "failed", message=f"Could not read positions: {exc}"))
            time.sleep(VERIFY_DELAY)
            continue
        targets = [p for p in positions if p.net_qty != 0 and _in_scope(p.product, scope)]
        if not targets:
            if attempt == 0:
                entries.append(LogEntry(acc.id, acc.name, "sell", "skipped", message="No open positions"))
            return entries

        def close(p: Position) -> LogEntry:
            side = "S" if p.net_qty > 0 else "B"
            action = "sell" if side == "S" else "buy"
            qty = abs(p.net_qty)
            try:
                res = call(
                    lambda b: b.place_market_order(p.exchange_segment, p.trading_symbol, side, qty, p.product or "MIS")
                )
                note = "" if attempt == 0 else f" (retry {attempt})"
                return LogEntry(acc.id, acc.name, action, "success" if res.ok else "failed", p.trading_symbol,
                                qty, res.order_id, (res.message or "") + note)
            except Exception as exc:  # noqa: BLE001
                return LogEntry(acc.id, acc.name, action, "failed", p.trading_symbol, qty, message=str(exc))

        with _pool(len(targets)) as ex:
            entries.extend(ex.map(close, targets))
        time.sleep(VERIFY_DELAY)

    # 3. Final check after all attempts.
    try:
        left = [p for p in call(lambda b: b.positions()) if p.net_qty != 0 and _in_scope(p.product, scope)]
    except Exception as exc:  # noqa: BLE001
        entries.append(LogEntry(acc.id, acc.name, "sell", "failed", message=f"Could not verify square-off: {exc}"))
        return entries
    for p in left:
        entries.append(LogEntry(acc.id, acc.name, "sell", "failed", p.trading_symbol, abs(p.net_qty),
                                message="Position still open after square-off attempts — check the Kotak Neo app"))
    return entries


def accounts_traded_today(user_id: int, day: date) -> set[int]:
    """Accounts where an entry run sent (or may have sent) at least one order on `day`."""
    with session_scope() as db:
        rows = db.scalars(
            select(OrderLog.account_id)
            .join(ExecutionRun, ExecutionRun.id == OrderLog.run_id)
            .where(
                ExecutionRun.user_id == user_id,
                ExecutionRun.kind == "entry",
                ExecutionRun.trading_date == day,
                OrderLog.status.in_(("success", "pending", UNKNOWN)),
                OrderLog.account_id.is_not(None),
            )
            .distinct()
        )
        return set(rows)


def _wait_for_entries(user_id: int, day: date) -> None:
    """Let a buy that is still being sent finish before deciding what to square off."""
    deadline = time.monotonic() + ENTRY_WAIT.total_seconds()
    while time.monotonic() < deadline:
        with session_scope() as db:
            busy = db.scalar(select(ExecutionRun.id).where(
                ExecutionRun.user_id == user_id, ExecutionRun.kind == "entry",
                ExecutionRun.trading_date == day, ExecutionRun.status == "running"))
        if busy is None:
            return
        time.sleep(0.5)


def run_exit(
    user_id: int, trigger: str = "scheduled", account_ids: list[int] | None = None, run_id: int | None = None
) -> int | None:
    today = now_ist().date()
    automatic = trigger in ("scheduled", "catchup")
    if automatic:
        if _already_ran(user_id, "exit", today):
            return None
        # The automatic square-off only touches accounts where TradeLab itself
        # bought today. No stocks given / nothing filled -> no orders at all,
        # and trades you placed yourself in the Kotak app are left alone. (A
        # holiday added by mistake after the entry can't block this either.)
        _wait_for_entries(user_id, today)
        account_ids = sorted(accounts_traded_today(user_id, today))
        if not account_ids:
            return None
    with session_scope() as db:
        scope = get_automation(db, user_id).squareoff_scope
        q = select(BrokerAccount).where(BrokerAccount.user_id == user_id)
        if account_ids is not None:
            q = q.where(BrokerAccount.id.in_(account_ids))
        if automatic:
            q = q.where(BrokerAccount.automation_enabled.is_(True))
        accounts = [_Acc(a.id, a.name, a.broker) for a in db.scalars(q)]
    if automatic and not accounts:
        return None
    run_id = run_id or _start_run(user_id, "exit", trigger)
    if not accounts:
        _finish_run(run_id, [], note="No accounts to square off")
        return run_id
    entries: list[LogEntry] = []
    # Never let two square-offs for the same user overlap: both would see the
    # same open position and the second would flip it into a fresh one.
    with _exit_lock(user_id):
        try:
            with _pool(len(accounts)) as ex:
                for result in ex.map(lambda a: square_off_account(a, scope), accounts):
                    entries.extend(result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Square-off run failed")
            entries.append(LogEntry(None, "—", "sell", "failed", message=f"Square-off crashed: {exc}"))
    _write_logs(user_id, run_id, entries)
    _finish_run(run_id, entries)
    try:
        from .reports import capture

        capture(user_id, [a.id for a in accounts])  # keep the day's P/L for the report
    except Exception:  # noqa: BLE001
        log.exception("Saving P/L after square-off failed")
    return run_id


class RunInProgress(Exception):
    pass


def _exit_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _exit_locks.setdefault(user_id, threading.Lock())


_exit_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def launch(kind: str, user_id: int, account_ids: list[int] | None = None, fire_at: datetime | None = None) -> int:
    """Start a manual entry / square-off in the background and return its run id."""
    with session_scope() as db:
        busy = db.scalar(
            select(ExecutionRun.id).where(
                ExecutionRun.user_id == user_id,
                ExecutionRun.kind == kind,
                ExecutionRun.status == "running",
                ExecutionRun.started_at > utcnow() - timedelta(minutes=10),
            )
        )
    if busy:
        raise RunInProgress("An identical run is already in progress — wait for it to finish.")
    run_id = _start_run(user_id, kind, "manual")
    if kind == "entry":
        target, args = run_entry, (user_id, "manual", fire_at, run_id)
    else:
        target, args = run_exit, (user_id, "manual", account_ids, run_id)
    threading.Thread(target=target, args=args, daemon=True, name=f"{kind}-{run_id}").start()
    return run_id


def broker_snapshot(account_id: int) -> dict:
    """Funds + positions summary for one account (used by dashboards)."""

    def fetch(b: Broker):
        funds = b.funds()
        positions = b.positions()
        return funds, positions

    funds, positions = broker_manager.call(account_id, fetch)
    open_pos = [p for p in positions if p.net_qty != 0]
    pnl = sum(p.pnl or 0 for p in positions)
    return {
        "available": funds.available,
        "margin_used": funds.margin_used,
        "open_positions": len(open_pos),
        "pnl": round(pnl, 2),
    }
