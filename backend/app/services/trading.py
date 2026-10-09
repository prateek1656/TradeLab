"""Buying and selling for every section, in every account at once.

Scheduled sections (Intraday, Holding) buy at their entry time after "Let's Trade";
Intraday also squares off at its exit time. F&O and Commodity buy and sell when the user
taps Buy or Sell. Sell only ever sells what that section itself bought.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..broker.base import Broker, Funds, Position
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
    SectionSettings,
    TradeSession,
    utcnow,
)
from ..money import inr
from . import calendar
from .scrips import scrip_master
from .sections import DEFAULT as INTRADAY, SECTIONS, SectionSpec, label, spec

log = logging.getLogger(__name__)

# Seconds to wait before checking that square-off orders went through (patched in tests).
VERIFY_DELAY = 2.0
CANCEL_DELAY = 1.0
# After the buy, look at each order book once this long later to catch late rejections.
REJECT_CHECK_DELAY = 2.0
SQUAREOFF_ATTEMPTS = 3
# An entry that could not start within this many seconds of its time is skipped.
ENTRY_LATE_TOLERANCE = timedelta(seconds=30)
# The entry job starts this early to log in and size orders, then fires on the dot.
ENTRY_LEAD = timedelta(seconds=60)
# If the market looks closed before the open, look again this long after it.
MARKET_RECHECK = timedelta(seconds=20)
# No new same-day buys this close to the square-off.
CLOSE_TO_EXIT = timedelta(minutes=5)
# A confirmation for today still waiting this long after its buy moment was missed
# (server restarted, buy time moved, ...): the user is asked to confirm again.
MISSED_AFTER = timedelta(minutes=2)
# Summary of runs a server restart cut short; they don't count as "already ran".
INTERRUPTED = "Interrupted (server restarted)"
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


def get_section(db, user_id: int, section: str) -> SectionSettings:
    """A user's settings for one section, created with defaults on first use.

    Intraday starts from the settings the app had before sections existed.
    """
    sp = spec(section)
    s = db.get(SectionSettings, (user_id, section))
    if s is not None:
        return s
    s = SectionSettings(user_id=user_id, section=section, entry_time=sp.entry_time, exit_time=sp.exit_time,
                        auto_exit=sp.has_exit, product=sp.products[0], sizing_mode="equal", reserve_pct=0.0,
                        max_lots=10, account_ids=None)
    if section == INTRADAY:
        legacy = get_automation(db, user_id)
        s.entry_time, s.exit_time, s.auto_exit = legacy.entry_time, legacy.exit_time, legacy.auto_exit
        s.sizing_mode = legacy.sizing_mode or "equal"
        s.reserve_pct = legacy.reserve_pct or 0.0
    try:
        with db.begin_nested():  # two requests may create it at the same moment
            db.add(s)
    except IntegrityError:
        s = db.get(SectionSettings, (user_id, section))
    return s


def section_accounts(user_id: int, section: str, db=None) -> list[int]:
    """Accounts a section trades in: autopilot on, and chosen for the section (all, unless limited)."""
    def load(db) -> list[int]:
        chosen = get_section(db, user_id, section).account_ids
        ids = db.scalars(select(BrokerAccount.id).where(
            BrokerAccount.user_id == user_id, BrokerAccount.automation_enabled.is_(True)))
        return sorted(i for i in ids if chosen is None or i in chosen)

    if db is not None:
        return load(db)
    with session_scope() as db:
        return load(db)


def section_info(db, user_id: int, section: str) -> dict:
    """A section's settings plus its fixed rules, for the app."""
    sp = spec(section)
    s = get_section(db, user_id, section)
    return {
        "section": section,
        "label": sp.label,
        "description": sp.description,
        "scheduled": sp.scheduled,
        "can_sell": sp.can_sell,
        "entry_time": s.entry_time if sp.scheduled else None,
        "exit_time": s.exit_time if sp.has_exit else None,
        "auto_exit": bool(s.auto_exit) if sp.has_exit else False,
        "has_exit": sp.has_exit,
        "product": s.product if s.product in sp.products else sp.products[0],
        "products": list(sp.products),
        "sides": list(sp.sides),
        "segments": list(sp.segments),
        "sizing_mode": s.sizing_mode,
        "reserve_pct": s.reserve_pct or 0.0,
        "max_lots": s.max_lots or 10,
        "lots": sp.lots,
        "account_ids": list(s.account_ids) if s.account_ids is not None else None,
        "opens": sp.opens.strftime("%H:%M"),
        "buy_until": sp.buy_until.strftime("%H:%M"),
        "sell_until": sp.sell_until.strftime("%H:%M"),
    }


def is_section_day(day: date, sp: SectionSpec) -> bool:
    """Weekdays; NSE sections also skip NSE holidays (MCX relies on the live market check)."""
    if day.weekday() >= 5:
        return False
    return sp.calendar != "nse" or holiday_name(day) is None


def next_section_day(day: date, sp: SectionSpec) -> date:
    for _ in range(60):
        day += timedelta(days=1)
        if is_section_day(day, sp):
            return day
    return day


def _cutoff(sett: SectionSettings, sp: SectionSpec) -> dtime:
    """After this time today's Let's Trade no longer applies to today."""
    if sp.has_exit and sett.exit_time:
        return parse_hms(sett.exit_time)
    return sp.buy_until


def plan_date(user_id: int, section: str = INTRADAY) -> date:
    """The trading day "Let's Trade" confirms right now for a section.

    Today until the section's square-off time (or the last buying time for Holding), so
    confirming during the session trades straight away; after that, the next trading day.
    """
    sp = spec(section)
    now = now_ist()
    with session_scope() as db:
        cutoff = _cutoff(get_section(db, user_id, section), sp)
    if is_section_day(now.date(), sp) and now.time().replace(tzinfo=None) < cutoff:
        return now.date()
    return next_section_day(now.date(), sp)


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
    # Skipped because the account's money doesn't cover even one share (or lot).
    funds_short: bool = False
    product: str = "MIS"
    lots: int | None = None  # derivatives: number of lots (quantity is in units)


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
    funds_short: bool = False  # not enough money in the account for this order
    exchange_segment: str | None = None
    product: str | None = None


# Broker rejections that mean "not enough money". Kotak's RMS says e.g. "RMS:Margin Exceeds,
# Required:…, Available:…" or "Insufficient balance."; it also says "exceeds limit" (price or
# quantity limits), "Insufficient holding quantity" and "circuit limit", which are NOT about money.
FUNDS_REJECTION = re.compile(
    r"margin\s+exceed|insufficient\s+(?:fund|balance|margin)|insufficient\s+(?:cash|limit)|"
    r"(?:fund|margin)s?\s+shortfall|shortfall\s+(?:of\s+)?(?:fund|margin)|additional\s+margin\s+required|"
    r"not\s+enough\s+(?:fund|margin|balance|money)",
    re.IGNORECASE,
)
NOT_FUNDS = re.compile(r"holding|circuit|price\s+band|freeze", re.IGNORECASE)


def is_funds_rejection(message: str | None) -> bool:
    return bool(message) and bool(FUNDS_REJECTION.search(message)) and not NOT_FUNDS.search(message)


def _write_logs(user_id: int, run_id: int | None, entries: list[LogEntry], section: str | None = None) -> None:
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
                    exchange_segment=e.exchange_segment,
                    product=e.product,
                    section=section,
                )
            )


def _start_run(user_id: int, kind: str, trigger: str, section: str = INTRADAY) -> int:
    with session_scope() as db:
        run = ExecutionRun(user_id=user_id, kind=kind, section=section, trigger=trigger,
                           trading_date=now_ist().date())
        db.add(run)
        db.flush()
        return run.id


def funds_short_accounts(entries: list[LogEntry]) -> list[str]:
    """Names of accounts with at least one buy that couldn't go through for lack of money."""
    names: list[str] = []
    for e in entries:
        if e.action not in ("buy", "sell") or e.account_id is None:
            continue
        short = e.funds_short or (e.status in ("failed", "skipped") and is_funds_rejection(e.message))
        if short and e.account_name not in names:
            names.append(e.account_name)
    return names


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
            if run.kind == "entry":
                run.funds_short = funds_short_accounts(entries) or None


def _already_ran(user_id: int, kind: str, day: date, section: str = INTRADAY) -> bool:
    with session_scope() as db:
        return (
            db.scalar(
                select(ExecutionRun.id).where(
                    ExecutionRun.user_id == user_id,
                    ExecutionRun.kind == kind,
                    ExecutionRun.section == section,
                    ExecutionRun.trading_date == day,
                    ExecutionRun.trigger.in_(("scheduled", "catchup")),
                    ExecutionRun.summary.is_distinct_from(INTERRUPTED),
                )
            )
            is not None
        )


