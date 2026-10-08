"""Morning entry (market orders at the entry time) and the afternoon hard square-off."""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import select

from ..broker.base import Broker, Position
from ..broker.manager import broker_manager
from ..broker.paper import synthetic_price
from ..config import IST, get_settings
from ..db import session_scope
from ..models import AutomationSettings, BasketItem, BrokerAccount, ExecutionRun, Holiday, OrderLog, utcnow
from ..money import inr
from .scrips import scrip_master

log = logging.getLogger(__name__)

# Seconds to wait before checking that square-off orders went through (patched in tests).
VERIFY_DELAY = 2.0
CANCEL_DELAY = 1.0
SQUAREOFF_ATTEMPTS = 3
# An entry that could not start within this many seconds of its time is skipped.
ENTRY_LATE_TOLERANCE = timedelta(seconds=30)
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
    """The trading day that stocks added right now are for.

    Today until today's square-off time (so the morning list and any manual
    buy during the session use it); after that, the next trading day.
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
    failed = sum(1 for e in entries if e.status == "failed")
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


def _size_custom(item: BasketItem, price: float | None, funds: float | None) -> tuple[int, str | None, str | None]:
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


def _prices(user_id: int, items: list[BasketItem], everything: bool) -> dict[str, float]:
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


def build_entry_plan(user_id: int, for_date: date | None = None, all_prices: bool = False) -> list[PlannedOrder]:
    """Orders for the stocks listed for `for_date` (default: today). Nothing is sent."""
    for_date = for_date or now_ist().date()
    with session_scope() as db:
        settings = get_automation(db, user_id)
        equal, reserve_pct = settings.sizing_mode == "equal", settings.reserve_pct or 0.0
        accounts = [
            _Acc(a.id, a.name, a.broker)
            for a in db.scalars(
                select(BrokerAccount).where(
                    BrokerAccount.user_id == user_id, BrokerAccount.automation_enabled.is_(True)
                )
            )
        ]
        items = list(
            db.scalars(
                select(BasketItem)
                .where(
                    BasketItem.user_id == user_id,
                    BasketItem.active.is_(True),
                    BasketItem.trade_date == for_date,
                )
                .order_by(BasketItem.created_at, BasketItem.id)
            )
        )
    if not accounts or not items:
        return []

    prices = _prices(user_id, items, everything=equal or all_prices)

    def applies(item: BasketItem, acc: _Acc) -> bool:
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


def fire_plan(plan: list[PlannedOrder]) -> list[LogEntry]:
    entries = [
        LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell",
                 "failed" if p.error else "skipped", p.trading_symbol, p.quantity or None, message=p.skip)
        for p in plan
        if p.skip
    ]
    tasks = [p for p in plan if not p.skip]
    if not tasks:
        return entries

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
            return LogEntry(p.account_id, p.account_name, action, "failed", p.trading_symbol, p.quantity,
                            message=str(exc))

    with _pool(len(tasks)) as ex:
        entries.extend(ex.map(place, tasks))
    return entries


def run_entry(
    user_id: int, trigger: str = "scheduled", fire_at: datetime | None = None, run_id: int | None = None
) -> int | None:
    """Prepare orders, wait for `fire_at` (if given), then place them all at once."""
    today = now_ist().date()
    if trigger != "manual":
        if not is_trading_day(today):
            log.info("Entry skipped for user %s: not a trading day", user_id)
            return None
        if _already_ran(user_id, "entry", today):
            log.info("Entry already ran today for user %s", user_id)
            return None
    run_id = run_id or _start_run(user_id, "entry", trigger)
    try:
        plan = build_entry_plan(user_id)
        if not plan:
            _finish_run(run_id, [], note="Nothing to trade (no stocks for today or no autopilot accounts)")
            return run_id
        if fire_at is not None:
            if now_ist() > fire_at + ENTRY_LATE_TOLERANCE:
                entries = [
                    LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell", "skipped",
                             p.trading_symbol, p.quantity or None, message="Entry window missed")
                    for p in plan
                ]
                _write_logs(user_id, run_id, entries)
                _finish_run(run_id, entries, note="Entry window missed")
                return run_id
            sleep_until(fire_at)
        entries = fire_plan(plan)
        _write_logs(user_id, run_id, entries)
        _finish_run(run_id, entries)
    except Exception as exc:  # noqa: BLE001
        log.exception("Entry run failed")
        entry = LogEntry(None, "—", "buy", "failed", message=f"Entry run crashed: {exc}")
        _write_logs(user_id, run_id, [entry])
        _finish_run(run_id, [entry])
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
    """Accounts where an entry run (automatic or manual) placed at least one order on `day`."""
    with session_scope() as db:
        rows = db.scalars(
            select(OrderLog.account_id)
            .join(ExecutionRun, ExecutionRun.id == OrderLog.run_id)
            .where(
                ExecutionRun.user_id == user_id,
                ExecutionRun.kind == "entry",
                ExecutionRun.trading_date == day,
                OrderLog.status == "success",
                OrderLog.account_id.is_not(None),
            )
            .distinct()
        )
        return set(rows)


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
    return run_id


class RunInProgress(Exception):
    pass


def _exit_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _exit_locks.setdefault(user_id, threading.Lock())


_exit_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def launch(kind: str, user_id: int, account_ids: list[int] | None = None) -> int:
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
        target, args = run_entry, (user_id, "manual", None, run_id)
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