def _pool(n: int) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=max(1, min(get_settings().max_parallel_orders, n)))


# Kotak allows about 10 requests a second per API user; stay safely below it per account.
ORDERS_PER_SECOND = 8


class _Pacer:
    """At most `rate` calls in any rolling second (thread-safe)."""

    def __init__(self, rate: int):
        self.rate = rate
        self.lock = threading.Lock()
        self.stamps: deque[float] = deque()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.stamps and now - self.stamps[0] >= 1.0:
                    self.stamps.popleft()
                if len(self.stamps) < self.rate:
                    self.stamps.append(now)
                    return
                pause = 1.0 - (now - self.stamps[0])
            time.sleep(max(pause, 0.005))


_pacers: dict[int, _Pacer] = {}
_pacers_guard = threading.Lock()


def _pace(account_id: int) -> None:
    with _pacers_guard:
        pacer = _pacers.get(account_id)
        if pacer is None or pacer.rate != ORDERS_PER_SECOND:
            pacer = _pacers[account_id] = _Pacer(ORDERS_PER_SECOND)
    pacer.wait()


def _interleave(tasks: list) -> list:
    """Order tasks round-robin by account, so every account's first orders go out first."""
    seen: dict[int, int] = {}
    keyed = []
    for i, t in enumerate(tasks):
        n = seen.get(t.account_id, 0)
        seen[t.account_id] = n + 1
        keyed.append((n, i, t))
    return [t for _, _, t in sorted(keyed, key=lambda k: (k[0], k[1]))]


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


NOT_ENOUGH = "Not enough money: "  # prefix of every skip caused by lack of funds


def _qty_for_budget(budget: float, price: float | None) -> tuple[int, str | None]:
    if not price or price <= 0:
        return 0, NO_PRICE
    # Tiny epsilon so float noise (e.g. 39.9999999) doesn't cost a whole share.
    qty = int(math.floor(budget / price + 1e-9))
    if qty < 1:
        return 0, f"{NOT_ENOUGH}{inr(budget, 0)} is less than the price of one share ({inr(price)})"
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
    # Derivatives
    lot_size: int = 1
    expiry: str | None = None  # ISO date
    instrument_type: str | None = None
    option_type: str | None = None
    strike: float | None = None

    @classmethod
    def from_item(cls, it: BasketItem) -> PlanItem:
        return cls(
            symbol=it.symbol, trading_symbol=it.trading_symbol, exchange_segment=it.exchange_segment,
            side=it.side, sizing_mode=it.sizing_mode, sizing_value=it.sizing_value, token=it.token,
            name=it.name, account_ids=list(it.account_ids) if it.account_ids else None,
            lot_size=it.lot_size or 1, expiry=it.expiry.isoformat() if it.expiry else None,
            instrument_type=it.instrument_type, option_type=it.option_type, strike=it.strike,
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


def _size_equal(budget: float | None, parts: int, price: float | None) -> tuple[int, str | None, str | None]:
    """Split the money free in this account equally across the items; buy as many shares as fit."""
    if budget is None:
        return 0, NO_FUNDS, None
    if budget <= 0:
        return 0, f"{NOT_ENOUGH}this account has {inr(max(budget, 0))} available", None
    share = budget / parts
    qty, reason = _qty_for_budget(share, price)
    note = f"{inr(share, 0)} allotted (1/{parts} of {inr(budget, 0)}) at ≈{inr(price or 0)}"
    return qty, reason, None if reason else note


NO_MARGIN = "Could not get the margin needed for one lot from the broker"


def _size_lots(budget: float | None, parts: int, margin: float | None, max_lots: int,
               custom_lots: int | None) -> tuple[int, str | None, str | None]:
    """Lots for a derivatives order: an equal part of the free money over the margin of one lot.

    A fixed number of lots (custom sizing) is still capped by the money free and by `max_lots`.
    """
    if budget is None:
        return 0, NO_FUNDS, None
    if not margin or margin <= 0:
        return 0, NO_MARGIN, None
    if budget <= 0:
        return 0, f"{NOT_ENOUGH}this account has {inr(max(budget, 0))} available", None
    share = budget if custom_lots is not None else budget / parts
    fits = int(math.floor(share / margin + 1e-9))
    if fits < 1:
        return 0, f"{NOT_ENOUGH}{inr(share, 0)} is less than the margin for one lot ({inr(margin)})", None
    wanted = custom_lots if custom_lots is not None else fits
    lots = min(wanted, fits, max_lots)
    if custom_lots is not None:
        note = f"{lots} lot{'s' if lots != 1 else ''} (fixed {custom_lots}), {inr(margin, 0)} per lot"
        if lots < custom_lots:
            note += " — capped by the money free" if fits < min(custom_lots, max_lots) else f" — capped at {max_lots}"
        return lots, None, note
    note = (f"{inr(share, 0)} allotted (1/{parts} of {inr(budget, 0)}), {inr(margin, 0)} per lot"
            + (f" — capped at {max_lots} lots" if fits > max_lots else ""))
    return lots, None, note


# Delivery (CNC) buys are paid in cash, with a little room for charges and price moves.
CNC_HEADROOM = 0.015


def free_money(funds: Funds | None, product: str, reserve_pct: float = 0.0) -> float | None:
    """Money this account can put into a new trade right now, after the keep-aside %.

    Margin products (MIS, NRML) can use everything Kotak shows as available, including margin
    from pledged shares; delivery (CNC) buys need cash, so pledge margin is left out.
    """
    if funds is None:
        return None
    free = funds.available
    if product == "CNC":
        free = (free - max(funds.collateral, 0.0)) * (1 - CNC_HEADROOM)
    keep = max(0.0, min(100.0, reserve_pct or 0.0))
    return free * (1 - keep / 100)


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


def order_units(lots: int, lot_size: int) -> int:
    """Kotak takes derivatives quantities in units: lots x the contract's lot size (NSE, BSE and MCX)."""
    return lots * max(1, int(lot_size or 1))


def _accounts(user_id: int, account_ids: list[int] | None) -> list[_Acc]:
    """`account_ids` (all if None) that still have autopilot on."""
    with session_scope() as db:
        q = select(BrokerAccount).where(BrokerAccount.user_id == user_id, BrokerAccount.automation_enabled.is_(True))
        if account_ids is not None:
            q = q.where(BrokerAccount.id.in_(account_ids))
        return [_Acc(a.id, a.name, a.broker) for a in db.scalars(q.order_by(BrokerAccount.id))]


def build_entry_plan(
    user_id: int,
    items: list[PlanItem],
    sizing_mode: str,
    reserve_pct: float = 0.0,
    all_prices: bool = False,
    account_ids: list[int] | None = None,
    section: str = INTRADAY,
    product: str | None = None,
    max_lots: int = 10,
) -> list[PlannedOrder]:
    """Buy orders for a section's `items` in each of `account_ids` (default: the section's accounts).

    Nothing is sent. The money free in each account right now (less the keep-aside %) is
    what gets split, so sections never need a share of their own: each one uses whatever
    is free when it trades.
    """
    sp = spec(section)
    product = product or sp.products[0]
    if account_ids is None:
        account_ids = section_accounts(user_id, section)
    accounts = _accounts(user_id, account_ids)
    if not accounts or not items:
        return []
    equal = sizing_mode == "equal"
    prices = _prices(user_id, items, everything=equal or all_prices or sp.lots)
    margins = _lot_margins(user_id, items, product, prices, [a.id for a in accounts]) if sp.lots else {}

    def applies(item: PlanItem, acc: _Acc) -> bool:
        return not item.account_ids or acc.id in item.account_ids

    def prepare(acc: _Acc) -> tuple[_Acc, Funds | None, str | None]:
        if not any(applies(i, acc) for i in items):
            return acc, None, None
        try:
            broker_manager.ensure_session(acc.id)
        except Exception as exc:  # noqa: BLE001
            return acc, None, f"Login failed: {exc}"
        funds = None
        try:
            funds = broker_manager.call(acc.id, lambda b: b.funds())
        except Exception as exc:  # noqa: BLE001
            log.warning("Funds unavailable for %s: %s", acc.name, exc)
        return acc, funds, None

    with _pool(len(accounts)) as ex:
        prepared = list(ex.map(prepare, accounts))

    plan: list[PlannedOrder] = []
    for acc, funds, err in prepared:
        mine = [it for it in items if applies(it, acc)]
        budget = free_money(funds, product, reserve_pct)
        for it in mine:
            price = prices.get(it.trading_symbol)
            if price is None and acc.broker == "paper":
                price = synthetic_price(it.trading_symbol)
            lots = None
            if sp.lots:
                custom = int(it.sizing_value) if not equal else None
                margin = margins.get(it.trading_symbol)
                if margin is None and acc.broker == "paper" and price:
                    margin = _paper_margin(it, price, product)
                lots, reason, note = _size_lots(budget, len(mine), margin, max_lots, custom)
                qty = order_units(lots, it.lot_size) if lots else 0
            elif equal:
                qty, reason, note = _size_equal(budget, len(mine), price)
            else:
                qty, reason, note = _size_custom(it, price, None if funds is None else free_money(funds, product))
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
                    error=bool(err) or reason in (NO_PRICE, NO_FUNDS, NO_MARGIN),
                    funds_short=not err and bool(reason) and reason.startswith(NOT_ENOUGH),
                    product=product,
                    lots=lots,
                )
            )
    return plan


def _paper_margin(item: PlanItem, price: float, product: str) -> float:
    from ..broker.paper import estimate_margin

    return estimate_margin(item.trading_symbol, price, order_units(1, item.lot_size), "B", product)


def _lot_margins(user_id: int, items: list[PlanItem], product: str, prices: dict[str, float],
                 account_ids: list[int]) -> dict[str, float]:
    """Margin Kotak needs for one lot of each contract, by trading symbol.

    Asked once per contract from the first Kotak account that answers (the margin for a plain
    buy is the same in every account). Paper accounts use their own estimate.
    """
    out: dict[str, float] = {}
    with session_scope() as db:
        ids = list(db.scalars(select(BrokerAccount.id).where(
            BrokerAccount.user_id == user_id, BrokerAccount.broker == "kotak", BrokerAccount.id.in_(account_ids)
        ).order_by(BrokerAccount.id)))
    for it in items:
        price = prices.get(it.trading_symbol)
        if it.trading_symbol in out or not price:
            continue
        for acc_id in ids:
            try:
                m = broker_manager.call(acc_id, lambda b, it=it, price=price: b.margin_per_lot(
                    it.exchange_segment, it.token or "", it.trading_symbol, "B", product,
                    order_units(1, it.lot_size), price))
            except Exception as exc:  # noqa: BLE001
                log.warning("Margin for %s unavailable from account %s: %s", it.trading_symbol, acc_id, exc)
                continue
            if m and m >= 1:  # a reply of 0 (or a few paise) is not a real margin
                out[it.trading_symbol] = m
                break
            log.warning("Ignoring odd margin %s for %s from account %s", m, it.trading_symbol, acc_id)
    return out


UNKNOWN = "unknown"  # the broker may or may not have the order (e.g. the reply timed out)


def _skip_entries(plan: list[PlannedOrder]) -> list[LogEntry]:
    return [
        LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell",
                 "failed" if p.error else "skipped", p.trading_symbol, p.quantity or None, message=p.skip,
                 funds_short=p.funds_short, exchange_segment=p.exchange_segment, product=p.product)
        for p in plan
        if p.skip
    ]


def _reserve_logs(user_id: int, run_id: int, tasks: list[PlannedOrder], section: str) -> list[int]:
    """Record every order as "pending" *before* sending it, so a crash or lost reply
    can never hide an order from the square-off."""
    with session_scope() as db:
        rows = [
            OrderLog(user_id=user_id, run_id=run_id, account_id=p.account_id, account_name=p.account_name,
                     action="buy" if p.side == "B" else "sell", symbol=p.trading_symbol, quantity=p.quantity,
                     status="pending", message="Sending…", exchange_segment=p.exchange_segment, product=p.product,
                     section=section)
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


def fire_plan(plan: list[PlannedOrder], user_id: int | None = None, run_id: int | None = None,
              section: str = INTRADAY) -> list[LogEntry]:
    """Send every planned order at once. Orders are logged as pending first when a run is given."""
    entries = _skip_entries(plan)
    tasks = _interleave([p for p in plan if not p.skip and p.quantity > 0])
    if not tasks:
        return entries
    ids = _reserve_logs(user_id, run_id, tasks, section) if run_id is not None else []
    after = " This account is still squared off." if spec(section).has_exit else ""

    def place(p: PlannedOrder) -> LogEntry:
        action = "buy" if p.side == "B" else "sell"
        _pace(p.account_id)
        try:
            res = broker_manager.call(
                p.account_id,
                lambda b: b.place_market_order(p.exchange_segment, p.trading_symbol, p.side, p.quantity, p.product),
            )
            message = f"{res.message} · {p.note}" if p.note and res.ok else res.message
            return LogEntry(p.account_id, p.account_name, action, "success" if res.ok else "failed",
                            p.trading_symbol, p.quantity, res.order_id, message,
                            exchange_segment=p.exchange_segment, product=p.product)
        except Exception as exc:  # noqa: BLE001
            return LogEntry(p.account_id, p.account_name, action, UNKNOWN, p.trading_symbol, p.quantity,
                            message=f"No reply from the broker ({exc}). The order may have gone through — "
                                    f"check the Kotak app.{after}",
                            exchange_segment=p.exchange_segment, product=p.product)

    with _pool(len(tasks)) as ex:
        results = list(ex.map(place, tasks))
    results = _catch_rejections(results)
    if ids:
        _settle_logs(ids, results)
    return entries + results


def _catch_rejections(results: list[LogEntry]) -> list[LogEntry]:
    """Kotak can accept an order and reject it a moment later (for example RMS: not enough
    margin). Read each account's order book once so the log, the summary and the funds
    alert show what really happened."""
    placed = [e for e in results if e.status == "success" and e.order_id]
    if not placed:
        return results
    time.sleep(REJECT_CHECK_DELAY)
    accounts = sorted({e.account_id for e in placed})

    def book(acc_id: int):
        try:
            return acc_id, {o.order_id: o for o in broker_manager.call(acc_id, lambda b: b.orders())}
        except Exception as exc:  # noqa: BLE001 - the orders were sent; this check is best effort
            log.info("Could not re-check orders of account %s: %s", acc_id, exc)
            return acc_id, {}

    with _pool(len(accounts)) as ex:
        books = dict(ex.map(book, accounts))
    for e in placed:
        order = books.get(e.account_id, {}).get(e.order_id)
        if order is not None and (order.status or "").lower().startswith("reject"):
            e.status = "failed"
            e.message = f"Rejected by the broker: {order.message or 'no reason given'}"
            e.funds_short = is_funds_rejection(order.message)
    return results


# ----------------------------------------------------------------------------- Let's Trade


class SessionError(Exception):
    """A "Let's Trade" request that can't be accepted (message is shown to the user)."""


@dataclass
class Live:
    """A section's current list (switched-on items), money settings and accounts."""

    items: list[PlanItem]
    sizing_mode: str
    reserve_pct: float
    product: str
    max_lots: int
    accounts: list[int]


def live_items(user_id: int, section: str = INTRADAY) -> Live:
    sp = spec(section)
    with session_scope() as db:
        s = get_section(db, user_id, section)
        items = [
            PlanItem.from_item(it)
            for it in db.scalars(
                select(BasketItem)
                .where(BasketItem.user_id == user_id, BasketItem.section == section, BasketItem.active.is_(True))
                .order_by(BasketItem.created_at, BasketItem.id)
            )
        ]
        product = s.product if s.product in sp.products else sp.products[0]
        return Live(items, s.sizing_mode, s.reserve_pct or 0.0, product, s.max_lots or 10,
                    section_accounts(user_id, section, db))


def _entry_moment(user_id: int, day: date, section: str = INTRADAY) -> datetime:
    with session_scope() as db:
        return datetime.combine(day, parse_hms(get_section(db, user_id, section).entry_time), tzinfo=IST)


def _exit_moment(user_id: int, day: date, section: str = INTRADAY) -> datetime:
    """The square-off moment, or for a section without one (Holding) the last moment to buy."""
    sp = spec(section)
    with session_scope() as db:
        cutoff = _cutoff(get_section(db, user_id, section), sp)
    return datetime.combine(day, cutoff, tzinfo=IST)


def _entry_busy(user_id: int, section: str = INTRADAY) -> bool:
    with session_scope() as db:
        return db.scalar(select(ExecutionRun.id).where(
            ExecutionRun.user_id == user_id,
            ExecutionRun.kind == "entry",
            ExecutionRun.section == section,
            ExecutionRun.status == "running",
            ExecutionRun.started_at > utcnow() - timedelta(minutes=10),
        )) is not None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _noun(section: str) -> str:
    return {"intraday": "stock", "holding": "ETF"}.get(section, "contract")


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _live_dict(live: Live) -> dict:
    return {
        "mode": live.sizing_mode, "reserve": live.reserve_pct, "product": live.product, "max_lots": live.max_lots,
        "accounts": live.accounts, "items": sorted((i.to_dict() for i in live.items), key=lambda d: d["trading_symbol"]),
    }


def _fingerprint(section: str, day: date, buys_now: bool, entry_at: datetime, live: Live) -> str:
    """Everything the "Are you sure?" dialog shows; confirming checks it hasn't changed since."""
    return _digest({"section": section, "day": day.isoformat(), "now": buys_now, "at": entry_at.isoformat(),
                    **_live_dict(live)})


def _expired(live: Live, day: date) -> list[str]:
    return [i.symbol for i in live.items if i.expiry and date.fromisoformat(str(i.expiry)) < day]


def _session_q(user_id: int, section: str, day: date):
    return select(TradeSession).where(
        TradeSession.user_id == user_id, TradeSession.section == section, TradeSession.trade_date == day)


def session_state(user_id: int, section: str = INTRADAY) -> dict:
    """What "Let's Trade" would do right now for a section, and whether its next day is confirmed."""
    sp = spec(section)
    day = plan_date(user_id, section)
    now = now_ist()
    live = live_items(user_id, section)
    accounts = live.accounts
    entry_at = _entry_moment(user_id, day, section)
    exit_at = _exit_moment(user_id, day, section)
    buys_now = day == now.date() and now >= entry_at - ENTRY_LEAD
    with session_scope() as db:
        sess = db.scalar(_session_q(user_id, section, day))
        current = None
        if sess is not None:
            now_items = sorted((i.to_dict() for i in live.items), key=lambda d: d["trading_symbol"])
            confirmed = sorted(sess.items, key=lambda d: d["trading_symbol"])
            status = sess.status
            if (status == "armed" and day == now.date()
                    and now > max(entry_at, _aware(sess.confirmed_at)) + MISSED_AFTER
                    and not _entry_busy(user_id, section)):
                status = "missed"  # nothing picked it up at the buy moment; confirming again buys now
            current = {
                "status": status,
                "confirmed_at": _aware(sess.confirmed_at).isoformat(),
                "stocks": [i["symbol"] for i in sess.items],
                "sizing_mode": sess.sizing_mode,
                "reserve_pct": sess.reserve_pct,
                "changed": now_items != confirmed or live.sizing_mode != sess.sizing_mode
                or live.reserve_pct != (sess.reserve_pct or 0.0) or live.product != sess.product
                or (sess.account_ids is not None and sorted(sess.account_ids) != accounts),
                "note": sess.note if status != "missed" else
                f"The {entry_at:%H:%M} trade didn't run (the server was restarted or the time changed). "
                "Nothing was traded.",
            }
    with session_scope() as db:
        others = [
            d.isoformat()
            for d in db.scalars(
                select(TradeSession.trade_date).where(
                    TradeSession.user_id == user_id,
                    TradeSession.section == section,
                    TradeSession.status == "armed",
                    TradeSession.trade_date != day,
                    TradeSession.trade_date >= now.date(),
                ).order_by(TradeSession.trade_date)
            )
            if is_section_day(d, sp)
        ]
    return {
        "section": section,
        "label": sp.label,
        "trade_date": day.isoformat(),
        "entry_at": entry_at.isoformat(),
        "exit_at": exit_at.isoformat() if sp.has_exit else None,
        # Other days still confirmed (e.g. after a holiday was removed); they trade on their date.
        "other_confirmed": others,
        "closing_soon": day == now.date() and now >= exit_at - CLOSE_TO_EXIT,
        # Confirming now places orders straight away (the entry moment has passed or is a minute away).
        "buys_now": buys_now,
        "stocks": [i.symbol for i in live.items],
        "shorts": [i.symbol for i in live.items if i.side == "S"],
        "expired": _expired(live, day),
        "sizing_mode": live.sizing_mode,
        "reserve_pct": live.reserve_pct,
        "product": live.product,
        "lots": sp.lots,
        "autopilot_accounts": len(accounts),
        "account_ids": accounts,
        # Sent back with "Yes" so exactly what the dialog showed is confirmed.
        "fingerprint": _fingerprint(section, day, buys_now, entry_at, live),
        "session": current,
    }


def arm(user_id: int, trade_date: date, fingerprint: str | None = None, section: str = INTRADAY) -> dict:
    """Confirm ("Let's Trade") a section's next trading day with a snapshot of its list and accounts."""
    sp = spec(section)
    if not sp.scheduled:
        raise SessionError(f"{sp.label} buys when you tap Buy; there is nothing to confirm in advance.")
    day = plan_date(user_id, section)
    if trade_date != day:
        raise SessionError(f"The trading day is now {day:%a, %d %b}. Check the details and confirm again.")
    live = live_items(user_id, section)
    if not live.items:
        raise SessionError(f"Add at least one {_noun(section)} (and switch it on) before you trade.")
    expired = _expired(live, day)
    if expired:
        raise SessionError(f"These contracts have expired: {', '.join(expired)}. Remove them first.")
    accounts = live.accounts
    if not accounts:
        raise SessionError(f"Switch on autopilot for at least one account (and choose it for {sp.label}) first.")
    now = now_ist()
    exit_at = _exit_moment(user_id, day, section)
    too_late = ("Too close to today's square-off. Confirm again after it, for the next trading day."
                if sp.has_exit else "Too close to today's market close. Confirm again for the next trading day.")
    if day == now.date() and now >= exit_at - CLOSE_TO_EXIT:
        raise SessionError(too_late)
    entry_at = _entry_moment(user_id, day, section)
    buys_now = day == now.date() and now >= entry_at - ENTRY_LEAD
    if fingerprint is not None and fingerprint != _fingerprint(section, day, buys_now, entry_at, live):
        raise SessionError("Something changed since this was shown (items, accounts, money or the time). "
                           "Check the details and confirm again.")
    if buys_now and _entry_busy(user_id, section):
        raise RunInProgress("The previous trade is still finishing. Try again in a few seconds.")
    with session_scope() as db:
        sess = db.scalar(_session_q(user_id, section, day))
        if sess is not None and sess.status in ("running", "firing", "done"):
            raise SessionError(f"{sp.label} already traded this day. You can confirm the next day after it.")
        if sess is None:
            sess = TradeSession(user_id=user_id, section=section, trade_date=day)
            db.add(sess)
        sess.status = "armed"
        sess.items = [i.to_dict() for i in live.items]
        sess.sizing_mode = live.sizing_mode
        sess.reserve_pct = live.reserve_pct
        sess.product = live.product
        sess.account_ids = accounts
        sess.confirmed_at = utcnow()
        sess.note = None
    run_id = None
    if buys_now:
        # The scheduled job has already started (or passed): trade from here.
        try:
            run_id = launch("entry", user_id, fire_at=max(entry_at, now), section=section)
        except RunInProgress:
            with session_scope() as db:
                db.execute(update(TradeSession).where(
                    TradeSession.user_id == user_id, TradeSession.section == section,
                    TradeSession.trade_date == day, TradeSession.status == "armed",
                ).values(status="cancelled", note="Not confirmed: the previous trade was still finishing"))
            raise
    return {"run_id": run_id, **session_state(user_id, section)}


def cancel_session(user_id: int, trade_date: date | None = None, section: str = INTRADAY) -> dict:
    """Withdraw a "Let's Trade" confirmation. Works until the orders actually go out."""
    day = trade_date or plan_date(user_id, section)
    with session_scope() as db:
        cancelled = db.execute(
            update(TradeSession)
            .where(
                TradeSession.user_id == user_id,
                TradeSession.section == section,
                TradeSession.trade_date == day,
                TradeSession.status.in_(("armed", "running")),
            )
            .values(status="cancelled", note="Cancelled by you")
        ).rowcount
        if not cancelled:
            status = db.scalar(select(TradeSession.status).where(
                TradeSession.user_id == user_id, TradeSession.section == section, TradeSession.trade_date == day))
            raise SessionError("Nothing to cancel." if status in (None, "cancelled", "skipped")
                               else "Too late: the orders have already been sent.")
    return session_state(user_id, section)


def _claim_session(user_id: int, day: date, section: str = INTRADAY) -> TradeSession | None:
    """Atomically take the confirmed session for `day` so it can only be traded once."""
    with session_scope() as db:
        sess = db.scalar(_session_q(user_id, section, day))
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


def _market_check(user_id: int, fire_at: datetime | None, items: list[PlanItem] | None = None,
                  section: str = INTRADAY) -> tuple[bool, bool]:
    """Live holiday check. Returns (closed, rechecked).

    `closed` is True only if the quotes clearly show NSE hasn't traded today (scheduled
    sections all trade on NSE). If the first look (a minute
    before entry) says closed, look again just after the open, so an ordinary trading
    day is never skipped by mistake.
    """
    clients = broker_manager.market_data_clients(user_id)

    def look():
        return calendar.market_open_today(clients, now_ist())

    if not clients or look() is not False:
        return False, False
    if fire_at is not None:
        sleep_until(fire_at + MARKET_RECHECK)
    return look() is False, True


def _add_detected_holiday(day: date) -> None:
    try:
        with session_scope() as db:
            if db.get(Holiday, day) is None:
                db.add(Holiday(day=day, description="Market closed (detected)", source="detected"))
    except IntegrityError:
        pass  # another user's entry recorded it at the same moment


def run_entry(
    user_id: int, trigger: str = "scheduled", fire_at: datetime | None = None, run_id: int | None = None,
    section: str = INTRADAY,
) -> int | None:
    """Trade a section's items confirmed with "Let's Trade" for today. Without a confirmation nothing happens."""
    sp = spec(section)
    today = now_ist().date()
    if trigger != "manual" and not is_section_day(today, sp):
        log.info("%s entry skipped for user %s: not a trading day", sp.label, user_id)
        return None
    # Claiming the confirmed session is atomic, so a day can never be traded twice.
    sess = _claim_session(user_id, today, section)
    if sess is None:
        if run_id is not None:
            _finish_run(run_id, [], note="Nothing was confirmed with Let's Trade for today")
        return run_id
    run_id = run_id or _start_run(user_id, "entry", trigger, section)
    try:
        items = [PlanItem(**d) for d in sess.items]
        with session_scope() as db:
            max_lots = get_section(db, user_id, section).max_lots or 10
        plan = build_entry_plan(user_id, items, sess.sizing_mode, sess.reserve_pct or 0.0,
                                account_ids=sess.account_ids, section=section, product=sess.product,
                                max_lots=max_lots)
        if not plan:
            _finish_run(run_id, [], note="Nothing to trade (no autopilot accounts)")
            _finish_session(sess.id, "done", "No autopilot accounts")
            return run_id
        # Live holiday check for every entry, including "buy now" confirmations.
        closed, rechecked = _market_check(user_id, fire_at, items, section)
        if closed:
            note = "Market closed today (detected automatically), nothing was traded"
            if sp.calendar == "nse":
                _add_detected_holiday(today)
            _finish_run(run_id, [], note=note)
            _finish_session(sess.id, "skipped", note)
            return run_id
        if fire_at is not None:
            # After a market re-check the orders go out a little after the open.
            tolerance = ENTRY_LATE_TOLERANCE + (MARKET_RECHECK if rechecked else timedelta(0))
            if now_ist() > fire_at + tolerance:
                entries = [
                    LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell", "skipped",
                             p.trading_symbol, p.quantity or None, message="Entry window missed",
                             exchange_segment=p.exchange_segment, product=p.product)
                    for p in plan
                ]
                _write_logs(user_id, run_id, entries, section)
                _finish_run(run_id, entries, note="Entry window missed")
                _finish_session(sess.id, "skipped", "Entry window missed")
                return run_id
            sleep_until(fire_at)
        # Last moment: did today become a holiday, was it cancelled, or were accounts switched off?
        if not is_section_day(today, sp):
            note = "Today was marked as a market holiday, nothing was traded"
            _finish_run(run_id, [], note=note)
            _finish_session(sess.id, "skipped", note)
            return run_id
        if not _begin_firing(sess.id):
            _finish_run(run_id, [], note="Cancelled before the buy, nothing was sent")
            return run_id
        plan = _still_on_autopilot(user_id, plan)
        entries = fire_plan(plan, user_id, run_id, section)
        _write_logs(user_id, run_id, _skip_entries(plan), section)  # sent orders were logged by fire_plan
        _finish_run(run_id, entries)
        _finish_session(sess.id, "done")
    except Exception as exc:  # noqa: BLE001
        log.exception("Entry run failed")
        entry = LogEntry(None, "—", "buy", "failed", message=f"Entry run crashed: {exc}")
        _write_logs(user_id, run_id, [entry], section)
        _finish_run(run_id, [entry])
        _finish_session(sess.id, "done", "Entry run crashed")
    return run_id


# ----------------------------------------------------------------------------- exit


ALL = "all"  # the panic button: square off every section at once


@dataclass
class Scope:
    """What a square-off may close. Only intraday-type (MIS) positions, ever: delivery
    (CNC), MTF and carry-forward (NRML) positions are never touched by a square-off.
    TradeLab's own NRML positions are closed separately, by quantity, from its order records.
    """

    section: str  # INTRADAY (stock MIS positions) or ALL (MIS positions in every segment)

    def covers(self, segment: str, product: str, symbol: str = "") -> bool:
        if (product or "").upper() not in INTRADAY_PRODUCTS:
            return False
        return self.section == ALL or (segment or "") in SECTIONS[INTRADAY].segments


def square_off_account(acc: _Acc, scope: Scope) -> list[LogEntry]:
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
        pending = [o for o in orders if o.is_open and scope.covers(o.exchange_segment, o.product, o.trading_symbol)]
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
        targets = [p for p in positions if p.net_qty != 0 and scope.covers(p.exchange_segment, p.product,
                                                                            p.trading_symbol)]
        if not targets:
            if attempt == 0:
                entries.append(LogEntry(acc.id, acc.name, "sell", "skipped", message="No open positions"))
            return entries

        def close(p: Position) -> LogEntry:
            side = "S" if p.net_qty > 0 else "B"
            action = "sell" if side == "S" else "buy"
            qty = abs(p.net_qty)
            _pace(acc.id)
            try:
                res = call(
                    lambda b: b.place_market_order(p.exchange_segment, p.trading_symbol, side, qty, p.product or "MIS")
                )
                note = "" if attempt == 0 else f" (retry {attempt})"
                return LogEntry(acc.id, acc.name, action, "success" if res.ok else "failed", p.trading_symbol,
                                qty, res.order_id, (res.message or "") + note,
                                exchange_segment=p.exchange_segment, product=p.product)
            except Exception as exc:  # noqa: BLE001
                return LogEntry(acc.id, acc.name, action, "failed", p.trading_symbol, qty, message=str(exc),
                                exchange_segment=p.exchange_segment, product=p.product)

        with _pool(len(targets)) as ex:
            entries.extend(ex.map(close, targets))
        time.sleep(VERIFY_DELAY)

    # 3. Final check after all attempts.
    try:
        left = [p for p in call(lambda b: b.positions())
                if p.net_qty != 0 and scope.covers(p.exchange_segment, p.product, p.trading_symbol)]
    except Exception as exc:  # noqa: BLE001
        entries.append(LogEntry(acc.id, acc.name, "sell", "failed", message=f"Could not verify square-off: {exc}"))
        return entries
    for p in left:
        entries.append(LogEntry(acc.id, acc.name, "sell", "failed", p.trading_symbol, abs(p.net_qty),
                                message="Position still open after square-off attempts — check the Kotak Neo app"))
    return entries


def _traded_orders(user_id: int, day: date, section: str | None):
    q = (
        select(OrderLog.account_id, OrderLog.symbol)
        .join(ExecutionRun, ExecutionRun.id == OrderLog.run_id)
        .where(
            ExecutionRun.user_id == user_id,
            ExecutionRun.kind == "entry",
            ExecutionRun.trading_date == day,
            OrderLog.status.in_(("success", "pending", UNKNOWN)),
            OrderLog.account_id.is_not(None),
        )
    )
    if section is not None:
        q = q.where(ExecutionRun.section == section)
    with session_scope() as db:
        return db.execute(q.distinct()).all()


def accounts_traded_today(user_id: int, day: date, section: str | None = None) -> set[int]:
    """Accounts where an entry run (of `section`, or any) sent or may have sent an order on `day`."""
    return {acc for acc, _ in _traded_orders(user_id, day, section)}


def _wait_for_entries(user_id: int, day: date, section: str) -> None:
    """Let a trade that is still being sent finish before deciding what to square off."""
    deadline = time.monotonic() + ENTRY_WAIT.total_seconds()
    while time.monotonic() < deadline:
        with session_scope() as db:
            busy = db.scalar(select(ExecutionRun.id).where(
                ExecutionRun.user_id == user_id, ExecutionRun.kind == "entry", ExecutionRun.section == section,
                ExecutionRun.trading_date == day, ExecutionRun.status == "running"))
        if busy is None:
            return
        time.sleep(0.5)


def _cancel_armed_today(user_id: int) -> int:
    """The panic button also withdraws today's confirmations that haven't bought yet."""
    with session_scope() as db:
        return db.execute(
            update(TradeSession)
            .where(TradeSession.user_id == user_id, TradeSession.trade_date == now_ist().date(),
                   TradeSession.status == "armed")
            .values(status="cancelled", note="Cancelled by “Square off everything”")
        ).rowcount


def run_exit(
    user_id: int, trigger: str = "scheduled", account_ids: list[int] | None = None, run_id: int | None = None,
    section: str = INTRADAY,
) -> int | None:
    """Square off Intraday (automatically at its exit time, or now), or everything (ALL, the panic button).

    ALL closes every intraday-type (MIS) position in every segment plus the carry-forward
    (NRML) contracts F&O and Commodity bought; delivery holdings are never sold by it.
    """
    today = now_ist().date()
    automatic = trigger in ("scheduled", "catchup")
    if section != ALL and not spec(section).has_exit:
        return None
    if automatic:
        if section == ALL or _already_ran(user_id, "exit", today, section):
            return None
        # The automatic square-off only touches accounts where this section itself
        # traded today. Nothing confirmed / nothing filled -> no orders at all, and
        # trades you placed yourself in the Kotak app are left alone. (A holiday
        # added by mistake after the entry can't block this either.)
        _wait_for_entries(user_id, today, section)
        account_ids = sorted(accounts_traded_today(user_id, today, section))
        if not account_ids:
            return None
    with session_scope() as db:
        q = select(BrokerAccount).where(BrokerAccount.user_id == user_id)
        if account_ids is not None:
            q = q.where(BrokerAccount.id.in_(account_ids))
        # (Automatic runs include accounts whose autopilot was switched off after the buy.)
        accounts = [_Acc(a.id, a.name, a.broker) for a in db.scalars(q.order_by(BrokerAccount.id))]
    if automatic and not accounts:
        return None
    run_id = run_id or _start_run(user_id, "exit", trigger, section)
    note = None
    if section == ALL and account_ids is None and _cancel_armed_today(user_id):
        note = "Today's Let's Trade confirmations were cancelled"
    if not accounts:
        _finish_run(run_id, [], note="No accounts to square off")
        return run_id
    entries: list[LogEntry] = []
    # Never let two square-offs for the same user overlap: both would see the
    # same open position and the second would flip it into a fresh one.
    with _exit_lock(user_id):
        try:
            with _pool(len(accounts)) as ex:
                for result in ex.map(lambda a: square_off_account(a, Scope(section)), accounts):
                    entries.extend(result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Square-off run failed")
            entries.append(LogEntry(None, "—", "sell", "failed", message=f"Square-off crashed: {exc}"))
        _write_logs(user_id, run_id, entries, INTRADAY if section == INTRADAY else None)
        if section == ALL:
            # TradeLab's own carry-forward contracts, sold by the quantity it bought.
            for key in ("fno", "commodity"):
                try:
                    plan = build_sell_plan(user_id, key, account_ids=[a.id for a in accounts], products={"NRML"})
                    entries += fire_plan([p for p in plan if not p.skip], user_id, run_id, key)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Selling %s contracts failed", key)
                    entries.append(LogEntry(None, "—", "sell", "failed", message=f"{label(key)} sell crashed: {exc}"))
    _finish_run(run_id, entries, note=note)
    try:
        from .reports import capture

        capture(user_id, [a.id for a in accounts])  # keep the day's P/L for the report
    except Exception:  # noqa: BLE001
        log.exception("Saving P/L after square-off failed")
    return run_id


# ----------------------------------------------------------------------------- what TradeLab holds


@dataclass(frozen=True)
class Lot:
    """A position key: account, segment, trading symbol and product."""

    account_id: int
    exchange_segment: str
    trading_symbol: str
    product: str


def tradelab_open(user_id: int, section: str, account_ids: list[int] | None = None,
                  symbols: set[str] | None = None, products: set[str] | None = None) -> dict[Lot, int]:
    """Units each account should still hold from what `section` bought: its buys minus its sells.

    Counted from TradeLab's own order records, so it never includes anything bought by hand
    in the Kotak app. A buy counts only once the broker accepted it; a sell counts as soon
    as it may have gone through (so an unclear reply never leads to selling twice).
    Intraday (MIS) products count only today's orders, since they can't be carried over.
    """
    day_start = datetime.combine(now_ist().date(), dtime(0, 0), tzinfo=IST).astimezone(timezone.utc)
    q = select(OrderLog.account_id, OrderLog.exchange_segment, OrderLog.symbol, OrderLog.product, OrderLog.action,
               OrderLog.status, OrderLog.quantity, OrderLog.created_at).where(
        OrderLog.user_id == user_id, OrderLog.section == section, OrderLog.account_id.is_not(None),
        OrderLog.action.in_(("buy", "sell")), OrderLog.quantity > 0,
    )
    if account_ids is not None:
        q = q.where(OrderLog.account_id.in_(account_ids))
    out: dict[Lot, int] = {}
    with session_scope() as db:
        rows = db.execute(q).all()
    for acc, seg, sym, prod, action, status, qty, created in rows:
        prod = (prod or "").upper()
        if not seg or not sym or (symbols is not None and sym not in symbols):
            continue
        if products is not None and prod not in products:
            continue
        if prod in INTRADAY_PRODUCTS and _aware(created) < day_start:
            continue
        if action == "buy" and status == "success":
            delta = qty
        elif action == "sell" and status in ("success", UNKNOWN, "pending"):
            delta = -qty
        else:
            continue
        key = Lot(acc, seg, sym, prod)
        out[key] = out.get(key, 0) + delta
    return {k: v for k, v in out.items() if v > 0}


def broker_held(acc_id: int, wanted: list[Lot]) -> dict[Lot, int]:
    """What the broker says each account can sell right now for these keys.

    Delivery (CNC) units: sellable demat holdings plus units bought today (still in positions).
    Anything else: the open (long) position.
    """
    out: dict[Lot, int] = {}
    positions = broker_manager.call(acc_id, lambda b: b.positions())
    net: dict[tuple[str, str, str], int] = {}
    for p in positions:
        k = (p.exchange_segment, p.trading_symbol, (p.product or "").upper())
        net[k] = net.get(k, 0) + p.net_qty
    holdings: dict[str, int] = {}
    if any(w.product == "CNC" for w in wanted):
        for h in broker_manager.call(acc_id, lambda b: b.holdings()):
            holdings[h.trading_symbol] = holdings.get(h.trading_symbol, 0) + h.sellable_qty
    for w in wanted:
        have = max(net.get((w.exchange_segment, w.trading_symbol, w.product), 0), 0)
        if w.product == "CNC":
            have += holdings.get(w.trading_symbol, 0)
        out[w] = have
    return out


def _item_info(user_id: int, section: str) -> dict[str, tuple[str, int]]:
    """Display name and lot size of the section's items, by trading symbol."""
    with session_scope() as db:
        return {it.trading_symbol: (it.symbol, it.lot_size or 1) for it in db.scalars(
            select(BasketItem).where(BasketItem.user_id == user_id, BasketItem.section == section))}


def build_sell_plan(user_id: int, section: str, symbols: set[str] | None = None,
                    account_ids: list[int] | None = None, products: set[str] | None = None) -> list[PlannedOrder]:
    """Sell orders for what `section` bought and still holds (all of it, or only `symbols`).

    Each order sells the smaller of what TradeLab bought and what the broker shows, so a
    family member's own shares or contracts are never sold.
    """
    sp = spec(section)
    chosen = section_accounts(user_id, section) if account_ids is None else account_ids
    ledger = tradelab_open(user_id, section, None, symbols, products)
    info = _item_info(user_id, section)
    by_acc: dict[int, list[Lot]] = {}
    for key in ledger:
        by_acc.setdefault(key.account_id, []).append(key)
    accounts = {a.id: a for a in _all_accounts(user_id)}
    plan: list[PlannedOrder] = []

    def order(acc: _Acc, key: Lot, qty: int, skip: str | None = None, note: str | None = None,
              error: bool = False) -> PlannedOrder:
        name, lot = info.get(key.trading_symbol, (key.trading_symbol, 1))
        return PlannedOrder(account_id=acc.id, account_name=acc.name, symbol=name, trading_symbol=key.trading_symbol,
                            exchange_segment=key.exchange_segment, side="S", quantity=qty, skip=skip, note=note,
                            error=error, product=key.product, lots=qty // lot if sp.lots and lot else None)

    def prepare(acc_id: int) -> list[PlannedOrder]:
        acc = accounts.get(acc_id)
        keys = sorted(by_acc[acc_id], key=lambda k: k.trading_symbol)
        if acc is None:
            return []
        if acc_id not in chosen:
            return [order(acc, k, ledger[k], skip=f"Not sold: this account isn't trading in {sp.label} "
                                                  "(autopilot off or not chosen)") for k in keys]
        try:
            broker_manager.ensure_session(acc_id)
            have = broker_held(acc_id, keys)
        except Exception as exc:  # noqa: BLE001
            return [order(acc, k, ledger[k], skip=f"Could not read this account's positions: {exc}", error=True)
                    for k in keys]
        out = []
        for k in keys:
            qty = min(ledger[k], have.get(k, 0))
            if qty <= 0:
                out.append(order(acc, k, 0, skip="Nothing left to sell (already sold or closed)"))
            else:
                extra = f" of {ledger[k]} bought by TradeLab" if qty < ledger[k] else ""
                out.append(order(acc, k, qty, note=f"Sell {qty}{extra}"))
        return out

    if by_acc:
        with _pool(len(by_acc)) as ex:
            for rows in ex.map(prepare, sorted(by_acc)):
                plan.extend(rows)
    return plan


def _all_accounts(user_id: int) -> list[_Acc]:
    with session_scope() as db:
        return [_Acc(a.id, a.name, a.broker) for a in db.scalars(
            select(BrokerAccount).where(BrokerAccount.user_id == user_id).order_by(BrokerAccount.id))]


def section_positions(user_id: int, section: str) -> dict:
    """What `section` bought and still holds, per account, with the latest prices."""
    sp = spec(section)
    ledger = tradelab_open(user_id, section)
    info = _item_info(user_id, section)
    accounts = {a.id: a for a in _all_accounts(user_id)}
    by_acc: dict[int, list[Lot]] = {}
    for key in ledger:
        by_acc.setdefault(key.account_id, []).append(key)

    def read(acc_id: int) -> tuple[int, dict, str | None]:
        try:
            positions = broker_manager.call(acc_id, lambda b: b.positions())
            holdings = broker_manager.call(acc_id, lambda b: b.holdings()) if sp.products[0] == "CNC" else []
            return acc_id, {"positions": positions, "holdings": holdings}, None
        except Exception as exc:  # noqa: BLE001
            return acc_id, {}, str(exc)

    rows = []
    errors = []
    if by_acc:
        with _pool(len(by_acc)) as ex:
            results = list(ex.map(read, sorted(by_acc)))
    else:
        results = []
    for acc_id, data, err in results:
        acc = accounts.get(acc_id)
        if acc is None:
            continue
        if err:
            errors.append({"account_id": acc_id, "account_name": acc.name, "error": err})
            continue
        for key in sorted(by_acc[acc_id], key=lambda k: k.trading_symbol):
            pos = [p for p in data["positions"] if p.trading_symbol == key.trading_symbol
                   and p.exchange_segment == key.exchange_segment and (p.product or "").upper() == key.product]
            hold = [h for h in data["holdings"] if h.trading_symbol == key.trading_symbol]
            have = sum(max(p.net_qty, 0) for p in pos) + sum(h.quantity for h in hold)
            qty = min(ledger[key], have)
            if qty <= 0:
                continue
            avg = next((h.avg_price for h in hold if h.avg_price), None) or next(
                (p.buy_avg for p in pos if p.buy_avg), None)
            ltp = next((p.ltp for p in pos if p.ltp), None) or next((h.ltp for h in hold if h.ltp), None)
            mult = next((p.multiplier for p in pos if p.multiplier), 1.0) or 1.0
            name, lot = info.get(key.trading_symbol, (key.trading_symbol, 1))
            token = next((p.token for p in pos if p.token), None) or next((h.token for h in hold if h.token), None)
            rows.append({
                "account_id": acc_id, "account_name": acc.name, "symbol": name, "trading_symbol": key.trading_symbol,
                "exchange_segment": key.exchange_segment, "product": key.product, "quantity": qty,
                "lots": qty // lot if sp.lots and lot else None, "avg_price": avg, "ltp": ltp,
                "multiplier": mult, "token": token,
            })
    _fill_prices(user_id, rows)
    for r in rows:
        r["value"] = round(r["quantity"] * r["ltp"] * r["multiplier"], 2) if r["ltp"] else None
        r["pnl"] = (round(r["quantity"] * (r["ltp"] - r["avg_price"]) * r["multiplier"], 2)
                    if r["ltp"] and r["avg_price"] else None)
        r.pop("token", None)
    return {"section": section, "rows": rows, "errors": errors,
            "value": round(sum(r["value"] or 0 for r in rows), 2),
            "pnl": round(sum(r["pnl"] or 0 for r in rows), 2)}


def _fill_prices(user_id: int, rows: list[dict]) -> None:
    """Latest prices for rows whose broker data carried none (holdings show yesterday's close)."""
    wanted = {}
    for r in rows:
        tok = r.get("token") or scrip_master.token_for(r["trading_symbol"])
        if tok:
            wanted[(r["exchange_segment"], str(tok))] = r
    if not wanted:
        return
    for client in broker_manager.market_data_clients(user_id):
        try:
            prices = client.ltp(list(wanted))
        except Exception as exc:  # noqa: BLE001
            log.info("No live prices for holdings: %s", exc)
            continue
        for (seg, tok), r in wanted.items():
            if prices.get(tok):
                for row in rows:
                    if row["trading_symbol"] == r["trading_symbol"] and row["exchange_segment"] == seg:
                        row["ltp"] = prices[tok]
        return


# ----------------------------------------------------------------------------- Buy / Sell on command


def trade_window_error(section: str, action: str, now: datetime | None = None) -> str | None:
    """Why a Buy or Sell can't be sent right now (None if it can)."""
    sp = spec(section)
    now = now or now_ist()
    t = now.time().replace(tzinfo=None)
    until = sp.buy_until if action == "buy" else sp.sell_until
    hours = f"{sp.opens:%H:%M}–{until:%H:%M}"
    if not is_section_day(now.date(), sp):
        why = holiday_name(now.date()) if now.weekday() < 5 else "weekend"
        return f"The market is closed today ({why}). {sp.label} can {action} on trading days, {hours}."
    if t < sp.opens:
        return f"{sp.label} opens at {sp.opens:%H:%M}. You can {action} {hours}."
    if t >= until:
        return f"Too late to {action} in {sp.label} today (allowed {hours}). Try again on the next trading day."
    return None


def _command_fingerprint(section: str, action: str, live: Live | None, symbols: set[str] | None,
                         accounts: list[int]) -> str:
    return _digest({"section": section, "action": action, "symbols": sorted(symbols) if symbols else None,
                    "accounts": accounts, **(_live_dict(live) if live else {})})


def _order_dict(p: PlannedOrder) -> dict:
    return {"account_id": p.account_id, "account_name": p.account_name, "symbol": p.symbol,
            "trading_symbol": p.trading_symbol, "side": p.side, "quantity": p.quantity, "lots": p.lots,
            "price": p.price, "product": p.product, "note": p.note, "skip": p.skip, "funds_short": p.funds_short}


def command_preview(user_id: int, section: str, action: str, symbols: set[str] | None = None) -> dict:
    """What Buy or Sell would send right now (nothing is sent), for the "Are you sure?" dialog."""
    sp = spec(section)
    if action == "buy":
        if sp.scheduled:
            raise SessionError(f"{sp.label} buys with Let's Trade.")
        live = live_items(user_id, section)
        items = [i for i in live.items if not symbols or i.trading_symbol in symbols]
        plan = build_entry_plan(user_id, items, live.sizing_mode, live.reserve_pct, all_prices=True,
                                account_ids=live.accounts, section=section, product=live.product,
                                max_lots=live.max_lots)
        fp = _command_fingerprint(section, "buy", live, symbols, live.accounts)
        expired = _expired(live, now_ist().date())
    else:
        if not sp.can_sell:
            raise SessionError(f"{sp.label} is squared off instead.")
        accounts = section_accounts(user_id, section)
        plan = build_sell_plan(user_id, section, symbols)
        fp = _command_fingerprint(section, "sell", None, symbols, accounts)
        expired = []
    return {"section": section, "action": action, "fingerprint": fp, "blocked": trade_window_error(section, action),
            "expired": expired, "orders": [_order_dict(p) for p in plan]}


def buy_now(user_id: int, section: str, fingerprint: str | None, symbols: set[str] | None = None) -> int:
    """The user tapped Buy and said "Yes": buy in every account of the section, straight away."""
    sp = spec(section)
    if sp.scheduled:
        raise SessionError(f"{sp.label} buys with Let's Trade.")
    blocked = trade_window_error(section, "buy")
    if blocked:
        raise SessionError(blocked)
    live = live_items(user_id, section)
    items = [i for i in live.items if not symbols or i.trading_symbol in symbols]
    if not items:
        raise SessionError("Add at least one contract (and switch it on) before you buy.")
    expired = _expired(Live(items, live.sizing_mode, live.reserve_pct, live.product, live.max_lots, []),
                       now_ist().date())
    if expired:
        raise SessionError(f"These contracts have expired: {', '.join(expired)}. Remove them first.")
    if not live.accounts:
        raise SessionError(f"Switch on autopilot for at least one account (and choose it for {sp.label}) first.")
    if fingerprint is not None and fingerprint != _command_fingerprint(section, "buy", live, symbols, live.accounts):
        raise SessionError("Something changed since this was shown (items, accounts or settings). "
                           "Check the details and try again.")
    return launch("entry", user_id, section=section, trigger="command",
                  job=lambda run_id: _run_buy(user_id, run_id, section, items, live))


def sell_now(user_id: int, section: str, fingerprint: str | None, symbols: set[str] | None = None) -> int:
    """The user tapped Sell and said "Yes": sell what the section bought, in every account."""
    sp = spec(section)
    if not sp.can_sell:
        raise SessionError(f"{sp.label} is squared off instead.")
    blocked = trade_window_error(section, "sell")
    if blocked:
        raise SessionError(blocked)
    accounts = section_accounts(user_id, section)
    if fingerprint is not None and fingerprint != _command_fingerprint(section, "sell", None, symbols, accounts):
        raise SessionError("Something changed since this was shown (accounts). Check the details and try again.")
    return launch("exit", user_id, section=section, trigger="command",
                  job=lambda run_id: _run_sell(user_id, run_id, section, symbols))


def _run_buy(user_id: int, run_id: int, section: str, items: list[PlanItem], live: Live) -> None:
    try:
        plan = build_entry_plan(user_id, items, live.sizing_mode, live.reserve_pct, account_ids=live.accounts,
                                section=section, product=live.product, max_lots=live.max_lots)
        blocked = trade_window_error(section, "buy")
        if blocked:
            _finish_run(run_id, [], note=f"Nothing was sent: {blocked}")
            return
        if not plan:
            _finish_run(run_id, [], note="Nothing to buy (no accounts)")
            return
        entries = fire_plan(plan, user_id, run_id, section)
        _write_logs(user_id, run_id, _skip_entries(plan), section)
        _finish_run(run_id, entries)
    except Exception as exc:  # noqa: BLE001
        log.exception("Buy run failed")
        entry = LogEntry(None, "—", "buy", "failed", message=f"Buy crashed: {exc}")
        _write_logs(user_id, run_id, [entry], section)
        _finish_run(run_id, [entry])


def _run_sell(user_id: int, run_id: int, section: str, symbols: set[str] | None) -> None:
    try:
        _wait_for_entries(user_id, now_ist().date(), section)
        with _exit_lock(user_id):
            plan = build_sell_plan(user_id, section, symbols)
            if not plan:
                _finish_run(run_id, [], note=f"{label(section)} holds nothing bought by TradeLab")
                return
            entries = fire_plan(plan, user_id, run_id, section)
            _write_logs(user_id, run_id, _skip_entries(plan), section)
        _finish_run(run_id, entries)
        try:
            from .reports import capture

            capture(user_id, sorted({p.account_id for p in plan}))
        except Exception:  # noqa: BLE001
            log.exception("Saving P/L after selling failed")
    except Exception as exc:  # noqa: BLE001
        log.exception("Sell run failed")
        entry = LogEntry(None, "—", "sell", "failed", message=f"Sell crashed: {exc}")
        _write_logs(user_id, run_id, [entry], section)
        _finish_run(run_id, [entry])


class RunInProgress(Exception):
    pass


def _exit_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _exit_locks.setdefault(user_id, threading.Lock())


_exit_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def recover_after_restart() -> None:
    """Runs and confirmations a previous server process left half-done."""
    with session_scope() as db:
        db.execute(update(ExecutionRun).where(ExecutionRun.status == "running")
                   .values(status="failed", summary=INTERRUPTED, finished_at=utcnow()))
        db.execute(update(TradeSession).where(TradeSession.status == "running")
                   .values(status="skipped", note="Interrupted before any order was sent (server restarted)"))
        db.execute(update(TradeSession).where(TradeSession.status == "firing")
                   .values(status="done", note="The server restarted while sending orders. Check Activity and "
                                               "the Kotak app; these accounts are still squared off."))


def launch(kind: str, user_id: int, account_ids: list[int] | None = None, fire_at: datetime | None = None,
           section: str = INTRADAY, trigger: str = "manual", job=None) -> int:
    """Start a run in the background and return its run id.

    Two runs of the same kind and section never overlap; Buy and Sell of an on-command
    section never overlap each other either.
    """
    with session_scope() as db:
        q = select(ExecutionRun.id).where(
            ExecutionRun.user_id == user_id,
            ExecutionRun.status == "running",
            ExecutionRun.started_at > utcnow() - timedelta(minutes=10),
        )
        if section == ALL or (kind == "exit" and section == INTRADAY):
            q = q.where(ExecutionRun.kind == "exit", ExecutionRun.section.in_((ALL, INTRADAY)))
        elif spec(section).scheduled:
            q = q.where(ExecutionRun.kind == kind, ExecutionRun.section == section)
        else:  # Buy and Sell of an on-command section, or a panic square-off selling its contracts
            q = q.where((ExecutionRun.section == section) | ((ExecutionRun.kind == "exit") & (ExecutionRun.section == ALL)))
        busy = db.scalar(q)
    if busy:
        raise RunInProgress("A trade for this section is still being sent — wait a few seconds and try again.")
    run_id = _start_run(user_id, kind, trigger, section)
    if job is not None:
        target, args = job, (run_id,)
    elif kind == "entry":
        target, args = run_entry, (user_id, "manual", fire_at, run_id, section)
    else:
        target, args = run_exit, (user_id, "manual", account_ids, run_id, section)
    threading.Thread(target=target, args=args, daemon=True, name=f"{kind}-{run_id}").start()
    return run_id


EQUITY_SEGMENTS = ("nse_cm", "bse_cm")


def with_live_pnl(account_id: int, positions: list[Position]) -> list[Position]:
    """Fill in P/L for open positions reported without a price (Kotak's positions carry none).

    Uses one quote request for the account's open positions. If quotes fail, the
    positions are returned unchanged (P/L stays unknown for them).
    """
    wanted = []
    for p in positions:
        if p.net_qty and p.pnl is None:
            tok = p.token or (scrip_master.token_for(p.trading_symbol) if p.exchange_segment in EQUITY_SEGMENTS
                              else None)
            if tok:
                wanted.append((p, str(tok)))
    if not wanted:
        return positions
    try:
        prices = broker_manager.call(account_id, lambda b: b.ltp([(p.exchange_segment, tok) for p, tok in wanted]))
    except Exception as exc:  # noqa: BLE001
        log.info("No live prices for open positions of account %s: %s", account_id, exc)
        return positions
    for p, tok in wanted:
        price = prices.get(tok)
        if price:
            p.ltp = price
            mult = p.multiplier or 1.0
            p.pnl = round((p.sell_avg * p.sell_qty - p.buy_avg * p.buy_qty + p.net_qty * price) * mult, 2)
    return positions


def broker_snapshot(account_id: int) -> dict:
    """Funds + trading summary for one account (used by dashboards).

    Delivery (CNC) positions are left out of the P/L and open count: Holding units are kept,
    not traded, and the Holding tab shows their value separately.
    """

    def fetch(b: Broker):
        funds = b.funds()
        positions = b.positions()
        return funds, positions

    funds, positions = broker_manager.call(account_id, fetch)
    positions = [p for p in positions if (p.product or "").upper() != "CNC"]
    positions = with_live_pnl(account_id, positions)
    open_pos = [p for p in positions if p.net_qty != 0]
    pnl = sum(p.pnl or 0 for p in positions)
    return {
        "available": funds.available,
        "margin_used": funds.margin_used,
        "open_positions": len(open_pos),
        "pnl": round(pnl, 2),
    }
