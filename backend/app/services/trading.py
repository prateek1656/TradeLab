"""Buying and selling for every section, in every account at once.

Intraday is the only section that works by the clock: it buys at its entry time after
"Let's Trade" and squares off at its exit time. Holding, F&O and Commodity buy and sell only
when the user taps Buy or Sell. Sell only ever sells what that section itself bought.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, timezone

from ..broker.base import Broker, Funds, LoginFailed, Order, Position, SessionExpired
from ..broker.manager import broker_manager
from ..config import IST, get_settings
from ..db import IntegrityError, collection, database, session_scope
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
NIGHT_ENDS = dtime(9, 0)  # records aren't written down before this (Kotak's start of day is done by then)
CANCEL_CHECKS = 3  # looks at the order book for a cancel Kotak accepted to show there
# After sending, look at the order books after each of these pauses until every order has finished
# (filled, rejected, cancelled): Kotak can accept an order and reject it a moment later.
FINAL_CHECKS = (2.0, 3.0, 5.0)
SQUAREOFF_ATTEMPTS = 5
# An on-command Buy that took longer than this to prepare (Kotak slow) is not sent: the market has moved
# since the user said yes.
COMMAND_DEADLINE = timedelta(seconds=90)
# A failed automatic square-off is tried again this often, until the section's last selling time.
EXIT_RETRY = timedelta(seconds=60)
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
# What a square-off closes: MIS, the only intraday product TradeLab trades (cover and bracket orders
# can't be closed through the API, and their stop-loss legs must not be cancelled), on the segments
# the API trades.
SQUAREOFF_PRODUCTS = {"MIS"}
API_SEGMENTS = {"nse_cm", "bse_cm", "nse_fo", "bse_fo", "mcx_fo"}


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
        # "removed": an NSE holiday the owner took out, kept so the daily NSE download doesn't bring it back.
        return h.description if h and h.source != "removed" else None


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and holiday_name(day) is None


def _create_once(db, record):  # noqa: ANN001, ANN202
    """Save a settings record with defaults unless it exists already (two requests may create it at
    the same moment: both then get the one saved first). Saves the session first, like a claim."""
    cls = type(record)
    doc = record._to_doc()
    db.update_one(cls, {"_id": doc.pop("_id")}, set_on_insert=doc, upsert=True)
    key = tuple(getattr(record, k) for k in cls.__key__)
    return db.get(cls, key[0] if len(key) == 1 else key)


def get_automation(db, user_id: int) -> AutomationSettings:
    s = db.get(AutomationSettings, user_id)
    if s is None:
        s = _create_once(db, AutomationSettings(user_id=user_id))
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
        _fit_times(s, sp)
        return s
    s = SectionSettings(user_id=user_id, section=section, entry_time=sp.entry_time, exit_time=sp.exit_time,
                        auto_exit=sp.has_exit, product=sp.products[0], sizing_mode="equal", reserve_pct=0.0,
                        max_lots=10, account_ids=None)
    if section == INTRADAY:
        legacy = get_automation(db, user_id)
        s.entry_time, s.exit_time, s.auto_exit = legacy.entry_time, legacy.exit_time, legacy.auto_exit
        s.sizing_mode = legacy.sizing_mode or "equal"
        s.reserve_pct = legacy.reserve_pct or 0.0
    _fit_times(s, sp)
    return _create_once(db, s)


def _fit_times(s: SectionSettings, sp: SectionSpec) -> None:
    """Keep a timed section's buy and square-off inside its limits.

    Older versions allowed a later Intraday square-off; Kotak squares intraday positions off itself
    at 15:05, so TradeLab's must come first (15:00 at the latest). Such times are moved to the limit
    and saved, and a buy time that isn't at least CLOSE_TO_EXIT before the square-off moves to the open.
    """
    if not sp.has_exit or not s.entry_time or not s.exit_time:
        return
    last, until = sp.sell_until.strftime("%H:%M:%S"), sp.buy_until.strftime("%H:%M:%S")
    if s.exit_time > last:
        s.exit_time = last
    if s.entry_time > until:
        s.entry_time = until
    if not times_ok(s.entry_time, s.exit_time):
        s.entry_time = sp.opens.strftime("%H:%M:%S")


def times_ok(entry: str, exit_: str) -> bool:
    """The buy comes at least CLOSE_TO_EXIT before the square-off (no buy-and-close-at-once)."""
    e, x = parse_hms(entry), parse_hms(exit_)
    return (x.hour * 3600 + x.minute * 60 + x.second) - (e.hour * 3600 + e.minute * 60 + e.second) \
        >= CLOSE_TO_EXIT.total_seconds()


def section_accounts(user_id: int, section: str, db=None) -> list[int]:
    """Accounts a section trades in: autopilot on, and chosen for the section (all, unless limited)."""
    def load(db) -> list[int]:
        chosen = get_section(db, user_id, section).account_ids
        ids = [a.id for a in db.find(BrokerAccount, {"user_id": user_id, "automation_enabled": True})]
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
    held: int | None = None  # a close: what Kotak showed of the position before it (signed)


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
    section: str | None = None  # overrides the run's section in the order log
    price: float | None = None  # average fill price, once the order book shows it
    logged: bool = False  # already saved as an order log row (sent orders are saved before sending)


# Broker rejections that mean "not enough money". Kotak's RMS says e.g. "RMS:Margin Exceeds,
# Required:…, Available:…" or "Insufficient balance."; it also says "exceeds limit" (price or
# quantity limits), "Insufficient holding quantity" and "circuit limit", which are NOT about money.
FUNDS_REJECTION = re.compile(
    r"margin\s+exceed|insufficient\s+(?:fund|balance|margin|cash|limit|collateral)|"
    r"(?:fund|margin|cash|collateral|balance)s?\s+(?:is\s+|are\s+)?(?:not\s+sufficient|insufficient)|"
    r"(?:fund|margin)s?\s+shortfall|shortfall\s+(?:of\s+)?(?:fund|margin)|additional\s+margin\s+required|"
    r"not\s+enough\s+(?:fund|margin|balance|money|cash)|required\s*:?\s*[\d.,]+.{0,40}available\s*:?\s*[\d.,]+",
    re.IGNORECASE,
)
# Clearly a margin shortfall, whatever else the message mentions.
SURE_FUNDS = re.compile(r"margin\s+exceed|required\s*:?\s*[\d.,]+.{0,40}available\s*:?\s*[\d.,]+", re.IGNORECASE)
NOT_FUNDS = re.compile(r"insufficient\s+(?:holding|quantity)|check\s+(?:t1\s+)?holding|circuit|price\s+band|freeze",
                       re.IGNORECASE)
# A delivery sale Kotak refused because the account hasn't authorised it (no DDPI / POA, and no
# TPIN approval in the Kotak app today). The API can't give that approval itself.
DELIVERY_APPROVAL = re.compile(r"e-?dis|tpin|ddpi|power\s+of\s+attorney|\bpoa\b|authori[sz]|"
                               r"check\s+(?:t1\s+)?holding|insufficient\s+(?:holding|quantity)", re.IGNORECASE)
# Kotak's "Check T1 holdings ... Insufficient Quantity" also covers real shortfalls, so the hint is a likely
# cause, not a certainty (TradeLab only sells units the broker shows, so a shortfall is unlikely).
APPROVAL_HINT = ("If these units are in the account's demat, this usually means delivery sales aren't authorised: "
                 "turn on DDPI in the Kotak Neo app (one time), or approve today's sale with TPIN in the Kotak Neo "
                 "app, then tap Sell again.")


def explain_rejection(action: str, product: str | None, message: str | None) -> str | None:
    """The broker's message, plus what to do when a delivery sale needs the holder's approval."""
    if action == "sell" and (product or "").upper() == "CNC" and message and DELIVERY_APPROVAL.search(message):
        return f"{message} — {APPROVAL_HINT}"
    return message


def is_funds_rejection(message: str | None) -> bool:
    if not message:
        return False
    return bool(SURE_FUNDS.search(message)) or (bool(FUNDS_REJECTION.search(message)) and not NOT_FUNDS.search(message))


def _write_logs(user_id: int, run_id: int | None, entries: list[LogEntry], section: str | None = None) -> None:
    entries = [e for e in entries if not e.logged]
    if not entries:
        return
    with session_scope() as db:
        for e in entries:
            e.logged = True
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
                    section=e.section or section,
                    price=e.price,
                )
            )


# Runs this server process is working on right now: run id -> (user, kind, section). (Exactly one
# server process runs; runs a previous process left "running" are closed by recover_after_restart.)
_live: dict[int, tuple[int, str, str]] = {}
_live_guard = threading.Lock()


def wait_for_runs(timeout: float) -> bool:
    """Wait (up to `timeout` seconds) until no run is in progress; True if none is left."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _live_guard:
            if not _live:
                return True
        time.sleep(0.2)
    with _live_guard:
        if _live:
            log.warning("Stopping with %d run(s) still in progress: %s", len(_live), sorted(_live))
        return not _live


def _busy(user_id: int, kind: str | None = None, sections: set[str] | None = None) -> bool:
    with _live_guard:
        return any(u == user_id and (kind is None or k == kind) and (sections is None or sec in sections)
                   for u, k, sec in _live.values())


def _start_run(user_id: int, kind: str, trigger: str, section: str = INTRADAY) -> int:
    with session_scope() as db:
        run = ExecutionRun(user_id=user_id, kind=kind, section=section, trigger=trigger,
                           trading_date=now_ist().date())
        db.add(run)  # gets its id here; saved when the block ends
    with _live_guard:
        _live[run.id] = (user_id, kind, section)
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


def _finish_run(run_id: int, entries: list[LogEntry], note: str | None = None) -> str:
    """Record how a run ended; returns its status (done, partial or failed)."""
    orders = [e for e in entries if e.action in ("buy", "sell")]
    ok = sum(1 for e in orders if e.status == "success")
    unknown = sum(1 for e in entries if e.status == UNKNOWN)
    failed = sum(1 for e in entries if e.status == "failed")
    skipped = sum(1 for e in entries if e.status == "skipped")
    if failed and not ok and not unknown:
        status = "failed"
    elif failed or unknown:
        status = "partial"
    else:
        status = "done"
    parts = [f"{ok} order{'s' if ok != 1 else ''} placed"]
    if unknown:
        parts.append(f"{unknown} with no reply from Kotak — check the Kotak app")
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    if note:
        parts.insert(0, note)
    try:
        with session_scope() as db:
            run = db.get(ExecutionRun, run_id)
            if run:
                run.status = status
                run.summary = " · ".join(parts)
                run.finished_at = utcnow()
                if run.kind == "entry":
                    run.funds_short = funds_short_accounts(entries) or None
    finally:
        with _live_guard:
            _live.pop(run_id, None)
    return status


def _already_ran(user_id: int, kind: str, day: date, section: str = INTRADAY) -> bool:
    """An automatic run of `day` is running or went through. One that failed or partly failed doesn't
    count, so it is tried again (a square-off left positions open, or couldn't reach Kotak)."""
    with session_scope() as db:
        rows = db.find(ExecutionRun, {
            "user_id": user_id,
            "kind": kind,
            "section": section,
            "trading_date": day,
            "trigger": {"$in": ["scheduled", "catchup"]},
            "summary": {"$ne": INTERRUPTED},
            "status": {"$in": ["running", "done"]},
        })
    with _live_guard:
        # A "running" row this server isn't running (its end couldn't be saved) doesn't count.
        return any(r.status == "done" or r.id in _live for r in rows)


def runs_in_progress() -> bool:
    with _live_guard:
        return bool(_live)


def _pool(n: int) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=max(1, min(get_settings().max_parallel_orders, n)))


def _account_pool(n: int) -> ThreadPoolExecutor:
    """One worker per account, whatever max_parallel_orders says: an account's whole close is a long loop of
    waits, so a capped pool would start the later accounts only when an earlier one is completely done. (The
    order-level pool and the pacing stay as they are.)"""
    return ThreadPoolExecutor(max_workers=max(1, n))


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


def sleep_until(target: datetime, still_wanted=None) -> bool:
    """Sleep until `target`; False (straight away) once `still_wanted()` says the wait is pointless."""
    checked = 0.0
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return True
        if still_wanted is not None and time.monotonic() - checked >= 1.0:
            checked = time.monotonic()
            if not still_wanted():
                return False
        time.sleep(min(remaining, 0.25))


# ----------------------------------------------------------------------------- entry


NO_PRICE = "No live price available to size this order"
NO_FUNDS = "Could not read available funds"
PRICE_ATTEMPTS = 3
PRICE_RETRY_DELAY = 2.0


NOT_ENOUGH = "Not enough money: "  # prefix of every skip caused by lack of funds


def _qty_for_budget(budget: float, price: float | None, own_size: bool = False) -> tuple[int, str | None]:
    """Shares that fit in `budget`. `own_size`: the budget is the item's own ₹ amount, not the account's money."""
    if not price or price <= 0:
        return 0, NO_PRICE
    # Tiny epsilon so float noise (e.g. 39.9999999) doesn't cost a whole share.
    qty = int(math.floor(budget / price + 1e-9))
    if qty < 1:
        if own_size:
            return 0, f"The {inr(budget, 0)} set for this item is less than the price of one share ({inr(price)})"
        return 0, f"{NOT_ENOUGH}{inr(max(budget, 0), 0)} is less than the price of one share ({inr(price)})"
    return qty, None


def protected_price(price: float | None, option: bool) -> float | None:
    """The limit price Kotak turns a market buy into, and blocks money at (its published grid): equity
    and futures 2% above the last price under ₹100, 1% up to ₹500, 0.5% above; options ₹0.10 above under
    ₹1, then 10% / 5% / 3% / 2% / 1% for prices up to ₹5 / ₹10 / ₹100 / ₹500 / above."""
    if not price or price <= 0:
        return price
    if option:
        if price < 1:
            return price + 0.10
        pct = 0.10 if price < 5 else 0.05 if price < 10 else 0.03 if price < 100 else 0.02 if price < 500 else 0.01
    else:
        pct = 0.02 if price < 100 else 0.01 if price < 500 else 0.005
    return price * (1 + pct)


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
            name=it.name, account_ids=list(it.account_ids) if it.account_ids is not None else None,
            lot_size=it.lot_size or 1, expiry=it.expiry.isoformat() if it.expiry else None,
            instrument_type=it.instrument_type, option_type=it.option_type, strike=it.strike,
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _size_custom(item: PlanItem, price: float | None, funds: float | None,
                 cost: float | None = None) -> tuple[int, str | None, str | None]:
    """Size from the stock's own setting: fixed shares, a rupee amount or a % of funds (after the
    keep-aside). `cost` is what one share blocks (Kotak's protected price for a buy)."""
    cost = cost or price
    if item.sizing_mode == "qty":
        qty = int(item.sizing_value)
        return (qty, None, None) if qty >= 1 else (0, "Quantity must be at least 1", None)
    if item.sizing_mode == "amount":
        budget = item.sizing_value
    else:
        if funds is None:
            return 0, NO_FUNDS, None
        budget = funds * item.sizing_value / 100
    qty, reason = _qty_for_budget(budget, cost, own_size=item.sizing_mode == "amount")
    return qty, reason, None if reason else f"{inr(budget, 0)} budget at ≈{inr(price or 0)}"


def _size_equal(budget: float | None, parts: int, price: float | None,
                cost: float | None = None) -> tuple[int, str | None, str | None]:
    """Split the money free in this account equally across the items; buy as many shares as fit.
    `cost` is what one share blocks (Kotak's protected price for a buy)."""
    if budget is None:
        return 0, NO_FUNDS, None
    if budget <= 0:
        return 0, f"{NOT_ENOUGH}this account has {inr(max(budget, 0))} available", None
    share = budget / parts
    qty, reason = _qty_for_budget(share, cost or price)
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


# Delivery (CNC) buys are paid in cash: this much is left for charges (the protected price Kotak
# blocks money at is allowed for separately, see protected_price).
CNC_HEADROOM = 0.015
# Lots are sized against the margin at the last price plus this much, because Kotak turns
# market orders into limit orders a little above it (and prices move in the meantime).
LOT_HEADROOM = 0.025
# SEBI: at least half the margin of a futures or sold-first position must be cash, so margin trades
# use at most twice the cash free (pledged shares alone aren't enough).
CASH_SHARE_OF_MARGIN = 0.5


def _is_option(item: PlanItem) -> bool:
    return item.option_type in ("CE", "PE")


def _pays_premium(item: PlanItem) -> bool:
    """Buying an option: the premium is paid in cash. Selling one first (writing) needs margin instead."""
    return _is_option(item) and item.side == "B"


def _margin_room(budget: float | None, cash: float | None) -> float | None:
    """Money a futures or sold-first trade may use: the margin free, but at most twice the cash free."""
    if budget is None or cash is None:
        return budget
    return min(budget, max(cash, 0.0) / CASH_SHARE_OF_MARGIN)


def free_money(funds: Funds | None, product: str, reserve_pct: float = 0.0, cash_only: bool = False) -> float | None:
    """Money this account can put into a new trade right now, after the keep-aside %.

    Margin trades (futures, MIS/NRML stocks) can use everything Kotak shows as available,
    including margin from pledged shares; delivery (CNC) buys and option premiums must be
    paid in cash, so pledge margin is left out for them.
    """
    if funds is None:
        return None
    free = funds.available
    if product == "CNC" or cash_only:
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
        flt: dict = {"user_id": user_id, "automation_enabled": True}
        if account_ids is not None:
            flt["id"] = {"$in": list(account_ids)}
        return [_Acc(a.id, a.name, a.broker) for a in db.find(BrokerAccount, flt, sort=[("id", 1)])]


SLOW_ACCOUNT = "Kotak didn't answer in time for this account, so nothing was sent to it"
# Accounts get until this long before the buy to be ready (but at least READY_MIN from now).
READY_BEFORE = timedelta(seconds=3)
READY_MIN = timedelta(seconds=20)


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
    ready_by: datetime | None = None,
    preview: bool = False,
) -> list[PlannedOrder]:
    """Buy orders for a section's `items` in each of `account_ids` (default: the section's accounts).

    With `ready_by`, an account whose Kotak checks haven't answered by then is skipped (nothing sent to
    it), so one slow account can't make every account miss the buy.

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
    gone: set[str] = set()
    if sp.lots:
        items, gone = _current_contracts(items)
    equal = sizing_mode == "equal"
    prices = _prices(user_id, items, everything=equal or all_prices or sp.lots or product == "CNC")
    margins = _lot_margins(user_id, items, product, prices, [a.id for a in accounts]) if sp.lots else {}
    # Contracts: what TradeLab itself holds, to tell it apart from the account holder's own positions.
    if sp.lots:
        reconcile_orders(user_id, [a.id for a in accounts])
    ledger = tradelab_open(user_id, section, [a.id for a in accounts], {i.trading_symbol for i in items},
                           {product}) if sp.lots else {}
    unfinished = unfinished_keys(user_id, [a.id for a in accounts]) if sp.lots else set()
    # TradeLab's records count such an order in full: an order sized against them (one that closes "TradeLab's"
    # position first) could close the holder's own contracts if it was in fact rejected.
    unconfirmed = unconfirmed_orders(user_id, [a.id for a in accounts]) if sp.lots else {}
    if sp.lots:  # (a Buy of today with no known outcome counts as unconfirmed too)
        unconfirmed |= {k: r.quantity for k, r in unresolved_entries(user_id, section, [a.id for a in accounts]).items()
                        if _aware(r.created_at) >= _order_book_day_start() and k not in unconfirmed}

    def applies(item: PlanItem, acc: _Acc) -> bool:
        return item.account_ids is None or acc.id in item.account_ids  # [] = no account at all

    def key(acc: _Acc, it: PlanItem) -> Lot:
        return Lot(acc.id, it.exchange_segment, it.trading_symbol, product)

    def prepare(acc: _Acc) -> tuple[_Acc, Funds | None, str | None, dict[Lot, int]]:
        mine = [i for i in items if applies(i, acc)]
        if not mine:
            return acc, None, None, {}
        try:
            broker_manager.ensure_session(acc.id)
        except Exception as exc:  # noqa: BLE001
            return acc, None, f"Login failed: {exc}", {}
        funds = None
        try:
            funds = broker_manager.call(acc.id, lambda b: b.funds())
        except Exception as exc:  # noqa: BLE001
            log.warning("Funds unavailable for %s: %s", acc.name, exc)
        held: dict[Lot, int] = {}
        if sp.lots:
            # Kotak nets an order against an open position in the same contract and product, so an
            # order on the other side of the holder's own position would close their contracts.
            try:
                view = broker_view(acc.id, [key(acc, i) for i in mine])
                _shrink_to_broker(user_id, section, acc, ledger, view, unfinished, write=not preview)
                held = view.held
            except Exception as exc:  # noqa: BLE001
                return acc, funds, f"Could not check this account's open positions, so nothing was sent to it: {exc}", {}
        return acc, funds, None, held

    ex = _pool(len(accounts))
    futures = [ex.submit(prepare, acc) for acc in accounts]
    timeout = None if ready_by is None else max(0.0, (ready_by - now_ist()).total_seconds())
    done, _ = wait_futures(futures, timeout=timeout)
    prepared = [f.result() if f in done else (acc, None, SLOW_ACCOUNT, {}) for acc, f in zip(accounts, futures)]
    ex.shutdown(wait=False, cancel_futures=True)  # a slow account's checks finish in the background

    plan: list[PlannedOrder] = []
    for acc, funds, err, held in prepared:
        mine = [it for it in items if applies(it, acc)]
        budget = free_money(funds, product, reserve_pct)
        cash = free_money(funds, product, reserve_pct, cash_only=True)
        # Fixed sizes: what earlier items of this account already use comes off the rest.
        left, left_cash = budget, cash
        for it in mine:
            price = prices.get(it.trading_symbol)
            cost = protected_price(price, _is_option(it)) if it.side == "B" else price
            lots = None
            outside = _outside_position(held.get(key(acc, it), 0), ledger.get(key(acc, it), 0), it.side) \
                if sp.lots and not err else 0
            if sp.lots and it.trading_symbol in gone:
                qty, reason, note = 0, GONE, None
            elif sp.lots and not err and key(acc, it) in unconfirmed:
                qty, reason, note = 0, UNCONFIRMED, None
            elif outside:
                qty, reason, note = 0, _own_position_note(outside, it.side), None
            elif sp.lots:
                option = _pays_premium(it)
                ours = ledger.get(key(acc, it), 0)
                custom = int(it.sizing_value) if not equal else None
                margin = margins.get((it.trading_symbol, it.side))
                padded = margin * (1 + LOT_HEADROOM) if margin else margin
                if custom is None:
                    # Option premium is paid in cash; futures and sold-first margin may use pledge margin,
                    # but at least half of it must be cash.
                    own = cash if option else _margin_room(budget, cash)
                    lots, reason, note = _size_lots(own, len(mine), padded, max_lots, None)
                else:
                    own = (None if left is None else min(left, left_cash if left_cash is not None else left)) if option \
                        else _margin_room(left, left_cash)
                    lots, reason, note = _size_lots(own, 1, padded, max_lots, custom)
                    if lots and padded and left is not None:
                        used = lots * padded
                        left -= used
                        if left_cash is not None:
                            left_cash -= used if option else used * CASH_SHARE_OF_MARGIN
                            left_cash = min(left_cash, left)
                qty = order_units(lots, it.lot_size) if lots else 0
                if qty and (ours > 0 if it.side == "S" else ours < 0):
                    # Kotak nets it against TradeLab's own position on the other side, which closes first.
                    closes = min(qty, abs(ours))
                    did = "bought" if ours > 0 else "sold first"
                    first = (f"Closes {closes} of the {abs(ours)} TradeLab {did}" if closes < abs(ours)
                             else f"First closes the {abs(ours)} TradeLab {did}")
                    if qty > abs(ours):
                        first += f", then {'sells first' if it.side == 'S' else 'buys'} {qty - abs(ours)}"
                    note = f"{first} · {note}" if note else first
            elif equal:
                qty, reason, note = _size_equal(budget, len(mine), price, cost)
            else:
                qty, reason, note = _size_custom(it, price, None if funds is None else free_money(funds, product, reserve_pct),
                                                 cost)
                if qty and product == "CNC" and left_cash is not None and cost:
                    # Delivery is paid in cash: fixed sizes are capped by the cash still free in the account.
                    fits = int(math.floor(left_cash / cost + 1e-9))
                    if fits < 1:
                        qty, reason, note = 0, f"{NOT_ENOUGH}{inr(max(left_cash, 0), 0)} cash left is less than one share ({inr(cost)})", None
                    elif fits < qty:
                        qty, note = fits, f"{note} — capped by the cash free" if note else "Capped by the cash free"
                    left_cash -= qty * cost
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
                    error=bool(err) or reason in (NO_PRICE, NO_FUNDS, NO_MARGIN, GONE, UNCONFIRMED),
                    funds_short=not err and bool(reason) and reason.startswith(NOT_ENOUGH),
                    product=product,
                    lots=lots,
                )
            )
    return plan


GONE = "This contract isn't in today's contract list any more (expired or changed) — remove it and add it again"
UNCONFIRMED = ("Kotak's order book hasn't confirmed TradeLab's last order for this yet, so nothing is sent for it. "
               "Try again in a moment, or check the Kotak Neo app.")


def _outside_position(broker: int, tradelab: int, side: str) -> int:
    """Units of the account holder's own position (not TradeLab's) that an order on `side` would close.

    `broker` and `tradelab` are signed (negative = sold first). Selling closes longs and buying
    closes shorts, so only the holder's part on the other side of the order matters.
    """
    if side == "S":
        return max(0, max(broker, 0) - max(tradelab, 0))
    return max(0, max(-broker, 0) - max(-tradelab, 0))


def _own_position_note(units: int, side: str) -> str:
    if side == "S":
        return (f"Not sent: this account already holds {units} of this contract bought outside TradeLab — "
                "selling first would sell them")
    return (f"Not sent: this account has already sold {units} of this contract first, outside TradeLab — "
            "buying would buy them back")


def _current_contracts(items: list[PlanItem]) -> tuple[list[PlanItem], set[str]]:
    """Lot size and token from today's contract list (NSE revises lot sizes of open contracts)."""
    from . import contracts

    out, gone = [], set()
    for it in items:
        try:
            c = contracts.contract_master.get(it.exchange_segment, it.trading_symbol)
        except Exception:  # noqa: BLE001 - list unavailable: keep what was saved
            log.warning("Contract list unavailable for %s", it.trading_symbol)
            out.append(it)
            continue
        if c is None:
            gone.add(it.trading_symbol)
            out.append(it)
        else:
            out.append(PlanItem(**{**it.to_dict(), "lot_size": c.lot_size, "token": c.token or it.token}))
    return out, gone


def _lot_margins(user_id: int, items: list[PlanItem], product: str, prices: dict[str, float],
                 account_ids: list[int]) -> dict[tuple[str, str], float]:
    """Margin Kotak needs for one lot of each contract, by (trading symbol, side).

    Asked once per contract and side from the first Kotak account that answers (a buy's
    margin, or a written option's, is the same in every account).
    """
    out: dict[tuple[str, str], float] = {}
    with session_scope() as db:
        ids = [a.id for a in db.find(BrokerAccount, {
            "user_id": user_id, "broker": "kotak", "id": {"$in": list(account_ids)},
        }, sort=[("id", 1)])]
    for it in items:
        price = prices.get(it.trading_symbol)
        if (it.trading_symbol, it.side) in out or not price:
            continue
        if it.side == "B":
            price = protected_price(price, _is_option(it))  # what Kotak blocks a market buy at
        for acc_id in ids:
            try:
                m = broker_manager.call(acc_id, lambda b, it=it, price=price: b.margin_per_lot(
                    it.exchange_segment, it.token or "", it.trading_symbol, it.side, product,
                    order_units(1, it.lot_size), price))
            except Exception as exc:  # noqa: BLE001
                log.warning("Margin for %s unavailable from account %s: %s", it.trading_symbol, acc_id, exc)
                continue
            if m and m >= 1:  # a reply of 0 (or a few paise) is not a real margin
                out[(it.trading_symbol, it.side)] = m
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


def _reserve_logs(user_id: int, run_id: int, tasks: list[PlannedOrder], section: str | list) -> list[int]:
    """Record every order as "pending" *before* sending it, so a crash or lost reply
    can never hide an order from the square-off. Each row's tag goes out with its order."""
    sections = section if isinstance(section, list) else [section] * len(tasks)
    def contract(p: PlannedOrder) -> dict:
        c = _contract(p.exchange_segment, p.trading_symbol) if p.exchange_segment in DERIVATIVE_SEGMENTS else None
        return {"lot_size": c.lot_size, "expiry": c.expiry.isoformat() if c.expiry else None} if c else {}

    with session_scope() as db:
        rows = [
            OrderLog(user_id=user_id, run_id=run_id, account_id=p.account_id, account_name=p.account_name,
                     action="buy" if p.side == "B" else "sell", symbol=p.trading_symbol, quantity=p.quantity,
                     status="pending", message="Sending…", exchange_segment=p.exchange_segment, product=p.product,
                     section=sec, **contract(p))
            for p, sec in zip(tasks, sections, strict=True)
        ]
        db.add_all(rows)  # ids are given here; saved when the block ends
        for r in rows:
            r.tag = f"TL{r.id}"
        return [r.id for r in rows]


def _settle_logs(ids: list[int], results: list[LogEntry]) -> None:
    now = utcnow()
    with session_scope() as db:
        # One read, then one all-or-nothing save of every row when the block ends.
        rows = {r.id: r for r in db.find(OrderLog, {"id": {"$in": list(ids)}})}
        for log_id, e in zip(ids, results, strict=True):
            row = rows.get(log_id)
            if row is None:
                continue
            e.logged = True
            if row.status != "pending":
                # Already settled from Kotak's order book (which knows better than the send's reply): keep it.
                row.order_id = row.order_id or e.order_id
                e.status, e.order_id, e.message, e.price = row.status, row.order_id, row.message, row.price
                if row.quantity is not None:
                    e.quantity = row.quantity
                continue
            row.status, row.order_id, row.message = e.status, e.order_id, (e.message or "")[:1000] or None
            if e.quantity is not None:
                row.quantity = e.quantity
            row.price = e.price
            row.sent_at = now


SENT_UNKNOWN = ("The run sending this order stopped before it was settled: the order may have gone through. "
                "TradeLab checks Kotak's order book for it.")


def _unsettled_to_unknown(ids: list[int]) -> None:
    """Rows of a run that is gone and never settled them: the orders may have gone out, so they count as
    possibly sent (looked up by tag in Kotak's order book) instead of staying "pending" for good."""
    if not ids:
        return
    with session_scope() as db:
        db.update_many(OrderLog, {"id": {"$in": list(ids)}, "status": "pending"},
                       {"status": UNKNOWN, "sent_at": utcnow(), "message": SENT_UNKNOWN})


# The most units one exchange order may carry ("freeze quantity"); a bigger order is rejected whole,
# so TradeLab sends it in several slices. Index contracts from the exchanges' published limits; any
# other contract goes in slices of at most SLICE_LOTS lots (MCX: MCX_SLICE_LOTS), which stays under
# the per-order limits of stock F&O and MCX contracts.
FREEZE_UNITS = {
    ("nse_fo", "NIFTY"): 1800, ("nse_fo", "BANKNIFTY"): 900, ("nse_fo", "FINNIFTY"): 1800,
    ("nse_fo", "MIDCPNIFTY"): 2800, ("nse_fo", "NIFTYNXT50"): 600,
    ("bse_fo", "SENSEX"): 1000, ("bse_fo", "BANKEX"): 900,
}
SLICE_LOTS = 20
MCX_SLICE_LOTS = 10


def _max_order_units(p: PlannedOrder) -> int | None:
    """Most units one order for `p`'s contract may carry, in whole lots (None: no limit, e.g. stocks)."""
    if p.exchange_segment not in ("nse_fo", "bse_fo", "mcx_fo"):
        return None
    c = _contract(p.exchange_segment, p.trading_symbol)
    lot = (c.lot_size if c else None) or (p.quantity // p.lots if p.lots else None)
    if not lot or lot < 1:
        return None
    underlying = (c.symbol if c else p.symbol or "").upper()
    lots = MCX_SLICE_LOTS if p.exchange_segment == "mcx_fo" else SLICE_LOTS
    freeze = FREEZE_UNITS.get((p.exchange_segment, underlying))
    if freeze:
        lots = max(1, (freeze - 1) // lot)  # strictly under the freeze quantity
    return lots * lot


def _slices(tasks: list[PlannedOrder]) -> list[PlannedOrder]:
    """Split orders bigger than their contract's freeze quantity into several orders."""
    out: list[PlannedOrder] = []
    for p in tasks:
        cap = _max_order_units(p)
        if not cap or p.quantity <= cap:
            out.append(p)
            continue
        lot = p.quantity // p.lots if p.lots else None
        left, n = p.quantity, 0
        while left > 0:
            q = min(cap, left)
            n += 1
            note = f"part {n} of {math.ceil(p.quantity / cap)} (exchange limit per order)"
            out.append(replace(p, quantity=q, lots=q // lot if lot else None,
                               note=f"{p.note} · {note}" if p.note else note))
            left -= q
    return out


def _unknown_hint(user_id: int | None, section: str) -> str:
    """What happens next to an order whose outcome isn't known, for its log message."""
    sp = spec(section)
    if not sp.has_exit or user_id is None:
        return ""
    with session_scope() as db:
        sett = get_section(db, user_id, section)
        if sett.auto_exit:
            return f" If it went through, it is squared off at {sett.exit_time[:5]} like the rest."
    return " Automatic square-off is off: close it yourself if it went through."


def _place(p: PlannedOrder, tag: str | None, after: str, pace: bool = True) -> LogEntry:
    """Send one planned order and record what came back."""
    action = "buy" if p.side == "B" else "sell"
    if pace:
        _pace(p.account_id)
    try:
        res = broker_manager.call(
            p.account_id,
            lambda b: b.place_market_order(p.exchange_segment, p.trading_symbol, p.side, p.quantity, p.product,
                                           tag=tag),
        )
        message = f"{res.message} · {p.note}" if p.note and res.ok else explain_rejection(action, p.product, res.message)
        return LogEntry(p.account_id, p.account_name, action, "success" if res.ok else "failed",
                        p.trading_symbol, p.quantity, res.order_id, message,
                        exchange_segment=p.exchange_segment, product=p.product)
    except (LoginFailed, SessionExpired) as exc:
        # Logging in failed, or Kotak refused the order again right after a fresh login: not sent.
        return LogEntry(p.account_id, p.account_name, action, "failed", p.trading_symbol, p.quantity,
                        message=f"Not sent: {exc}", exchange_segment=p.exchange_segment, product=p.product)
    except Exception as exc:  # noqa: BLE001 - the request may have reached Kotak
        return LogEntry(p.account_id, p.account_name, action, UNKNOWN, p.trading_symbol, p.quantity,
                        message=f"No clear reply from Kotak ({exc}). The order may have gone through — "
                                f"check the Kotak app.{after}",
                        exchange_segment=p.exchange_segment, product=p.product)


def fire_plan(plan: list[PlannedOrder], user_id: int | None = None, run_id: int | None = None,
              section: str = INTRADAY, closing: bool = False, unclosed: list[PlannedOrder] | None = None,
              ) -> list[LogEntry]:
    """Send every planned order at once. Orders are logged as pending first when a run is given, then
    followed in Kotak's order book until they finish, so the log shows what really happened. `closing`:
    the orders close positions, so one that ends short of its quantity is reported as not done. A closing
    account that holds both a sold contract and a bought one on the same underlying (a hedged position) is
    closed in two waves, so the hedge is never sold while the sold leg stays open: the buy-backs first, the
    sales once they have finished. An opening account is the other way round: its bought contracts go first and
    a sold-first contract of the same underlying only once they all filled in full, so a hedge that is refused
    never leaves the sold leg naked. `unclosed`: sold contracts of the plan left out of `plan` that stay open."""
    if run_id is not None and user_id is not None:
        later = _hedges_to_wait_for(plan, unclosed or []) if closing else _sales_to_wait_for(plan)
        if later:
            return _fire_in_waves(plan, later, unclosed or [], user_id, run_id, section, closing)
    return _fire(plan, user_id, run_id, section, closing)


def _short_close(p) -> bool:
    """A close that buys back a sold contract (a PlannedOrder or a LogEntry of one)."""
    side = getattr(p, "side", None) or ("B" if getattr(p, "action", None) == "buy" else "S")
    return side == "B" and p.exchange_segment in DERIVATIVE_SEGMENTS


# MCX's mini / micro contracts hedge their full-size family (margin offsets are Kotak's to give or refuse).
MCX_FAMILIES = {"CRUDEOILM": "CRUDEOIL", "GOLDM": "GOLD", "GOLDPETAL": "GOLD", "GOLDGUINEA": "GOLD",
                "SILVERM": "SILVER", "SILVERMIC": "SILVER", "NATGASMINI": "NATURALGAS", "COPPERM": "COPPER",
                "ZINCMINI": "ZINC", "LEADMINI": "LEAD", "ALUMINI": "ALUMINIUM"}


def _underlying_family(segment: str, symbol: str) -> str:
    return MCX_FAMILIES.get(symbol, symbol) if segment == "mcx_fo" else symbol


def _same_underlying(a, b) -> bool:
    """Whether two contracts of one account may be a hedged pair. Unknown (a contract not in today's list)
    counts as the same, so a hedge is held rather than sold beside a short that may belong to it."""
    if a.exchange_segment != b.exchange_segment:
        return False
    ca, cb = (_contract(x.exchange_segment, getattr(x, "trading_symbol", None) or x.symbol) for x in (a, b))
    return ca is None or cb is None or (_underlying_family(a.exchange_segment, ca.symbol)
                                        == _underlying_family(b.exchange_segment, cb.symbol))


def _hedges_to_wait_for(plan: list[PlannedOrder], unclosed: list[PlannedOrder]) -> list[PlannedOrder]:
    """Sales of bought derivative contracts in an account that also has a sold contract of the same
    underlying to buy back (or one that stays open): they go out after the buy-backs."""
    shorts = [p for p in [*plan, *unclosed]
              if _short_close(p) and ((p.skip and p.error) or (not p.skip and p.quantity > 0))]
    return [p for p in plan
            if p.side == "S" and p.exchange_segment in DERIVATIVE_SEGMENTS and not p.skip and p.quantity > 0
            and any(s.account_id == p.account_id and _same_underlying(p, s) for s in shorts)]


def _sales_to_wait_for(plan: list[PlannedOrder]) -> list[PlannedOrder]:
    """Sold-first derivative contracts in an account that also buys a contract of the same underlying (its hedge,
    also one skipped or sized to nothing): they go out after the bought contracts."""
    hedges = [p for p in plan if p.side == "B" and p.exchange_segment in DERIVATIVE_SEGMENTS]
    return [p for p in plan
            if p.side == "S" and p.exchange_segment in DERIVATIVE_SEGMENTS and not p.skip and p.quantity > 0
            and any(h.account_id == p.account_id and _same_underlying(p, h) for h in hedges)]


def _fire_in_waves(plan: list[PlannedOrder], later: list[PlannedOrder], unclosed: list[PlannedOrder],
                   user_id: int, run_id: int, section: str, closing: bool = True) -> list[LogEntry]:
    """Per account in parallel: everything but the hedges first, and each hedge only if every sold contract
    of the same underlying in that account was fully bought back (a buy-back left working, part filled,
    refused or unknown counts as not bought back). Opening (not `closing`): the same with the roles swapped,
    the sold-first contracts wait until every bought contract of their underlying filled in full (one that
    was skipped, refused, part filled or is still working counts as not bought)."""
    waiting = {id(p) for p in later}
    began = time.monotonic()
    hedged = sorted({p.account_id for p in later})
    free = [p for p in plan if p.account_id not in hedged]

    def one_account(acc_id: int) -> list[LogEntry]:
        mine = [p for p in plan if p.account_id == acc_id]
        hold = [p for p in mine if id(p) in waiting]
        try:
            entries = _fire([p for p in mine if id(p) not in waiting], user_id, run_id, section, closing)
            # Closing: sold contracts still open (not fully bought back, or left out of the plan). Opening: bought
            # contracts that aren't all in (a skipped one too: it was not bought).
            open_shorts = [e for e in entries
                           if _short_close(e) and e.status not in (("success", "skipped") if closing else ("success",))]
            if closing:
                open_shorts += [u for u in unclosed if u.account_id == acc_id and _short_close(u)]
            send, kept = [], []
            for p in hold:
                (kept if any(_same_underlying(p, s) for s in open_shorts) else send).append(p)
            why = ("the sold leg it hedges couldn't be bought back first, so selling this one now could leave that "
                   "position without its hedge. Close again, or check the Kotak Neo app." if closing else
                   "its hedge couldn't be bought first, so selling this one could leave it without a hedge. Check "
                   "the Kotak Neo app.")
            if not closing and send and _stopped_at.get(user_id, -1.0) >= began:
                kept, send = hold, []
                why = "“Square off everything” was tapped while the hedge was being bought, so this was not sold."
            blocked = [LogEntry(
                p.account_id, p.account_name, "sell", "failed", p.trading_symbol, p.quantity,
                message=f"Not sold: {why}", exchange_segment=p.exchange_segment, product=p.product) for p in kept]
            _write_logs(user_id, run_id, blocked, section)
            entries += blocked
            if send:
                entries += _fire(send, user_id, run_id, section, closing)
            return entries
        except Exception as exc:  # noqa: BLE001
            log.exception("Closing account %s in two waves failed", acc_id)
            return [LogEntry(acc_id, hold[0].account_name, "sell", "failed", message=f"Closing crashed: {exc}")]

    with _account_pool(len(hedged) + 1) as ex:
        rest = ex.submit(_fire, free, user_id, run_id, section, closing) if free else None
        out = [e for es in ex.map(one_account, hedged) for e in es]
        return (rest.result() if rest else []) + out


def _fire(plan: list[PlannedOrder], user_id: int | None = None, run_id: int | None = None,
          section: str = INTRADAY, closing: bool = False) -> list[LogEntry]:
    entries = _skip_entries(plan)
    tasks = _interleave(_slices([p for p in plan if not p.skip and p.quantity > 0]))
    if not tasks:
        return entries
    ids = _reserve_logs(user_id, run_id, tasks, section) if run_id is not None else []
    try:
        after = _unknown_hint(user_id, section)

        def place(i: int) -> LogEntry:
            return _place(tasks[i], f"TL{ids[i]}" if ids else None, after)

        with _pool(len(tasks)) as ex:
            results = list(ex.map(place, range(len(tasks))))
        if ids:
            _settle_logs(ids, results)
            results = _await_final(user_id, ids, results)
            if closing or spec(section).lots:
                short = _short_closes(ids, tasks, closing)
                _write_logs(user_id, run_id, short, section)
                results += short
        return entries + results
    except BaseException:
        # Orders may be out: whatever this run didn't settle can't stay "pending" (nothing follows those up).
        try:
            _unsettled_to_unknown(ids)
        except Exception:  # noqa: BLE001
            log.exception("Marking unsettled orders as unknown failed")
        raise


def _stray_orders(user_id: int, closes: list[PlannedOrder]) -> list[LogEntry]:
    """Another order still working at Kotak on a contract TradeLab just closed (a stop-loss set in the Kotak Neo
    app, say) opens a new position if it fills beyond what the holder still holds there, with nobody watching it.
    TradeLab never cancels it (it may protect the holder's own units), but reports it, so the run isn't taken as
    done. Delivery is left out: a delivery sale can't open a position."""
    by_acc: dict[int, list[PlannedOrder]] = {}
    for p in closes:
        if not p.skip and p.quantity > 0 and (p.product or "").upper() != "CNC":
            by_acc.setdefault(p.account_id, []).append(p)
    if not by_acc:
        return []
    with session_scope() as db:
        rows = db.find(OrderLog, {"user_id": user_id, "account_id": {"$in": sorted(by_acc)},
                                  "created_at": {"$gte": _order_book_day_start()}}, track=False)
    mine = {x for r in rows for x in (r.order_id, f"TL{r.id}") if x}

    def check(acc_id: int) -> list[LogEntry]:
        ps = by_acc[acc_id]
        try:
            orders = broker_manager.call(acc_id, lambda b: b.orders())
            _note_tags(acc_id, orders)
        except Exception as exc:  # noqa: BLE001
            return [LogEntry(acc_id, ps[0].account_name, "sell", "failed", message=(
                "Closed, but Kotak's order book couldn't be read to check for another order still working on these "
                f"contracts (a stop-loss, for example): {exc}. Check the Kotak Neo app."))]
        out = []
        for p in ps:
            k = (p.exchange_segment, p.trading_symbol, (p.product or "").upper())
            stray = sum(max(o.quantity - max(o.filled_qty, 0), 0) for o in orders
                        if o.is_open and o.order_id not in mine and (o.tag or "") not in mine
                        and not (o.tag or "").startswith("SQ") and o.side == p.side
                        and (o.exchange_segment, o.trading_symbol, (o.product or "").upper()) == k)
            # What the holder still holds on that side takes such an order first.
            left = max(0, (p.held or 0) - p.quantity) if p.side == "S" else max(0, -(p.held or 0) - p.quantity)
            if stray > left:
                out.append(LogEntry(acc_id, p.account_name, "sell" if p.side == "S" else "buy", "failed",
                                    p.trading_symbol, stray - left, message=(
                                        "Closed, but another order on this contract (a stop-loss, for example) is "
                                        "still working at Kotak and would open a new position if it fills. Cancel "
                                        "it in the Kotak Neo app."),
                                    exchange_segment=p.exchange_segment, product=p.product))
        return out

    with _pool(len(by_acc)) as ex:
        return [e for es in ex.map(check, sorted(by_acc)) for e in es]


def _short_closes(ids: list[int], tasks: list[PlannedOrder], closing: bool = True) -> list[LogEntry]:
    """Closes that ended with less than they asked for (part filled, then rejected or cancelled), or that are
    still working when TradeLab stops following them: what they didn't close is still open, so each gets a
    failed entry for it. The run then isn't "done", and the pop-up names the account. The same for the
    opening orders of a lots section (`closing` False): what they didn't buy or sell first is missing."""
    with session_scope() as db:
        rows = {r.id: r for r in db.find(OrderLog, {"id": {"$in": list(ids)}}, track=False)}
    out = []
    for log_id, p in zip(ids, tasks, strict=True):
        row = rows.get(log_id)
        if row is None or row.status != "success":
            continue  # failed or unknown: already reported as such
        if not row.final:
            done = min(row.filled or 0, p.quantity)
            message = (f"Still working at Kotak ({done} of {p.quantity} filled so far), so it may not all be "
                       + ("closed. Close again to cancel it and close what is left, or check the Kotak Neo app."
                          if closing else "done. Check the Kotak Neo app."))
            rest = p.quantity - done
        elif row.quantity is not None and row.quantity < p.quantity:
            rest = p.quantity - row.quantity
            message = (f"Only {row.quantity} of {p.quantity} {'closed' if closing else 'filled'} "
                       f"({row.message or 'Kotak gave no reason'}). "
                       + (f"{rest} still open: close again, or close them in the Kotak Neo app." if closing else
                          f"The other {rest} weren't done: check the Kotak Neo app."))
        else:
            continue
        out.append(LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell", "failed",
                            p.trading_symbol, rest, message=message[:1000], exchange_segment=p.exchange_segment,
                            product=p.product))
    return out


def _await_final(user_id: int, ids: list[int], results: list[LogEntry]) -> list[LogEntry]:
    """Follow just-sent orders in Kotak's order book until each has finished (or the checks run out),
    and bring the log entries up to date: late rejections, part fills, fill prices."""
    for delay in FINAL_CHECKS:
        with session_scope() as db:
            waiting = db.count(OrderLog, {"id": {"$in": list(ids)}, "final": {"$ne": True},
                                          "status": {"$in": ["success", "pending", UNKNOWN]}})
        if not waiting:
            break
        time.sleep(delay)
        try:
            reconcile_orders(user_id, row_ids=ids)
        except Exception:  # noqa: BLE001 - the orders were sent; following them is best effort
            log.exception("Following orders in the order book failed")
    with session_scope() as db:
        rows = {r.id: r for r in db.find(OrderLog, {"id": {"$in": list(ids)}}, track=False)}
    for log_id, e in zip(ids, results, strict=True):
        row = rows.get(log_id)
        if row is None:
            continue
        rejected = e.status != "failed" and row.status == "failed"
        # (or part filled and the rest rejected, or lapsed)
        cut = row.quantity is not None and e.quantity is not None and row.quantity < e.quantity
        e.status, e.order_id, e.message, e.price = row.status, row.order_id, row.message, row.price
        if row.quantity is not None:
            e.quantity = row.quantity
        if rejected or cut:
            e.funds_short = is_funds_rejection(row.message)
    return results


# Orders not seen in Kotak's order book this long after being sent were never placed.
MISSING_AFTER = timedelta(seconds=60)


def _note_tags(account_id: int, orders: list[Order]) -> None:
    """Remember, per segment, that this account's order book echoed a TradeLab tag: that is a property of
    Kotak's API for the segment, not of the day, and a later day's book may hold no TradeLab order yet."""
    for seg in {o.exchange_segment for o in orders if (o.tag or "").startswith(("TL", "SQ")) and o.exchange_segment}:
        database()["tag_proofs"].update_one({"_id": f"{account_id}:{seg}"}, {"$set": {"seen_at": utcnow()}}, upsert=True)


def _tags_echoed(orders: list[Order], segment: str | None, account_id: int | None = None) -> bool:
    """Whether Kotak's order book shows a TradeLab tag on an order of today in `segment`, or showed one of this
    account's before: only then does a missing order mean it was never placed (a book that doesn't echo tags
    would lack a placed order just the same)."""
    if any((o.tag or "").startswith(("TL", "SQ")) and (segment is None or o.exchange_segment == segment)
           for o in orders):
        return True
    if account_id is None:
        return False
    flt = {"_id": f"{account_id}:{segment}"} if segment else {"_id": {"$regex": f"^{account_id}:"}}
    return database()["tag_proofs"].find_one(flt) is not None


def _look_alike(orders: list[Order], row: OrderLog) -> bool:
    """Whether the book holds an order that could be `row`'s, tagged or not: same segment, contract and side."""
    side = "B" if row.action == "buy" else "S"
    mine = (row.symbol or "").strip().upper().removesuffix("-EQ")

    def same(symbol: str) -> bool:  # (a spelling that differs a little still counts: never prove absence on it)
        s = (symbol or "").strip().upper().removesuffix("-EQ")
        return bool(s) and (s.startswith(mine) or mine.startswith(s))

    return any(o.exchange_segment == row.exchange_segment and same(o.trading_symbol)
               and (o.side or "")[:1].upper() == side for o in orders)


def _order_book_day_start() -> datetime:
    """Start (UTC) of the day Kotak's order books cover: they list only today's orders."""
    return datetime.combine(datetime.now(IST).date(), dtime(0, 0), tzinfo=IST).astimezone(timezone.utc)


def _apply_order(row: OrderLog, order: Order) -> None:
    """Update a log row from what Kotak's order book shows for its order."""
    st = (order.status or "").lower()
    if not row.order_id and order.order_id:
        row.order_id = order.order_id
    if order.avg_price and order.avg_price > 0 and order.filled_qty > 0:
        row.price = order.avg_price  # what this order really filled at (for TradeLab's own P/L)
    if st.startswith("reject") and order.filled_qty > 0:
        # Part filled, the rest rejected: the filled units are real and TradeLab's own.
        row.status = "success"
        row.quantity = order.filled_qty
        row.message = (f"Only {order.filled_qty} filled; the rest was rejected by the broker: "
                       f"{order.message or 'no reason given'}")[:1000]
        row.final = True
    elif st.startswith("reject"):
        row.status = "failed"
        row.message = explain_rejection(row.action, row.product,
                                        f"Rejected by the broker: {order.message or 'no reason given'}")[:1000]
        row.final = True
    elif order.is_open:
        row.filled = max(order.filled_qty, 0)  # a working order counts by what has filled so far
        if row.status in ("pending", UNKNOWN):
            row.status = "success"
            row.message = "Placed (found in Kotak's order book), not filled yet"
    else:  # complete / traded / cancelled / expired / lapsed
        asked = row.quantity or 0
        filled = max(order.filled_qty, 0)
        if filled <= 0:
            row.status = "failed"
            row.message = f"Not filled: the order ended {order.status or 'unfilled'} at Kotak"
        else:
            if row.status in ("pending", UNKNOWN):
                row.message = "Placed (found in Kotak's order book)"
            row.status = "success"
            if asked and filled < asked:
                # Record what really went through, so Sell never sells more than TradeLab bought.
                row.message = f"Only {filled} of {asked} filled · {row.message or ''}".rstrip(" ·")
            row.quantity = filled
        row.final = True


def _abandoned_to_unknown(user_id: int, account_ids: list[int] | None, row_ids: list[int] | None,
                          day_start: datetime) -> None:
    """Today's "pending" rows whose run is no longer working (it died after reserving them) become unknown,
    so they are looked up by tag. A row whose run is still sending is left alone: that run settles it."""
    flt: dict = {"user_id": user_id, "status": "pending", "run_id": {"$ne": None}, "created_at": {"$gte": day_start}}
    if row_ids is not None:
        flt["id"] = {"$in": list(row_ids)}
    if account_ids is not None:
        flt["account_id"] = {"$in": list(account_ids)}
    with session_scope() as db:
        rows = [(r.id, r.run_id) for r in db.find(OrderLog, flt, track=False)]
    if not rows:
        return
    with _live_guard:  # (after the read: a run that started since reserved its rows later than this snapshot)
        live = set(_live)
    _unsettled_to_unknown([i for i, run_id in rows if run_id not in live])


def reconcile_orders(user_id: int, account_ids: list[int] | None = None, row_ids: list[int] | None = None) -> int:
    """Bring today's TradeLab orders that haven't finished yet up to date from Kotak's order books.

    Late rejections, part fills, orders that lapsed unfilled and orders whose reply was lost are all
    settled here, so TradeLab's records of what it holds match what really happened. Returns how many
    rows changed. Kotak's order book lists only today's orders, so only today's rows are looked at.
    """
    day_start = _order_book_day_start()
    _abandoned_to_unknown(user_id, account_ids, row_ids, day_start)
    # (Not "pending": a run is still sending that order, and settles the row itself when the reply is back.)
    flt: dict = {"user_id": user_id, "action": {"$in": ["buy", "sell"]}, "account_id": {"$ne": None},
                 "status": {"$in": ["success", UNKNOWN]}, "final": {"$ne": True},
                 "created_at": {"$gte": day_start}}
    if row_ids is not None:
        flt["id"] = {"$in": list(row_ids)}
    if account_ids is not None:
        flt["account_id"] = {"$ne": None, "$in": list(account_ids)}
    with session_scope() as db:
        if not db.count(OrderLog, flt):
            return 0
        accounts = db.distinct(OrderLog, "account_id", flt)

    def book(acc_id: int):
        try:
            orders = broker_manager.call(acc_id, lambda b: b.orders())
            _note_tags(acc_id, orders)
            return acc_id, orders
        except Exception as exc:  # noqa: BLE001
            log.info("Could not read the order book of account %s: %s", acc_id, exc)
            return acc_id, None

    with _pool(len(accounts)) as ex:
        books = dict(ex.map(book, accounts))
    changed = 0
    now = utcnow()
    with session_scope() as db:
        for row in db.find(OrderLog, flt):
            orders = books.get(row.account_id)
            if orders is None:
                continue
            match = next((o for o in orders if row.order_id and o.order_id == row.order_id), None)
            if match is None and row.tag:
                match = next((o for o in orders if o.tag == row.tag), None)
            before = (row.status, row.quantity, row.final, row.price, row.order_id)
            filled_before = row.filled
            if match is not None:
                _apply_order(row, match)
            elif (row.status == UNKNOWN and row.tag and now - _aware(row.sent_at or row.created_at) > MISSING_AFTER
                  and (_tags_echoed(orders, row.exchange_segment or None, row.account_id)
                       or (row.exchange_segment and row.symbol and not _look_alike(orders, row)))):
                row.status = "failed"
                row.message = "Not placed: Kotak's order book has no such order"
                row.final = True
            if (row.status, row.quantity, row.final, row.price, row.order_id) != before or row.filled != filled_before:
                changed += 1
    return changed


def reconcile_today() -> None:
    """Settle every user's unfinished orders of today (scheduled before the order books are cleared)."""
    day_start = _order_book_day_start()
    with session_scope() as db:
        users = db.distinct(OrderLog, "user_id", {"final": {"$ne": True}, "created_at": {"$gte": day_start},
                                                  "status": {"$in": ["success", "pending", UNKNOWN]}})
    for user_id in users:
        try:
            reconcile_orders(user_id)
        except Exception:  # noqa: BLE001
            log.exception("Settling orders of user %s failed", user_id)


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
            for it in db.find(BasketItem, {"user_id": user_id, "section": section, "active": True},
                              sort=[("created_at", 1), ("id", 1)])
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
    return _busy(user_id, "entry", {section})


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _noun(section: str) -> str:
    return {"intraday": "stock", "holding": "stock or ETF"}.get(section, "contract")


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _live_dict(live: Live) -> dict:
    return {
        "mode": live.sizing_mode, "reserve": live.reserve_pct, "product": live.product, "max_lots": live.max_lots,
        "accounts": live.accounts, "items": sorted((i.to_dict() for i in live.items), key=lambda d: d["trading_symbol"]),
    }


def _fingerprint(section: str, day: date, buys_now: bool, entry_at: datetime, live: Live,
                 exit_settings: list | None = None) -> str:
    """Everything the "Are you sure?" dialog shows; confirming checks it hasn't changed since."""
    return _digest({"section": section, "day": day.isoformat(), "now": buys_now, "at": entry_at.isoformat(),
                    **({"exit": exit_settings} if exit_settings is not None else {}), **_live_dict(live)})


def _fingerprint_for(user_id: int, section: str, day: date, buys_now: bool, entry_at: datetime, live: Live) -> str:
    """The fingerprint of a Let's Trade dialog, including the square-off time and switch it shows."""
    exit_settings = None
    if spec(section).has_exit:
        with session_scope() as db:
            sett = get_section(db, user_id, section)
            exit_settings = [sett.exit_time, bool(sett.auto_exit)]
    return _fingerprint(section, day, buys_now, entry_at, live, exit_settings)


def _expired(live: Live, day: date) -> list[str]:
    return [i.symbol for i in live.items if i.expiry and date.fromisoformat(str(i.expiry)) < day]


def _session_q(user_id: int, section: str, day: date) -> dict:
    """Filter for a section's confirmation of `day` (at most one: a unique rule)."""
    return {"user_id": user_id, "section": section, "trade_date": day}


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
        sess = db.find_one(TradeSession, _session_q(user_id, section, day))
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
            for d in (s.trade_date for s in db.find(TradeSession, {
                "user_id": user_id,
                "section": section,
                "status": "armed",
                # Later days only: today's confirmation is either the one shown above or past its time.
                "trade_date": {"$ne": day, "$gt": now.date()},
            }, sort=[("trade_date", 1)]))
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
        "fingerprint": _fingerprint_for(user_id, section, day, buys_now, entry_at, live),
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
    if fingerprint != _fingerprint_for(user_id, section, day, buys_now, entry_at, live):
        raise SessionError("Something changed since this was shown (items, accounts, money or the time). "
                           "Check the details and confirm again.")
    if buys_now and _entry_busy(user_id, section):
        raise RunInProgress("The previous trade is still finishing. Try again in a few seconds.")
    try:
        with session_scope() as db:
            sess = db.find_one(TradeSession, _session_q(user_id, section, day))
            if sess is not None and sess.status in ("running", "firing", "done"):
                raise SessionError(f"{sp.label} already traded this day. You can confirm the next day after it.")
            if sess is None:
                sess = TradeSession(user_id=user_id, section=section, trade_date=day, sizing_mode=live.sizing_mode)
                db.add(sess)
            sess.status = "armed"
            sess.items = [i.to_dict() for i in live.items]
            sess.sizing_mode = live.sizing_mode
            sess.reserve_pct = live.reserve_pct
            sess.product = live.product
            sess.account_ids = accounts
            sess.confirmed_at = now_ist()  # the trading clock, which every timing rule here is checked against
            sess.note = None
    except IntegrityError:  # another tap confirmed this day at the same moment
        raise SessionError(f"{sp.label} was just confirmed for this day by another tap. Check the details.") from None
    run_id = None
    if buys_now:
        # The scheduled job has already started (or passed): trade from here.
        try:
            run_id = launch("entry", user_id, fire_at=max(entry_at, now), section=section)
        except RunInProgress:
            with session_scope() as db:
                db.update_many(TradeSession, {**_session_q(user_id, section, day), "status": "armed"},
                               {"status": "cancelled", "note": "Not confirmed: the previous trade was still finishing"})
            raise
    return {"run_id": run_id, **session_state(user_id, section)}


def cancel_session(user_id: int, trade_date: date | None = None, section: str = INTRADAY) -> dict:
    """Withdraw a "Let's Trade" confirmation. Works until the orders actually go out."""
    day = trade_date or plan_date(user_id, section)
    with session_scope() as db:
        cancelled = db.update_many(
            TradeSession,
            {**_session_q(user_id, section, day), "status": {"$in": ["armed", "running"]}},
            {"status": "cancelled", "note": "Cancelled by you"},
        )
        if not cancelled:
            sess = db.find_one(TradeSession, _session_q(user_id, section, day))
            status = sess.status if sess is not None else None
            raise SessionError("Nothing to cancel." if status in (None, "cancelled", "skipped")
                               else "Too late: the orders have already been sent.")
    return session_state(user_id, section)


def _claim_session(user_id: int, day: date, section: str = INTRADAY) -> TradeSession | None:
    """Atomically take the confirmed session for `day` so it can only be traded once."""
    with session_scope() as db:
        sess = db.find_one(TradeSession, _session_q(user_id, section, day))
        if sess is None:
            return None
        # Only one caller can move it from "armed": the update matches it at most once.
        claimed = db.update_one(TradeSession, {"id": sess.id, "status": "armed"}, {"status": "running"})
        if not claimed:
            return None
        db.expunge(sess)
        return sess


def _begin_firing(session_id: int) -> bool:
    """Atomically move a prepared session to "firing"; False if it was cancelled meanwhile."""
    with session_scope() as db:
        return bool(db.update_one(TradeSession, {"id": session_id, "status": "running"}, {"status": "firing"}))


def _session_waiting(session_id: int) -> bool:
    """The claimed session is still waiting for its moment (not cancelled)."""
    with session_scope() as db:
        return db.count(TradeSession, {"id": session_id, "status": "running"}) > 0


def _still_on_autopilot(user_id: int, plan: list[PlannedOrder]) -> list[PlannedOrder]:
    with session_scope() as db:
        on = {a.id for a in db.find(BrokerAccount, {"user_id": user_id, "automation_enabled": True})}
    return [p for p in plan if p.account_id in on]


def _finish_session(session_id: int, status: str, note: str | None = None) -> None:
    with session_scope() as db:
        sess = db.get(TradeSession, session_id)
        if sess is not None:
            sess.status = status
            sess.note = note


# ----------------------------------------------------------------------------- entry


def _market_check(user_id: int, fire_at: datetime | None, items: list[PlanItem] | None = None,
                  section: str = INTRADAY, still_wanted=None) -> tuple[bool, bool]:
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
    if fire_at is not None and not sleep_until(fire_at + MARKET_RECHECK, still_wanted):
        return False, True  # cancelled meanwhile; the caller sees that and stops
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
    if not sp.scheduled:
        # Holding, F&O and Commodity never buy by time, whatever an old confirmation says.
        log.warning("Refused a timed %s entry for user %s: it buys only on command", sp.label, user_id)
        if run_id is not None:
            _finish_run(run_id, [], note=f"{sp.label} buys only when you tap Buy — nothing was sent")
        return run_id
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
    finished = False

    def stop(entries: list[LogEntry], note: str | None, status: str, session_note: str | None = None) -> int:
        nonlocal finished
        _finish_run(run_id, entries, note=note)
        _finish_session(sess.id, status, session_note if session_note is not None else note)
        finished = True
        return run_id

    def waiting() -> bool:
        return _session_waiting(sess.id)

    try:
        items = [PlanItem(**d) for d in sess.items]
        with session_scope() as db:
            max_lots = get_section(db, user_id, section).max_lots or 10
        ready_by = None if fire_at is None else max(fire_at - READY_BEFORE, now_ist() + READY_MIN)
        plan = build_entry_plan(user_id, items, sess.sizing_mode, sess.reserve_pct or 0.0,
                                account_ids=sess.account_ids, section=section, product=sess.product,
                                max_lots=max_lots, ready_by=ready_by)
        if not plan:
            return stop([], "Nothing to trade (no autopilot accounts)", "skipped",
                        "Nothing was sent: no autopilot accounts")
        # Live holiday check for every entry, including "buy now" confirmations.
        closed, rechecked = _market_check(user_id, fire_at, items, section, waiting)
        if closed:
            note = "Market closed today (detected automatically), nothing was traded"
            if sp.calendar == "nse":
                _add_detected_holiday(today)
            return stop([], note, "skipped")
        if fire_at is not None:
            # After a market re-check the orders go out a little after the open.
            tolerance = ENTRY_LATE_TOLERANCE + (MARKET_RECHECK if rechecked else timedelta(0))

            def missed() -> int:
                entries = [
                    LogEntry(p.account_id, p.account_name, "buy" if p.side == "B" else "sell", "skipped",
                             p.trading_symbol, p.quantity or None, message="Entry window missed",
                             exchange_segment=p.exchange_segment, product=p.product)
                    for p in plan
                ]
                _write_logs(user_id, run_id, entries, section)
                return stop(entries, "Entry window missed", "skipped")

            if now_ist() > fire_at + tolerance:
                return missed()
            if not sleep_until(fire_at, waiting):
                _finish_run(run_id, [], note="Cancelled before the buy, nothing was sent")
                finished = True
                return run_id
            # The process may have been paused (laptop asleep, VM suspended) and woken late: never buy
            # late, and never on a day other than the confirmed one.
            now = now_ist()
            if now > fire_at + tolerance or now.date() != today:
                return missed()
        # Last moment: did today become a holiday, was it cancelled, or were accounts switched off?
        if not is_section_day(now_ist().date(), sp) or now_ist().date() != today:
            return stop([], "Today was marked as a market holiday, nothing was traded", "skipped")
        if not _begin_firing(sess.id):
            _finish_run(run_id, [], note="Cancelled before the buy, nothing was sent")
            finished = True
            return run_id
        plan = _still_on_autopilot(user_id, plan)
        entries = fire_plan(plan, user_id, run_id, section)
        _write_logs(user_id, run_id, _skip_entries(plan), section)  # sent orders were logged by fire_plan
        sent = any(e.status in ("success", UNKNOWN, "pending") for e in entries)
        if sent:
            stop(entries, None, "done")
        else:
            # Nothing went out (logins failed, every order refused): the day can be confirmed again.
            stop(entries, None, "skipped", "Nothing was bought — see Activity. You can confirm again.")
    except Exception as exc:  # noqa: BLE001
        log.exception("Entry run failed")
        entry = LogEntry(None, "—", "buy", "failed", message=f"Entry run crashed: {exc}")
        try:
            _write_logs(user_id, run_id, [entry], section)
            with session_scope() as db:
                firing = db.count(TradeSession, {"id": sess.id, "status": "firing"}) > 0
            # Orders may be out only once it was firing; before that nothing was sent.
            _finish_session(sess.id, "done" if firing else "skipped",
                            "Entry run crashed — check Activity and the Kotak app" if firing
                            else "Entry run crashed before sending anything. You can confirm again.")
        except Exception:  # noqa: BLE001
            log.exception("Recording the crashed entry failed")
        finally:
            _finish_run(run_id, [entry])
            finished = True
    finally:
        if not finished:
            _finish_run(run_id, [], note="Stopped unexpectedly")
    return run_id


# ----------------------------------------------------------------------------- exit


ALL = "all"  # the panic button: square off every section at once
# Panic closes of intraday stock positions are credited to Intraday. F&O and Commodity hold only
# carry-forward (NRML) positions, which the panic closes by their own records, so an intraday
# (MIS) contract it closes is someone else's and is credited to no section.
SEGMENT_SECTION = {"nse_cm": INTRADAY, "bse_cm": INTRADAY}


@dataclass
class Scope:
    """What a square-off may close. Only intraday-type (MIS) positions, ever: delivery
    (CNC), MTF and carry-forward (NRML) positions are never touched by a square-off.
    TradeLab's own NRML positions are closed separately, by quantity, from its order records.
    """

    section: str  # INTRADAY (stock MIS positions) or ALL (MIS positions in every segment)

    def covers(self, segment: str, product: str, symbol: str = "") -> bool:
        if (product or "").upper() not in SQUAREOFF_PRODUCTS or (segment or "") not in API_SEGMENTS:
            return False
        return self.section == ALL or (segment or "") in SECTIONS[INTRADAY].segments

    def log_section(self, segment: str, side: str) -> str | None:
        """The section a close is credited to in the order log.

        Intraday's square-off closes Intraday's positions. The panic button credits closes of intraday
        stock positions to Intraday; F&O and Commodity hold only carry-forward (NRML) positions, closed
        by their own records, so an intraday (MIS) contract it closes is someone else's and is credited
        to no section. Only closes of long positions are credited: a buy-to-cover is never something
        TradeLab bought.
        """
        if self.section == INTRADAY:
            return INTRADAY
        return SEGMENT_SECTION.get(segment) if side == "S" else None


# What each account's square-offs have sent today, per position: (day, {key: first net}, {key: close ids},
# {close id: when sent, for the closes Kotak never confirmed}). A square-off that follows another (the panic
# button tapped twice, "Square off now" at 14:30) counts the earlier closes like its own, so positions that
# lag behind them can't make it close the same units again.
_squared: dict[int, tuple[date, dict, dict, dict]] = {}
_squared_guard = threading.Lock()


def _earlier_closes(user_id: int, account_id: int) -> tuple[dict, dict, dict]:
    """Today's square-off closes of an account, from the order log (the in-memory memory is gone, say after a
    restart): per position, their order ids (or tags), when the unconfirmed ones were sent, and when the
    latest close of each position was."""
    sent: dict = {}
    when: dict = {}
    latest: dict = {}
    with session_scope() as db:
        runs = {r.id for r in db.find(ExecutionRun, {"user_id": user_id, "kind": "exit",
                                                      "trading_date": now_ist().date()}, track=False)}
        rows = db.find(OrderLog, {"user_id": user_id, "account_id": account_id, "run_id": {"$in": list(runs)},
                                  "action": {"$in": ["buy", "sell"]}, "status": {"$in": ["success", UNKNOWN]},
                                  "created_at": {"$gte": _order_book_day_start()}}, sort=[("id", 1)], track=False)
    for r in rows:
        if (r.product or "").upper() not in SQUAREOFF_PRODUCTS or r.exchange_segment not in API_SEGMENTS:
            continue
        ident = r.order_id or r.tag
        if not ident:
            continue
        k = (r.exchange_segment, r.symbol, (r.product or "").upper())
        sent.setdefault(k, []).append(ident)
        at = _aware(r.sent_at or r.created_at)
        latest[k] = max(latest.get(k, at), at)
        if not r.order_id:
            when[ident] = at
    return sent, when, latest


def square_off_account(acc: _Acc, scope: Scope, user_id: int | None = None,
                       run_id: int | None = None) -> list[LogEntry]:
    today = now_ist().date()
    with _squared_guard:
        known = _squared.get(acc.id)
        if known is not None and known[0] == today:
            _, first, sent, when = known
            first, sent, when = dict(first), {k: list(v) for k, v in sent.items()}, dict(when)
            known = True
    # After a restart the memory is rebuilt from the order log, which has the closes but not the position they
    # started from: `rebuilt` holds, per position, when its latest earlier close was sent, until that is worked out.
    rebuilt: dict = {}
    lost = False
    if known is not True:
        first, sent, when = {}, {}, {}
        if user_id is not None:
            try:
                sent, when, rebuilt = _earlier_closes(user_id, acc.id)
            except Exception:  # noqa: BLE001
                # Without them nothing says what was closed already: positions are held back and reported.
                log.exception("Reading today's earlier closes failed")
                lost = True
    flat: set = set()
    try:
        return _square_off_account(acc, scope, user_id, run_id, first, sent, flat, when, rebuilt, lost)
    finally:
        # A memory still missing what it was rebuilt without is not kept: the next run rebuilds it again.
        if not lost and not any(k not in flat for k in rebuilt):
            with _squared_guard:
                # A position Kotak has shown flat is done with; one opened later is a fresh one.
                kept = {k: v for k, v in sent.items() if k not in flat}
                _squared[acc.id] = (today, {k: v for k, v in first.items() if k not in flat}, kept,
                                    {i: t for i, t in when.items() if any(i in v for v in kept.values())})


def _square_off_account(acc: _Acc, scope: Scope, user_id: int | None, run_id: int | None,
                        first: dict, sent: dict, flat: set, when: dict, rebuilt: dict | None = None,
                        lost: bool = False) -> list[LogEntry]:
    """Cancel pending intraday orders and close every MIS position in `scope`, retrying until flat.

    A retry never closes more than the position first seen: closes already sent are looked up in
    Kotak's order book first, and while one is still working (or can't be seen) nothing more is sent
    for that position. So a positions view that lags behind a fill can't turn into a second close
    that flips the position to the other side.
    """
    entries: list[LogEntry] = []
    rebuilt = rebuilt if rebuilt is not None else {}
    # first: position -> signed net when first seen (what to close); sent: position -> ids (or tags) of closes
    # sent, today's earlier square-offs included.
    found: dict = {}
    # Positions with another order on them (say a stop-loss the holder set) that is being cancelled: they
    # are closed only once the order book shows that order gone. Closing next to an order still working
    # would flip the position when it fills.
    held_back: set[tuple[str, str, str]] = set()
    # Until this run has read the order book, every position is held back: an order on it (a stop-loss the
    # holder set, say) may be working, and it can't be seen.
    book_read = False
    # Orders whose cancel Kotak accepted, until its book shows them stopped: one may still fill (and open a
    # position) after a square-off that took Kotak's word for it.
    cancelling: dict[str, Order] = {}
    # Bought derivatives not sold yet because a sold contract of the same underlying is still open.
    hedge_held: set[tuple[str, str, str]] = set()
    odd_lots: set[tuple[str, str, str]] = set()  # derivative positions that are no whole number of lots

    def remembered(k: tuple[str, str, str]) -> bool:
        return not book_read or lost

    def call(fn):
        return broker_manager.call(acc.id, fn)

    def key(p) -> tuple[str, str, str]:  # noqa: ANN001
        return (p.exchange_segment or "", p.trading_symbol or "", (p.product or "").upper())

    for attempt in range(SQUAREOFF_ATTEMPTS):
        try:
            orders = call(lambda b: b.orders())
            books_ok = True
        except Exception as exc:  # noqa: BLE001
            entries.append(LogEntry(acc.id, acc.name, "cancel", "failed", message=f"Could not read orders: {exc}"))
            orders, books_ok = [], False
        found = {o.order_id: o for o in orders} | {o.tag: o for o in orders if o.tag}
        if books_ok:
            held_back = set()  # what the book shows now decides; an unreadable book keeps the last answer
            book_read = True
            for oid in [i for i, o in cancelling.items() if i in found and not found[i].is_open]:
                del cancelling[oid]
            # A close Kotak never confirmed, still missing from a book that reads, MISSING_AFTER after it was sent,
            # was never placed: it no longer blocks its position. (One that may still be working stays.)
            # Only when the book is proved to echo TradeLab's tags for that segment: otherwise it would lack a
            # placed close just the same.
            for k, ids in sent.items():
                if not _tags_echoed(orders, k[0]):
                    continue
                for i in [i for i in ids if i in when and i not in found and utcnow() - when[i] > MISSING_AFTER]:
                    ids.remove(i)
                    del when[i]
        ours = {i for ids in sent.values() for i in ids}
        # 1. Cancel pending intraday orders (not this square-off's own closes) so nothing new opens. One whose
        # cancel Kotak already accepted isn't cancelled again: the book is watched until it shows it stopped.
        for o in orders:
            if o.is_open and o.order_id in cancelling:
                held_back.add(key(o))
        pending = [o for o in orders if o.is_open and o.order_id not in ours and (o.tag or "") not in ours
                   and o.order_id not in cancelling and scope.covers(o.exchange_segment, o.product, o.trading_symbol)]
        for o in pending:
            held_back.add(key(o))  # until the book shows it gone, even when Kotak says "cancelled"
            try:
                res = call(lambda b, oid=o.order_id: b.cancel_order(oid))
                entries.append(LogEntry(acc.id, acc.name, "cancel", "success" if res.ok else "failed",
                                        o.trading_symbol, o.quantity, o.order_id, res.message,
                                        exchange_segment=o.exchange_segment, product=o.product))
                if res.ok:
                    cancelling[o.order_id] = o
            except Exception as exc:  # noqa: BLE001
                entries.append(LogEntry(acc.id, acc.name, "cancel", "failed", o.trading_symbol, o.quantity,
                                        o.order_id, str(exc), exchange_segment=o.exchange_segment, product=o.product))
        if pending:
            time.sleep(CANCEL_DELAY)

        # 2. Close what is still open, never more than the position first seen.
        try:
            positions: list[Position] = call(lambda b: b.positions())
        except Exception as exc:  # noqa: BLE001
            entries.append(LogEntry(acc.id, acc.name, "sell", "failed", message=f"Could not read positions: {exc}"))
            time.sleep(VERIFY_DELAY)
            continue
        net = {key(p): p for p in positions if p.net_qty != 0 and scope.covers(p.exchange_segment, p.product)}
        flat.clear()
        flat.update(k for k in (*first, *rebuilt) if k not in net)
        todo: list[tuple[Position, int]] = []
        working = bool(cancelling)  # a cancelled order not shown stopped yet may still fill: look again
        for k, p in net.items():
            if k in rebuilt and k not in first:
                # Closed earlier, before a restart: what it started from is its net now plus what those closes
                # filled, once Kotak's positions have caught up with them (a lagging view would count them
                # twice, and the rest closed again). Until then, or while one is working or unseen, wait.
                done = [found.get(i) for i in sent.get(k, [])]
                if (k in held_back or remembered(k) or not books_ok or any(o is None or o.is_open for o in done)
                        or utcnow() - rebuilt[k] < MISSING_AFTER):
                    working = True
                    continue
                first[k] = p.net_qty + (1 if p.net_qty > 0 else -1) * sum(max(o.filled_qty, 0) for o in done)
                del rebuilt[k]
            start = first.setdefault(k, p.net_qty)
            if (p.net_qty > 0) != (start > 0):
                continue  # on the other side of what was first seen: never touched (reported below)
            if k in held_back or remembered(k):
                working = True  # another order on it is being cancelled: close it once that is confirmed
                continue
            closes = [found.get(i) for i in sent.get(k, [])]
            if closes and (not books_ok or any(o is None or o.is_open for o in closes)):
                working = True  # an earlier close is still working, or can't be seen yet: wait for it
                continue
            filled = sum(max(o.filled_qty, 0) for o in closes if o is not None)
            left = min(abs(p.net_qty), abs(start) - filled)
            c = _contract(p.exchange_segment, p.trading_symbol) if p.exchange_segment in DERIVATIVE_SEGMENTS else None
            if left > 0 and c is not None and c.lot_size and left % c.lot_size:
                if k not in odd_lots:  # Kotak would refuse it (or its figure counts lots, not units)
                    odd_lots.add(k)
                    entries.append(LogEntry(acc.id, acc.name, "sell", "failed", p.trading_symbol, left, message=(
                        f"Not closed: {left} units is not a whole number of lots ({c.lot_size} units to a lot), "
                        "which Kotak would refuse, so nothing was sent. Check the Kotak Neo app and close it there."),
                        exchange_segment=p.exchange_segment, product=p.product))
                continue
            if left > 0:
                todo.append((p, left))
        # A bought derivative isn't sold while a sold one of the same underlying is still open in the account
        # (Kotak's positions order proves nothing): its buy-back goes first, and the sale waits until the
        # positions no longer show that short.
        shorts = [p for p in net.values() if p.net_qty < 0 and p.exchange_segment in DERIVATIVE_SEGMENTS]
        hedge_held.clear()
        for p, left in list(todo):
            if p.net_qty > 0 and p.exchange_segment in DERIVATIVE_SEGMENTS and any(_same_underlying(p, s) for s in shorts):
                todo.remove((p, left))
                hedge_held.add(key(p))
                working = True  # look again once the buy-back shows
        if not todo:
            if not working:
                if attempt == 0 and not net:
                    entries.append(LogEntry(acc.id, acc.name, "sell", "skipped", message="No open positions"))
                    return entries
                break
            time.sleep(VERIFY_DELAY)
            continue
        note = "" if attempt == 0 else f" (retry {attempt})"
        plan = [PlannedOrder(account_id=acc.id, account_name=acc.name, symbol=p.trading_symbol,
                             trading_symbol=p.trading_symbol, exchange_segment=p.exchange_segment,
                             side="S" if p.net_qty > 0 else "B", quantity=left, product=p.product or "MIS",
                             note=note.strip() or None)
                    for p, left in todo]
        plan = _slices(plan)
        sections = [scope.log_section(o.exchange_segment, o.side) for o in plan]
        ids = _reserve_logs(user_id, run_id, plan, sections) if run_id is not None else []
        tags = [f"TL{ids[i]}" if ids else f"SQ{secrets.token_hex(4)}" for i in range(len(plan))]
        answered: set[str] = set()
        try:
            for i, o in enumerate(plan):
                e = _place(o, tags[i], " The square-off checks it before closing anything more.")
                e.section = sections[i]
                entries.append(e)
                answered.add(tags[i])
                if e.status != "failed":  # a close Kotak refused outright isn't working: it may be sent again
                    sent.setdefault((o.exchange_segment, o.trading_symbol, (o.product or "").upper()), []).append(
                        e.order_id or tags[i])
                    if not e.order_id:
                        when[tags[i]] = utcnow()  # no order number: not confirmed until the book shows it
                if ids:
                    _settle_logs([ids[i]], [e])
        except BaseException:
            # Closes of this attempt that may have gone out (this one, or those after it) count as sent, and
            # their rows can't stay "pending": the next square-off looks them up in the order book first.
            for i, o in enumerate(plan):
                if tags[i] not in answered:
                    sent.setdefault((o.exchange_segment, o.trading_symbol, (o.product or "").upper()), []).append(
                        tags[i])
                    when[tags[i]] = utcnow()
            try:
                _unsettled_to_unknown(ids)
            except Exception:  # noqa: BLE001
                log.exception("Marking unsettled orders as unknown failed")
            raise
        time.sleep(VERIFY_DELAY)

    for o in cancelling.values():
        entries.append(LogEntry(acc.id, acc.name, "cancel", "failed", o.trading_symbol, o.quantity, o.order_id,
                                "Kotak accepted the cancel, but its order book doesn't show the order stopped yet: "
                                "what hasn't filled may still fill. Check the Kotak Neo app, then square off again.",
                                exchange_segment=o.exchange_segment, product=o.product))

    # 3. Final check, after giving the last closes a moment.
    try:
        left = [p for p in call(lambda b: b.positions())
                if p.net_qty != 0 and scope.covers(p.exchange_segment, p.product)]
        flat.clear()
        flat.update(k for k in (*first, *rebuilt) if k not in {key(p) for p in left})
    except Exception as exc:  # noqa: BLE001
        entries.append(LogEntry(acc.id, acc.name, "sell", "failed", message=f"Could not verify square-off: {exc}"))
        return entries
    for p in left:
        start = first.get(key(p))
        if start is not None and (p.net_qty > 0) != (start > 0):
            msg = (f"This position is now {p.net_qty:+d} — on the other side of the {start:+d} the square-off "
                   "started from. Check the Kotak Neo app and close it there.")
        elif lost:
            msg = ("Not closed: TradeLab couldn't read what it closed earlier today, so closing this again could "
                   "close it twice. Square off again, or close it in the Kotak Neo app.")
        elif key(p) in rebuilt:
            msg = ("Not closed yet: part of this position was closed earlier today and Kotak's positions may not "
                   "show that yet, so closing it now could close it twice. Square off again in a minute.")
        elif not book_read:
            msg = ("Not closed: Kotak's order book couldn't be read, so an order on this position (a stop-loss, "
                   "for example) couldn't be checked or cancelled, and closing beside it could open the opposite "
                   "position. Square off again, or close it in the Kotak Neo app.")
        elif key(p) in held_back:
            msg = ("Not closed: another order on this position (a stop-loss, for example) is still working at "
                   "Kotak and couldn't be cancelled, and closing next to it could flip the position. Cancel it "
                   "in the Kotak Neo app, then square off again.")
        elif key(p) in hedge_held:
            msg = ("Not sold: the sold leg it hedges couldn't be bought back first, so selling this one now could "
                   "leave that position without its hedge. Close again, or check the Kotak Neo app.")
        elif start is not None and sum(max(o.filled_qty, 0) for o in (found.get(i) for i in sent.get(key(p), []))
                                       if o is not None) >= abs(start):
            msg = ("Kotak's order book shows this position closed, but its positions don't show that yet. Check "
                   "again in a moment (TradeLab won't close it twice).")
        else:
            msg = "Position still open after square-off attempts — check the Kotak Neo app"
        entries.append(LogEntry(acc.id, acc.name, "sell", "failed", p.trading_symbol, abs(p.net_qty),
                                message=msg, exchange_segment=p.exchange_segment, product=p.product))
    return entries


def _traded_orders(user_id: int, day: date, section: str | None) -> list[tuple[int, str | None]]:
    """Distinct (account, symbol) of orders sent (or maybe sent) by the user's entry runs on `day`."""
    runs: dict = {"user_id": user_id, "kind": "entry", "trading_date": day}
    if section is not None:
        runs["section"] = section
    with session_scope() as db:
        run_ids = [r.id for r in db.find(ExecutionRun, runs)]
        if not run_ids:
            return []
        logs = db.find(OrderLog, {
            "run_id": {"$in": run_ids},
            "status": {"$in": ["success", "pending", UNKNOWN]},
            "account_id": {"$ne": None},
        })
        return list(dict.fromkeys((o.account_id, o.symbol) for o in logs))


def accounts_traded_today(user_id: int, day: date, section: str | None = None) -> set[int]:
    """Accounts where an entry run (of `section`, or any) sent or may have sent an order on `day`."""
    return {acc for acc, _ in _traded_orders(user_id, day, section)}


def _wait_for_entries(user_id: int, day: date, section: str | None) -> None:
    """Let a buy this server is still working on finish before deciding what to close (any section if None)."""
    deadline = time.monotonic() + ENTRY_WAIT.total_seconds()
    sections = None if section is None else {section}
    while time.monotonic() < deadline:
        if not _busy(user_id, "entry", sections):
            return
        time.sleep(0.25)


# When the user last pressed "Square off everything" (monotonic seconds), by user: an on-command Buy
# that was still preparing then is not sent.
_stopped_at: dict[int, float] = {}


def _cancel_own_open_orders(user_id: int, accounts: list[_Acc], section: str | None = None,
                            symbols: set[str] | None = None, exclude_run: int | None = None) -> list[LogEntry]:
    """Cancel TradeLab's own orders of today that are still working at Kotak (any product; only
    `section`'s and `symbols` if given; not those of run `exclude_run`, the closes the panic button just
    sent), so none of them fills after the panic button or an Exit."""
    day_start = _order_book_day_start()
    flt: dict = {"user_id": user_id, "account_id": {"$in": [a.id for a in accounts]},
                 "action": {"$in": ["buy", "sell"]}, "status": {"$in": ["success", "pending", UNKNOWN]},
                 "final": {"$ne": True}, "created_at": {"$gte": day_start}}
    if section is not None:
        flt["section"] = section
    if symbols:
        flt["symbol"] = {"$in": sorted(symbols)}
    if exclude_run is not None:
        flt["run_id"] = {"$ne": exclude_run}
    with session_scope() as db:
        rows = db.find(OrderLog, flt, track=False)
    mine: dict[int, set[str]] = {}
    for r in rows:
        mine.setdefault(r.account_id, set()).update(x for x in (r.order_id, r.tag) if x)
    entries: list[LogEntry] = []
    cancelled: dict[int, list[Order]] = {}  # account -> orders Kotak accepted a cancel for
    for acc in accounts:
        ids = mine.get(acc.id)
        if not ids:
            continue
        try:
            orders = broker_manager.call(acc.id, lambda b: b.orders())
        except Exception as exc:  # noqa: BLE001
            entries.append(LogEntry(acc.id, acc.name, "cancel", "failed",
                                    message=f"Could not read orders to cancel TradeLab's working orders: {exc}"))
            continue
        for o in orders:
            if o.is_open and (o.order_id in ids or (o.tag or "") in ids):
                try:
                    res = broker_manager.call(acc.id, lambda b, oid=o.order_id: b.cancel_order(oid))
                    entries.append(LogEntry(acc.id, acc.name, "cancel", "success" if res.ok else "failed",
                                            o.trading_symbol, o.quantity, o.order_id, res.message,
                                            exchange_segment=o.exchange_segment, product=o.product))
                    if res.ok:
                        cancelled.setdefault(acc.id, []).append(o)
                except Exception as exc:  # noqa: BLE001
                    entries.append(LogEntry(acc.id, acc.name, "cancel", "failed", o.trading_symbol, o.quantity,
                                            o.order_id, str(exc), exchange_segment=o.exchange_segment,
                                            product=o.product))
    if cancelled:
        time.sleep(CANCEL_DELAY)
        entries += _confirm_cancels({a.id: a for a in accounts}, cancelled)
    return entries


def _confirm_cancels(accounts: dict[int, _Acc], cancelled: dict[int, list[Order]]) -> list[LogEntry]:
    """Kotak accepting a cancel doesn't stop the order at once, and what is closed next is sized from the
    order book: an order still shown working there may yet fill. Every account's book is looked at, all at
    once, a few times; an order still working after that (or not to be checked) is reported, so the run is
    never taken as done."""
    left = {acc_id: {o.order_id: o for o in orders} for acc_id, orders in cancelled.items()}
    problems: dict[int, str] = {}

    def look(acc_id: int) -> None:
        try:
            book = {o.order_id: o for o in broker_manager.call(acc_id, lambda b: b.orders())}
        except Exception as exc:  # noqa: BLE001
            problems[acc_id] = f" (its order book couldn't be read: {exc})"
            return
        problems.pop(acc_id, None)
        left[acc_id] = {i: o for i, o in left[acc_id].items() if i not in book or book[i].is_open}

    for attempt in range(CANCEL_CHECKS):
        waiting = [acc_id for acc_id, orders in left.items() if orders]
        if not waiting:
            break
        if attempt:
            time.sleep(CANCEL_DELAY)
        with _pool(len(waiting)) as ex:
            list(ex.map(look, waiting))
    out: list[LogEntry] = []
    for acc_id, orders in left.items():
        acc = accounts[acc_id]
        for o in orders.values():
            out.append(LogEntry(acc.id, acc.name, "cancel", "failed", o.trading_symbol, o.quantity, o.order_id,
                                "Kotak accepted the cancel, but its order book doesn't show the order stopped yet"
                                f"{problems.get(acc_id, '')}: what hasn't filled may still fill. Check the Kotak Neo "
                                "app, then close again.", exchange_segment=o.exchange_segment, product=o.product))
    return out


def _cancel_armed_today(user_id: int) -> int:
    """The panic button also withdraws today's confirmations that haven't bought yet."""
    with session_scope() as db:
        return db.update_many(
            TradeSession,
            {"user_id": user_id, "trade_date": now_ist().date(),
             # "running" = claimed and waiting for its moment; cancelling it stops the buy.
             "status": {"$in": ["armed", "running"]}},
            {"status": "cancelled", "note": "Cancelled by “Square off everything”"},
        )


def run_exit(
    user_id: int, trigger: str = "scheduled", account_ids: list[int] | None = None, run_id: int | None = None,
    section: str = INTRADAY,
) -> int | None:
    """Square off Intraday (automatically at its exit time, or now), or everything (ALL, the panic button).

    ALL closes every intraday (MIS) position in every segment plus the carry-forward (NRML) contracts
    F&O and Commodity opened, after cancelling TradeLab's own orders still working; delivery holdings
    are never sold by it. A run that was started (run_id given) is always finished, whatever happens.
    """
    today = now_ist().date()
    automatic = trigger in ("scheduled", "catchup")
    entries: list[LogEntry] = []
    note: str | None = None
    accounts: list[_Acc] = []
    status = None
    lost: Exception | None = None  # an automatic run that crashed before it had a run row
    unsaved = False  # an automatic run whose end couldn't be saved
    try:
        if section != ALL and not spec(section).has_exit:
            note = f"{label(section)} has no square-off"
            return run_id
        if section == ALL and account_ids is None:  # (one account's "Square off" doesn't stop other accounts' Buys)
            _stopped_at[user_id] = time.monotonic()
        if automatic:
            if section == ALL:
                return run_id
            # Never after the section's last moment to sell (a stalled job waking late would race Kotak's
            # own 15:05 square-off), and never once the owner has switched Automatic square-off off.
            with session_scope() as db:
                if not get_section(db, user_id, section).auto_exit:
                    return run_id
            if now_ist().time().replace(tzinfo=None) >= spec(section).sell_until:
                _record_missed_exit(user_id, section, trigger, today)
                return run_id
            if _already_ran(user_id, "exit", today, section):
                return run_id
            # The automatic square-off only touches accounts where this section itself traded today.
            # Nothing confirmed / nothing filled -> no orders at all, and trades placed by hand in the
            # Kotak app in other accounts are left alone. (A holiday added by mistake after the entry
            # can't block this either.)
            _wait_for_entries(user_id, today, section)
            account_ids = sorted(accounts_traded_today(user_id, today, section))
            if not account_ids:
                return run_id
        with session_scope() as db:
            flt: dict = {"user_id": user_id}
            if account_ids is not None:
                flt["id"] = {"$in": list(account_ids)}
            # (Automatic runs include accounts whose autopilot was switched off after the buy.)
            accounts = [_Acc(a.id, a.name, a.broker) for a in db.find(BrokerAccount, flt, sort=[("id", 1)])]
        if automatic and not accounts:
            return run_id
        run_id = run_id or _start_run(user_id, "exit", trigger, section)
        _set_scope(run_id, [a.id for a in accounts])
        if section == ALL and account_ids is None:
            # Closing positions matters most: a problem here never stops the square-off itself.
            try:
                if _cancel_armed_today(user_id):
                    note = "Today's Let's Trade confirmations were cancelled"
            except Exception as exc:  # noqa: BLE001
                log.exception("Cancelling today's confirmations failed")
                entries.append(LogEntry(None, "—", "cancel", "failed",
                                        message=f"Could not cancel today's Let's Trade confirmations: {exc}"))
        # A buy already on its way lands first, so it is closed too instead of opening right after the
        # square-off. (A Let's Trade the panic just cancelled stops at once; an on-command Buy still
        # preparing is not sent.)
        _wait_for_entries(user_id, today, None if section == ALL else section)
        if not accounts:
            note = "No accounts to square off"
            return run_id
        # Never let two square-offs for the same user overlap: both would see the same open position.
        with _exit_lock(user_id):
            carried: list[LogEntry] = []  # carry-forward (NRML) closes of the panic button
            try:
                def square_off(a: _Acc) -> list[LogEntry]:
                    try:
                        return square_off_account(a, Scope(section), user_id, run_id)
                    except Exception as exc:  # noqa: BLE001 - the other accounts' results must not be lost
                        log.exception("Square-off of account %s failed", a.id)
                        return [LogEntry(a.id, a.name, "sell", "failed", message=f"Square-off crashed: {exc}")]

                def carry_forward(a: _Acc) -> list[LogEntry]:
                    # TradeLab's own carry-forward contracts: first stop its orders still working, then
                    # close what it holds, by the quantity it opened. (NRML and MIS are different positions,
                    # so this never closes what the account's square-off does.)
                    out: list[LogEntry] = []
                    try:
                        cancels = _cancel_own_open_orders(user_id, [a], exclude_run=run_id)
                        _write_logs(user_id, run_id, cancels)
                        out += cancels
                        reconcile_orders(user_id, [a.id])
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Cancelling TradeLab's working orders failed")
                        out.append(LogEntry(a.id, a.name, "cancel", "failed",
                                            message=f"Cancelling TradeLab's working orders failed: {exc}"))

                    def close(key: str) -> list[LogEntry]:
                        res: list[LogEntry] = []
                        try:
                            plan = build_sell_plan(user_id, key, account_ids=[a.id], products={"NRML"}, panic=True)
                            res += fire_plan([p for p in plan if not p.skip], user_id, run_id, key, closing=True,
                                             unclosed=[p for p in plan if p.skip and p.error])
                            strays = _stray_orders(user_id, plan)
                            _write_logs(user_id, run_id, strays, key)
                            res += strays
                            # A contract that couldn't be checked (login, positions) is a failure, not "done".
                            problems = [p for p in plan if p.error]
                            _write_logs(user_id, run_id, _skip_entries(problems), key)
                            res += _skip_entries(problems)
                        except Exception as exc:  # noqa: BLE001
                            log.exception("Selling %s contracts failed", key)
                            crashed = LogEntry(a.id, a.name, "sell", "failed", message=f"{label(key)} sell crashed: {exc}")
                            _write_logs(user_id, run_id, [crashed], key)
                            res.append(crashed)
                        return res

                    with _account_pool(2) as kex:
                        for res in kex.map(close, ("fno", "commodity")):
                            out += res
                    return out

                def account_close(a: _Acc) -> tuple[list[LogEntry], list[LogEntry]]:
                    # Each account's carry-forward closes start when its own square-off ends, not when every
                    # account's has: one slow account must not leave another's sold contracts open.
                    intraday = square_off(a)
                    if section != ALL:
                        return intraday, []
                    try:
                        return intraday, carry_forward(a)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Closing carry-forward contracts of account %s failed", a.id)
                        crashed = LogEntry(a.id, a.name, "sell", "failed", message=f"Carry-forward close crashed: {exc}")
                        _write_logs(user_id, run_id, [crashed])
                        return intraday, [crashed]

                with _account_pool(len(accounts)) as ex:
                    for result, carry in ex.map(account_close, accounts):
                        entries.extend(result)
                        carried.extend(carry)
            except Exception as exc:  # noqa: BLE001
                log.exception("Square-off run failed")
                entries.append(LogEntry(None, "—", "sell", "failed", message=f"Square-off crashed: {exc}"))
            try:
                _write_logs(user_id, run_id, entries, INTRADAY if section == INTRADAY else None)
            except Exception:  # noqa: BLE001 - the closes themselves were recorded before sending
                log.exception("Saving square-off log failed")
            entries += carried
    except Exception as exc:  # noqa: BLE001
        log.exception("Square-off run failed")
        entries.append(LogEntry(None, "—", "sell", "failed", message=f"Square-off crashed: {exc}"))
        if automatic and run_id is None:
            lost = exc
    finally:
        if run_id is not None:
            try:
                status = _finish_run(run_id, entries, note=note)
            except Exception:  # noqa: BLE001 - an automatic run is tried again; a manual one tells its caller
                if not automatic:
                    raise
                log.exception("Saving the end of the square-off run failed")
                unsaved = True
    if lost is not None:
        _record_lost_exit(user_id, section, trigger, lost)
    if automatic and (lost is not None or unsaved or (status is not None and status != "done")):
        _schedule_exit_retry(user_id, section)
    if accounts and run_id is not None and entries:
        try:
            from .reports import capture

            capture(user_id, [a.id for a in accounts])  # keep the day's P/L for the report
        except Exception:  # noqa: BLE001
            log.exception("Saving P/L after square-off failed")
    return run_id


_missed_exit_lock = threading.Lock()


def _record_lost_exit(user_id: int, section: str, trigger: str, exc: Exception) -> None:
    """An automatic square-off that crashed before it had a run row leaves a failed one behind, so the
    owner gets the pop-up (it doesn't count as having run, so the retry still goes ahead)."""
    try:
        run_id = _start_run(user_id, "exit", trigger, section)
        _finish_run(run_id, [LogEntry(None, "—", "sell", "failed", message=f"Square-off crashed: {exc}")],
                    note="The square-off could not start")
    except Exception:  # noqa: BLE001 - the database is still down; the retry is all that is left
        log.exception("Recording the lost square-off failed")


def _set_scope(run_id: int, account_ids: list[int], symbols: set[str] | None = None) -> None:
    """Remember which accounts (and contracts) an exit run covers, so a pop-up for a failure elsewhere stays."""
    try:
        with session_scope() as db:
            db.update_many(ExecutionRun, {"id": run_id},
                           {"scope_accounts": sorted(account_ids), "scope_symbols": sorted(symbols) if symbols else None})
    except Exception:  # noqa: BLE001 - without it the run covers nothing, which only keeps pop-ups
        log.exception("Recording what the run covers failed")


def _record_missed_exit(user_id: int, section: str, trigger: str, today: date) -> None:
    """An automatic square-off that only woke after the section's last moment to sell leaves a failed run
    behind, once, when the section traded today and no square-off ran, so the owner gets the pop-up."""
    try:
        with _missed_exit_lock:
            traded = accounts_traded_today(user_id, today, section)
            if _already_ran(user_id, "exit", today, section) or not traded:
                return
            with session_scope() as db:
                # A square-off the owner ran by hand (of this section, or of everything for all these accounts)
                # has its own result and pop-up; "didn't run" would be false.
                for r in db.find(ExecutionRun, {"user_id": user_id, "kind": "exit", "trading_date": today,
                                                "section": {"$in": [section, ALL]}}, track=False):
                    if (r.summary or "").startswith("The square-off didn't run"):
                        continue
                    if r.section == section or (r.scope_accounts is not None and traded <= set(r.scope_accounts)):
                        return
                if db.find(ExecutionRun, {"user_id": user_id, "kind": "exit", "section": section,
                                          "trading_date": today, "summary": {"$regex": "^The square-off didn't run"}},
                           track=False):
                    return
            msg = (f"The square-off didn't run before {spec(section).sell_until.strftime('%H:%M')}: TradeLab was "
                   "not running. Kotak's own square-off closes intraday positions; check the Kotak Neo app.")
            run_id = _start_run(user_id, "exit", trigger, section)
            entries = [LogEntry(None, "—", "sell", "failed", message=msg)]
            _write_logs(user_id, run_id, entries, section)
            _finish_run(run_id, entries, note=msg)
    except Exception:  # noqa: BLE001 - nothing more can be done; the positions are Kotak's to close
        log.exception("Recording the missed square-off failed")


def _schedule_exit_retry(user_id: int, section: str) -> None:
    """Try a failed automatic square-off again in a minute, while the section can still sell today."""
    sp = spec(section)
    when = now_ist() + EXIT_RETRY
    if when.time().replace(tzinfo=None) >= sp.sell_until or when.date() != now_ist().date():
        return
    t = threading.Timer(EXIT_RETRY.total_seconds(), run_exit, args=(user_id, "catchup"), kwargs={"section": section})
    t.daemon = True
    t.start()


# ----------------------------------------------------------------------------- what TradeLab holds


@dataclass(frozen=True)
class Lot:
    """A position key: account, segment, trading symbol and product."""

    account_id: int
    exchange_segment: str
    trading_symbol: str
    product: str


@dataclass
class Held:
    """What a section opened and still has open in one account, by TradeLab's own records."""

    qty: int  # > 0: bought (long); < 0: sold first (short, e.g. a written option)
    cost: float | None = None  # TradeLab's average entry price per unit (None if a fill price is missing)


def tradelab_book(user_id: int, section: str, account_ids: list[int] | None = None,
                  symbols: set[str] | None = None, products: set[str] | None = None,
                  count_fills: bool = True, closes_by_fill: bool = False,
                  closes_proved: bool = False) -> dict[Lot, Held]:
    """Positions `section` opened and hasn't closed yet, from TradeLab's own order records.

    Orders sent by Buy now / Let's Trade (entry runs) open positions; Sell, square-offs and the
    panic button (exit runs) close them. An opening order counts only once the broker accepted
    it; a closing order counts as soon as it may have gone through, so an unclear reply never
    leads to closing twice. Anything bought or sold by hand in the Kotak app is never included.
    Intraday (MIS) products count only today's orders, since they can't be carried over.
    `closes_by_fill` (for what the screens show): a close still working counts by what has filled, so
    what it hasn't closed yet still shows (closing again cancels it first).
    `closes_proved`: a close counts only by what Kotak's book has shown filled (one still working, unknown or
    not yet seen in the book counts 0), for what must stay open until a close is proved (a sold leg's hedge).
    """
    products = products if products is not None else set(spec(section).products)
    day_start = datetime.combine(now_ist().date(), dtime(0, 0), tzinfo=IST).astimezone(timezone.utc)
    flt: dict = {"user_id": user_id, "section": section, "account_id": {"$ne": None},
                 "action": {"$in": ["buy", "sell"]}, "quantity": {"$gt": 0}}
    if account_ids is not None:
        flt["account_id"] = {"$ne": None, "$in": list(account_ids)}
    # The whole history can be long and is only read here: fetch just the fields needed, as plain data.
    fields = ("account_id", "exchange_segment", "symbol", "product", "action", "status", "quantity", "price",
              "created_at", "final", "filled", "run_id")
    logs = list(collection(OrderLog).find(flt, dict.fromkeys(fields, 1), sort=[("created_at", 1), ("_id", 1)]))
    # Each order's run kind; None when it has no run or its run was deleted (it opens nothing).
    run_ids = sorted({o.get("run_id") for o in logs} - {None})
    kinds = {r["_id"]: r.get("kind") for r in collection(ExecutionRun).find({"_id": {"$in": run_ids}}, {"kind": 1})}
    rows = [(*(o.get(f) for f in fields[:-1]), kinds.get(o.get("run_id"))) for o in logs]
    book: dict[Lot, list] = {}  # key -> [net, cost qty, cost amount, cost known]
    for acc, seg, sym, prod, action, status, qty, price, created, final, filled, kind in rows:
        prod = (prod or "").upper()
        if not seg or not sym or (symbols is not None and sym not in symbols) or prod not in products:
            continue
        if prod in INTRADAY_PRODUCTS and _aware(created) < day_start:
            continue
        if kind is None and status != ADJUSTED:
            continue  # an order whose run is gone opens and closes nothing
        opening = kind == "entry"
        if not (status == "success" if opening else status in ("success", UNKNOWN, "pending", ADJUSTED)):
            continue
        if not opening and status == UNKNOWN and not final and _aware(created) < day_start:
            # Never resolved by the end of its day (Kotak's book is gone): not counted as closed. Kotak's figure
            # decides: the units are written down if it went through, closed by the next Exit if it didn't.
            qty = min(qty, filled or 0)
            if qty <= 0:
                continue
        elif closes_proved and not opening and not final:
            qty = min(qty, filled or 0)
            if qty <= 0:
                continue
        elif not final and filled is not None and ((count_fills and opening) or (closes_by_fill and not opening)):
            qty = min(qty, filled)  # still working at Kotak: only what has filled counts
            if qty <= 0:
                continue
        delta = qty if action == "buy" else -qty
        b = book.setdefault(Lot(acc, seg, sym, prod), [0, 0, 0.0, True])
        old, new = b[0], b[0] + delta
        known = bool(price and price > 0)
        if old == 0 or (old > 0) == (delta > 0):
            if opening:  # adds to the position: its average cost takes this order in
                if old == 0:
                    b[1:] = [0, 0.0, True]  # a fresh position: its cost starts again
                b[1] += qty
                b[2] += qty * price if known else 0.0
                b[3] = b[3] and known
        elif new != 0 and (new > 0) != (old > 0):
            # Through zero in one order (e.g. a long turned into a sold-first position): what is
            # left open was opened by this order, at its price.
            b[1:] = [abs(new), abs(new) * price if known else 0.0, known]
        elif b[1]:
            # Part closed: the units still open keep their average cost.
            ratio = abs(new) / abs(old)
            b[1] *= ratio
            b[2] *= ratio
        b[0] = new
        if b[0] == 0:
            b[1:] = [0, 0.0, True]
    return {k: Held(b[0], b[2] / b[1] if b[3] and b[1] else None) for k, b in book.items() if b[0] != 0}


# Status of a record that brings TradeLab's own records down to what Kotak shows, after a position it
# opened was closed outside TradeLab (Kotak's risk system, a tender-period close, by hand, at expiry).
ADJUSTED = "adjusted"


def tradelab_open(user_id: int, section: str, account_ids: list[int] | None = None,
                  symbols: set[str] | None = None, products: set[str] | None = None,
                  closes_by_fill: bool = False, closes_proved: bool = False) -> dict[Lot, int]:
    """Units `section` still has open per key: positive = bought, negative = sold first."""
    return {k: h.qty for k, h in tradelab_book(user_id, section, account_ids, symbols, products,
                                               closes_by_fill=closes_by_fill, closes_proved=closes_proved).items()}


@dataclass
class BrokerView:
    """What Kotak shows for some position keys of one account."""

    held: dict[Lot, int]  # signed; delivery (CNC): positions plus sellable holdings
    total: dict[Lot, int]  # signed; delivery (CNC): positions plus all holdings (pledged too)
    listed: set[Lot]  # keys with a row in today's positions (or holdings)
    rows: int  # rows the positions and holdings calls returned in all
    day_bought: dict[Lot, int] = field(default_factory=dict)  # units today's positions show bought today
    day_sold: dict[Lot, int] = field(default_factory=dict)  # ... and sold today
    # Derivative keys with no row, while a row of the same instrument (its token, or its symbol in other
    # letters) is listed under another spelling: Kotak may name the contract differently than TradeLab does.
    respelled: dict[Lot, str] = field(default_factory=dict)


def broker_view(acc_id: int, wanted: list[Lot]) -> BrokerView:
    positions = broker_manager.call(acc_id, lambda b: b.positions())
    net: dict[tuple[str, str, str], int] = {}
    day: dict[tuple[str, str, str], list[int]] = {}
    for p in positions:
        k = (p.exchange_segment, p.trading_symbol, (p.product or "").upper())
        net[k] = net.get(k, 0) + p.net_qty
        d = day.setdefault(k, [0, 0])
        d[0] += max(p.day_buy_qty, 0)
        d[1] += max(p.day_sell_qty, 0)
    sellable: dict[str, int] = {}
    owned: dict[str, int] = {}
    holdings = broker_manager.call(acc_id, lambda b: b.holdings()) if any(w.product == "CNC" for w in wanted) else []
    for h in holdings:
        sellable[h.trading_symbol] = sellable.get(h.trading_symbol, 0) + h.sellable_qty
        owned[h.trading_symbol] = owned.get(h.trading_symbol, 0) + h.quantity
    view = BrokerView({}, {}, set(), len(positions) + len(holdings))
    for w in wanted:
        k = (w.exchange_segment, w.trading_symbol, w.product)
        n = net.get(k, 0)
        if k in net:
            view.day_bought[w], view.day_sold[w] = day[k]
        if k in net or (w.product == "CNC" and w.trading_symbol in owned):
            view.listed.add(w)
        elif w.product != "CNC" and w.exchange_segment in DERIVATIVE_SEGMENTS:
            c = _contract(w.exchange_segment, w.trading_symbol)
            same = next((p for p in positions if p.exchange_segment == w.exchange_segment
                         and (p.product or "").upper() == w.product
                         and (p.trading_symbol.lower() == w.trading_symbol.lower()
                              or (c is not None and c.token and p.token == c.token))), None)
            if same is not None:
                view.respelled[w] = same.trading_symbol
        if w.product == "CNC":
            view.held[w] = max(n, 0) + sellable.get(w.trading_symbol, 0)
            view.total[w] = max(n, 0) + owned.get(w.trading_symbol, 0)
        else:
            view.held[w] = view.total[w] = n
    return view


DERIVATIVE_SEGMENTS = ("nse_fo", "bse_fo", "mcx_fo")


def _contract_changed(key: Lot) -> str | None:
    """Why a derivative contract TradeLab holds no longer matches its records (a bonus, split or similar adjustment
    by the exchange multiplies the units and lot size, and moves options into new contracts), or None."""
    if key.exchange_segment not in DERIVATIVE_SEGMENTS or key.product == "CNC":
        return None
    with session_scope() as db:
        rows = db.find(OrderLog, {"account_id": key.account_id, "exchange_segment": key.exchange_segment,
                                  "symbol": key.trading_symbol, "lot_size": {"$ne": None}},
                       sort=[("id", -1)], limit=1, track=False)
    if not rows:
        return None
    rec, c = rows[0], _contract(key.exchange_segment, key.trading_symbol)
    if c is None and rec.expiry and rec.expiry >= now_ist().date().isoformat():
        return ("This contract left Kotak's contract list before its expiry (a bonus or split moves positions into "
                "an adjusted contract), so TradeLab's records no longer match it and nothing is sent. Close it in "
                "the Kotak Neo app.")
    if c is not None and c.lot_size and rec.lot_size and c.lot_size != rec.lot_size:
        return (f"This contract's lot size changed from {rec.lot_size} to {c.lot_size} since TradeLab opened it (a "
                "bonus or split), so TradeLab's records no longer match Kotak's units and nothing is sent. Close it "
                "in the Kotak Neo app.")
    return None


def _lot_of(key: Lot) -> int | None:
    """Units per lot of a derivative TradeLab opened (as recorded when it was opened, else today's list)."""
    if key.exchange_segment not in DERIVATIVE_SEGMENTS or key.product == "CNC":
        return None
    with session_scope() as db:
        rows = db.find(OrderLog, {"account_id": key.account_id, "exchange_segment": key.exchange_segment,
                                  "symbol": key.trading_symbol, "lot_size": {"$ne": None}},
                       sort=[("id", -1)], limit=1, track=False)
    c = _contract(key.exchange_segment, key.trading_symbol)
    return (rows[0].lot_size if rows else None) or (c.lot_size if c else None)


def _units_doubt(key: Lot, ours: int, broker: int, shown: int, view: BrokerView) -> str | None:
    """Why Kotak's figure for a derivative can't be taken as a real position of that contract (so the records
    are kept and Exit reports it instead of closing a guess), or None."""
    if key.exchange_segment not in DERIVATIVE_SEGMENTS or key.product == "CNC":
        return None
    if key in view.respelled:
        return (f"Kotak's positions don't list {key.trading_symbol}, but list {view.respelled[key]}, which looks like "
                "the same contract spelled differently, so TradeLab's records were kept and nothing is sent. Check "
                "the Kotak Neo app and close it there.")
    lot = _lot_of(key)
    # A position is always whole lots; Kotak showing a part of one, or exactly TradeLab's lots, is not units.
    if lot and lot > 1 and shown > 0 and (shown % lot or shown * lot == abs(ours)):
        return (f"Kotak shows {broker} here but TradeLab opened {ours} ({lot} units to a lot): that can't be a real "
                "position of this contract, or Kotak counts lots instead of units, so TradeLab's records were kept "
                "and nothing is sent. Check the Kotak Neo app and close it there.")
    return None


def _expired_contract(key: Lot) -> bool:
    c = _contract(key.exchange_segment, key.trading_symbol)
    if c is not None:
        return bool(c.expiry and c.expiry < now_ist().date())
    # Not in today's list: a contract that expired (or was withdrawn). Delivery stocks are never "expired".
    return key.product != "CNC" and key.exchange_segment in ("nse_fo", "bse_fo", "mcx_fo")


def _open_rows_today(user_id: int, account_ids: list[int] | None, flt_extra: dict) -> list:
    flt: dict = {"user_id": user_id, "action": {"$in": ["buy", "sell"]}, "account_id": {"$ne": None},
                 "final": {"$ne": True}, "created_at": {"$gte": _order_book_day_start()}, **flt_extra}
    if account_ids is not None:
        flt["account_id"] = {"$ne": None, "$in": list(account_ids)}
    with session_scope() as db:
        return db.find(OrderLog, flt, track=False)


def _row_key(r: OrderLog) -> Lot:
    return Lot(r.account_id, r.exchange_segment or "", r.symbol or "", (r.product or "").upper())


def unfinished_keys(user_id: int, account_ids: list[int] | None = None) -> set[Lot]:
    """Position keys with a TradeLab order of today that hasn't finished at Kotak. Their records are not
    brought down to Kotak's figure meanwhile: Kotak's positions can lag a fill its order book already shows."""
    rows = _open_rows_today(user_id, account_ids, {"status": {"$in": ["success", "pending", UNKNOWN]}})
    return {_row_key(r) for r in rows}


def unconfirmed_orders(user_id: int, account_ids: list[int] | None = None) -> dict[Lot, int]:
    """Units (signed: + bought, - sold first) of TradeLab's opening orders of today that Kotak accepted but its
    order book hasn't shown yet, by position key. How much of them filled isn't known, and TradeLab's records
    count them in full, so Exit doesn't size a close from those records (it could take in the holder's own
    units). Working orders the book has shown count by what filled."""
    rows = _open_rows_today(user_id, account_ids, {"status": "success", "filled": None})
    run_ids = sorted({r.run_id for r in rows if r.run_id is not None})
    entries = {r["_id"] for r in collection(ExecutionRun).find({"_id": {"$in": run_ids}, "kind": "entry"}, {"_id": 1})}
    out: dict[Lot, int] = {}
    for r in rows:
        if r.run_id in entries and r.quantity:
            out[_row_key(r)] = out.get(_row_key(r), 0) + (r.quantity if r.action == "buy" else -r.quantity)
    return out


def unresolved_closes(user_id: int, section: str, account_ids: list[int] | None = None) -> dict[Lot, OrderLog]:
    """TradeLab's closing orders of today with no known outcome (sent, no clear reply, not in Kotak's order book
    and not proved missing), by position key: the oldest per key. Whether the position is still open depends on
    them, so nothing may call it closed (or send another close) meanwhile."""
    rows = _open_rows_today(user_id, account_ids, {"status": UNKNOWN, "section": section})
    run_ids = sorted({r.run_id for r in rows if r.run_id is not None})
    entries = {r["_id"] for r in collection(ExecutionRun).find({"_id": {"$in": run_ids}, "kind": "entry"}, {"_id": 1})}
    out: dict[Lot, OrderLog] = {}
    for r in sorted(rows, key=lambda r: r.created_at):
        if r.run_id not in entries and r.quantity and r.exchange_segment and r.symbol:
            out.setdefault(_row_key(r), r)
    return out


def unresolved_entries(user_id: int, section: str, account_ids: list[int] | None = None) -> dict[Lot, OrderLog]:
    """TradeLab's opening orders of any day with no known outcome (sent, no clear reply, never found in Kotak's
    order book), by position key: the oldest per key. They are not in TradeLab's records, yet the position may
    exist, so Exit can neither close it nor call the key empty. Intraday (MIS) rows of earlier days can't be open."""
    flt: dict = {"user_id": user_id, "action": {"$in": ["buy", "sell"]}, "account_id": {"$ne": None},
                 "final": {"$ne": True}, "status": UNKNOWN, "section": section}
    if account_ids is not None:
        flt["account_id"] = {"$ne": None, "$in": list(account_ids)}
    with session_scope() as db:
        rows = db.find(OrderLog, flt, track=False)
    run_ids = sorted({r.run_id for r in rows if r.run_id is not None})
    entries = {r["_id"] for r in collection(ExecutionRun).find({"_id": {"$in": run_ids}, "kind": "entry"}, {"_id": 1})}
    day_start = _order_book_day_start()
    out: dict[Lot, OrderLog] = {}
    for r in sorted(rows, key=lambda r: r.created_at):
        if (r.run_id in entries and r.quantity and r.exchange_segment and r.symbol
                and not ((r.product or "").upper() in INTRADAY_PRODUCTS and _aware(r.created_at) < day_start)):
            out.setdefault(_row_key(r), r)
    return out


def _fills_today(user_id: int, account_id: int) -> dict[Lot, tuple[int, int]]:
    """Units TradeLab's own orders of today bought and sold in an account, by position key."""
    with session_scope() as db:
        rows = db.find(OrderLog, {"user_id": user_id, "account_id": account_id, "action": {"$in": ["buy", "sell"]},
                                  "status": "success", "created_at": {"$gte": _order_book_day_start()}},
                       track=False)
    out: dict[Lot, tuple[int, int]] = {}
    for r in rows:
        if r.quantity:
            done = r.quantity if r.final or r.filled is None else min(r.quantity, r.filled)
            b, s = out.get(_row_key(r), (0, 0))
            out[_row_key(r)] = (b + done, s) if r.action == "buy" else (b, s + done)
    return out


def _settling_since(today: date) -> date:
    """The first trading day whose delivery buys may still be settling today: the previous trading day, and
    every earlier one whose settlement day (the next weekday that is no trading or clearing holiday) is not
    over yet, e.g. Monday's buys on Wednesday when Tuesday is a clearing holiday. If the clearing holidays
    of a year involved were never downloaded, the last two trading days. Never fewer than the last two trading
    days either way: a wrong or partial clearing list may only widen the window."""
    days: list[date] = []
    day = today
    for _ in range(14):
        day -= timedelta(days=1)
        if is_trading_day(day):
            days.append(day)
            if len(days) == 4:
                break
    if not days:
        return today - timedelta(days=1)
    clearing = [calendar.clearing_holidays(y) for y in range(days[-1].year, today.year + 1)]
    fallback = days[min(1, len(days) - 1)]
    if any(c is None for c in clearing):
        return fallback
    closed = set().union(*clearing)
    since = days[0]
    for trade in days[1:]:
        settled = trade + timedelta(days=1)
        while settled in closed or not is_trading_day(settled):
            settled += timedelta(days=1)
        if settled < today:
            break
        since = trade
    return min(since, fallback)


def _previous_day_delivery_buys(user_id: int, section: str, account_id: int) -> dict[str, int]:
    """Delivery units TradeLab bought on the previous trading day (and earlier ones still settling),
    by trading symbol."""
    since = datetime.combine(_settling_since(datetime.now(IST).date()), dtime(0, 0),  # the records' clock
                             tzinfo=IST).astimezone(timezone.utc)
    with session_scope() as db:
        rows = db.find(OrderLog, {"user_id": user_id, "section": section, "account_id": account_id,
                                  "action": "buy", "product": "CNC", "status": "success",
                                  "created_at": {"$gte": since, "$lt": _order_book_day_start()}}, track=False)
    out: dict[str, int] = {}
    for r in rows:
        if r.symbol and r.quantity:
            out[r.symbol] = out.get(r.symbol, 0) + (r.quantity if r.final or r.filled is None
                                                    else min(r.quantity, r.filled))
    return out


def _note_holders_own(key: Lot, units: int) -> None:
    """Remember how many units of a delivery holding are the holder's own (beyond TradeLab's records)."""
    now, start = utcnow(), _order_book_day_start()
    col = database()["holder_units"]
    doc = col.find_one({"_id": f"{key.account_id}:{key.trading_symbol}"}) or {}
    upd = {"own": units, "seen_at": now}
    if doc.get("seen_at") is not None and _aware(doc["seen_at"]) < start:
        upd |= {"own_before": doc.get("own", 0), "before_at": doc["seen_at"]}
    col.update_one({"_id": f"{key.account_id}:{key.trading_symbol}"}, {"$set": upd}, upsert=True)


def _note_holders_sale(key: Lot, view: BrokerView, tradelab_sold: int) -> None:
    """The holder sold some of her own units today (her own go first): take them off the units noted as hers, so
    TradeLab's units arriving later aren't taken for hers."""
    own = _holders_own_noted(key)
    her = min(own or 0, max(0, view.day_sold.get(key, 0) - tradelab_sold))
    if her:
        _note_holders_own(key, (own or 0) - her)


def _holders_own_noted(key: Lot) -> int | None:
    """The holder's own units as last seen before today, or None when TradeLab never noted them."""
    doc = database()["holder_units"].find_one({"_id": f"{key.account_id}:{key.trading_symbol}"})
    if not doc or doc.get("seen_at") is None:
        return None
    if _aware(doc["seen_at"]) < _order_book_day_start():
        return int(doc.get("own", 0))
    return int(doc.get("own_before", 0)) if doc.get("before_at") is not None else None


def _holders_own_before_today(key: Lot) -> int:
    return _holders_own_noted(key) or 0


def _delivery_sizes(user_id: int, section: str, acc_id: int, ledger: dict[Lot, int], keys: list[Lot],
                    view: BrokerView) -> tuple[dict[Lot, int], dict[Lot, int], dict[Lot, int]]:
    """How many delivery units of each key TradeLab may sell, by key, as (sizes, hidden, unknown).

    Kotak's figure pools every owner's units. While some of TradeLab's units of the symbol may still be settling
    (bought on a recent trading day) or are not sellable (pledged), the units shown could be the holder's own, so
    her own (as last seen before today) come off first. `hidden` = those units; `unknown` = Kotak's figure for a
    key whose own units were never noted: nothing can be sold there.
    """
    sizes = {k: view.held.get(k, 0) for k in keys}
    hidden: dict[Lot, int] = {}
    unknown: dict[Lot, int] = {}
    earlier: dict[str, int] | None = None
    for k in keys:
        if k.product != "CNC" or ledger.get(k, 0) <= 0 or sizes[k] <= 0:
            continue
        if earlier is None:
            earlier = _previous_day_delivery_buys(user_id, section, acc_id)
        if not (earlier.get(k.trading_symbol) or view.total.get(k, 0) > sizes[k]):
            continue
        own = _holders_own_noted(k)
        if own is None:
            unknown[k], sizes[k] = sizes[k], 0
        else:
            hidden[k], sizes[k] = own, max(sizes[k] - own, 0)
    return sizes, hidden, unknown


def _written_off_today(user_id: int, section: str, account_id: int) -> dict[Lot, int]:
    """Units of a section's records in an account written off today ("Closed outside TradeLab"), by key."""
    with session_scope() as db:
        rows = db.find(OrderLog, {"user_id": user_id, "section": section, "account_id": account_id,
                                  "status": ADJUSTED, "created_at": {"$gte": _order_book_day_start()}}, track=False)
    out: dict[Lot, int] = {}
    for r in rows:
        if r.quantity:
            out[_row_key(r)] = out.get(_row_key(r), 0) + r.quantity
    return out


@dataclass
class Unseen:
    """Units of TradeLab's records Kotak doesn't show yet, so they were kept (not written off)."""

    lagging: int = 0  # bought (or sold first) today, not in today's positions yet
    settling: int = 0  # delivery units of the last trading days, not in the demat yet
    unsure: str = ""  # why Kotak's figure can't be compared with the records (they were kept, nothing is sent)


def _shrink_to_broker(user_id: int, section: str, acc: _Acc, ledger: dict[Lot, int], view: BrokerView,
                      unfinished: set[Lot] | frozenset = frozenset(), write: bool = True) -> dict[Lot, Unseen]:
    """When Kotak shows less of a position than TradeLab's records say TradeLab opened, the rest was closed
    outside TradeLab: bring the records down to Kotak's figure, so they never count units that are gone
    (which could later be "closed" against the account holder's own position, or hide a new one).

    Never below what Kotak may not show yet: TradeLab's own trades of today that today's positions don't
    show (Kotak's positions can lag its order book), and delivery units bought on the previous trading day
    that are still settling (except as many as were sold today outside TradeLab). Contracts: only with
    evidence that the units are really gone (today's positions list the contract, other positions came back
    so the list isn't just empty, or the contract has expired), since forgetting a contract that is still
    open would leave it unmanaged. Never while a TradeLab order there hasn't finished (`unfinished`).
    `write=False` (a preview) changes only `ledger`, never the records.

    Returns, by key, the units kept because Kotak doesn't show them yet.

    Between midnight and 09:00 nothing is written: until its start of day Kotak may still show yesterday's
    trades while TradeLab's day has already turned, and nothing can be traded then anyway.
    """
    write = write and not (dtime(0, 0) <= now_ist().time() < NIGHT_ENDS)
    fills: dict[Lot, tuple[int, int]] | None = None
    earlier: dict[str, int] | None = None
    written: dict[Lot, int] = {}
    kept: dict[Lot, Unseen] = {}
    for key, ours in list(ledger.items()):
        if key.account_id != acc.id or ours == 0 or key not in view.total or key in unfinished:
            continue
        if _contract_changed(key):
            continue  # an adjusted contract: Kotak's figure isn't comparable with the records (reported by Exit)
        side = 1 if ours > 0 else -1
        broker = view.total[key]
        shown = max(side * broker, 0)  # what Kotak shows on TradeLab's side
        if shown >= abs(ours):
            if key.product == "CNC" and write:
                if side > 0 and earlier is None:
                    earlier = _previous_day_delivery_buys(user_id, section, acc.id)
                    written = _written_off_today(user_id, section, acc.id)
                # While some of TradeLab's units of the symbol may still be settling, `shown - ours` is not the
                # holder's figure (her units may be covering the missing ones): keep the last one seen.
                if side < 0 or not earlier.get(key.trading_symbol):
                    _note_holders_own(key, shown - abs(ours))
                else:
                    if fills is None:
                        fills = _fills_today(user_id, acc.id)
                    _note_holders_sale(key, view, fills.get(key, (0, 0))[1])
            continue
        doubt = _units_doubt(key, ours, broker, shown, view)
        if doubt:
            kept[key] = Unseen(unsure=doubt)  # (reported by Exit; nothing is written)
            continue
        if fills is None:
            fills = _fills_today(user_id, acc.id)
        bought, sold = fills.get(key, (0, 0))
        mine_today = bought if side > 0 else sold
        theirs_today = view.day_bought.get(key, 0) if side > 0 else view.day_sold.get(key, 0)
        lagging = max(0, mine_today - theirs_today)
        cap = shown + lagging
        if key.product == "CNC" and side > 0:
            if earlier is None:
                earlier = _previous_day_delivery_buys(user_id, section, acc.id)
                written = _written_off_today(user_id, section, acc.id)
            prev = earlier.get(key.trading_symbol, 0)
            if prev:
                # Units bought on the previous trading day may not show yet (settling). Meanwhile only what was
                # sold outside TradeLab today is written off, and never more than those units can explain. It is
                # worked out from the records as they stood before today's write-offs, so looking again changes
                # nothing.
                # Sold today outside TradeLab: the holder's own units (as last seen before today) go first.
                outside = max(0, view.day_sold.get(key, 0) - sold - _holders_own_before_today(key))
                if write:
                    _note_holders_sale(key, view, sold)
                before_today = abs(ours) + written.get(key, 0)
                cap = min(cap + prev, max(cap, before_today - outside))
        keep = min(abs(ours), cap)
        if keep > shown:
            lag = min(lagging, keep - shown)
            kept[key] = Unseen(lagging=lag, settling=keep - shown - lag)
        if keep >= abs(ours):
            continue
        if key.product != "CNC" and not (key in view.listed or view.rows > 0 or _expired_contract(key)):
            continue
        gone = abs(ours) - keep
        if write:
            # Another writer (a true-up, a Buy's plan, an Exit or the panic button's close) may have changed this
            # key since the records and unfinished orders were read: decide again from them, inside the lock.
            with _writedown_lock(acc.id):
                now = tradelab_open(user_id, section, [acc.id], {key.trading_symbol}, {key.product}).get(key, 0)
                if now != ours or key in unfinished_keys(user_id, [acc.id]):
                    ledger[key] = now
                    continue
                note = ("so TradeLab's records now match" if keep == shown else
                        f"and {keep - shown} more may not show there yet, so TradeLab's records now count {side * keep}")
                with session_scope() as db:
                    db.add(OrderLog(user_id=user_id, run_id=None, account_id=acc.id, account_name=acc.name,
                                    action="sell" if ours > 0 else "buy", symbol=key.trading_symbol, quantity=gone,
                                    status=ADJUSTED, exchange_segment=key.exchange_segment, product=key.product,
                                    section=section, final=True,
                                    message=f"Closed outside TradeLab: Kotak shows {broker} of the {ours} TradeLab "
                                            f"opened, {note}"))
            log.warning("Account %s %s: Kotak shows %s of TradeLab's %s; records adjusted",
                        acc.id, key.trading_symbol, broker, ours)
        ledger[key] = side * keep
    return kept


def _writedown_lock(account_id: int) -> threading.Lock:
    with _locks_guard:
        return _writedown_locks.setdefault(account_id, threading.Lock())


_writedown_locks: dict[int, threading.Lock] = {}


def true_up_all() -> None:
    """Bring every user's records of what Holding, F&O and Commodity hold down to what Kotak shows
    (run daily after the close, so positions closed outside TradeLab are noticed even when nobody
    opens the app)."""
    from .sections import SELLABLE

    with session_scope() as db:
        users = db.distinct(OrderLog, "user_id", {"section": {"$in": list(SELLABLE)}})
    for user_id in users:
        try:
            reconcile_orders(user_id)
            unfinished = unfinished_keys(user_id)
            accounts = {a.id: a for a in _all_accounts(user_id)}
            for section in SELLABLE:
                ledger = tradelab_open(user_id, section)
                for acc_id in sorted({k.account_id for k in ledger}):
                    acc = accounts.get(acc_id)
                    if acc is None:
                        continue
                    try:
                        view = broker_view(acc_id, [k for k in ledger if k.account_id == acc_id])
                    except Exception as exc:  # noqa: BLE001
                        log.info("True-up of account %s skipped: %s", acc_id, exc)
                        continue
                    _shrink_to_broker(user_id, section, acc, ledger, view, unfinished)
        except Exception:  # noqa: BLE001
            log.exception("True-up of user %s failed", user_id)


def broker_held(acc_id: int, wanted: list[Lot]) -> dict[Lot, int]:
    """What the broker shows for these keys right now, signed (negative = a short position).

    Delivery (CNC) units: sellable demat holdings plus units bought today (still in positions).
    Anything else: the open position's net quantity.
    """
    out: dict[Lot, int] = {}
    positions = broker_manager.call(acc_id, lambda b: b.positions())
    net: dict[tuple[str, str, str], int] = {}
    day: dict[tuple[str, str, str], list[int]] = {}
    for p in positions:
        k = (p.exchange_segment, p.trading_symbol, (p.product or "").upper())
        net[k] = net.get(k, 0) + p.net_qty
        d = day.setdefault(k, [0, 0])
        d[0] += max(p.day_buy_qty, 0)
        d[1] += max(p.day_sell_qty, 0)
    holdings: dict[str, int] = {}
    if any(w.product == "CNC" for w in wanted):
        for h in broker_manager.call(acc_id, lambda b: b.holdings()):
            holdings[h.trading_symbol] = holdings.get(h.trading_symbol, 0) + h.sellable_qty
    for w in wanted:
        n = net.get((w.exchange_segment, w.trading_symbol, w.product), 0)
        out[w] = max(n, 0) + holdings.get(w.trading_symbol, 0) if w.product == "CNC" else n
    return out


def _item_info(user_id: int, section: str) -> dict[str, tuple[str, int]]:
    """Display name and lot size of the section's items, by trading symbol."""
    with session_scope() as db:
        # Oldest first, so a symbol listed twice (in two segments) always keeps the newest item's details.
        return {it.trading_symbol: (it.symbol, it.lot_size or 1) for it in db.find(
            BasketItem, {"user_id": user_id, "section": section}, sort=[("id", 1)])}


def build_sell_plan(user_id: int, section: str, symbols: set[str] | None = None,
                    account_ids: list[int] | None = None, products: set[str] | None = None,
                    opened: str | None = None, preview: bool = False, panic: bool = False) -> list[PlannedOrder]:
    """Orders that close what `section` opened (all of it, or only `symbols`): sell what it
    bought, buy back what it sold first (written options). `opened` = "B" closes only what it
    bought, "S" only what it sold first.

    Each order is the smaller of what TradeLab opened and what the broker shows, so a family
    member's own shares or contracts are never touched.
    """
    sp = spec(section)
    chosen = section_accounts(user_id, section) if account_ids is None else account_ids
    reconcile_orders(user_id)  # late rejections, part fills and lost replies first
    # (The screen shows what a close still working hasn't closed; a run never closes it twice: it cancels
    # the close first, and counts it in full until Kotak's book shows it stopped.)
    # Only the accounts asked for: one account's "Square off" never closes another account's positions.
    ledger = tradelab_open(user_id, section, account_ids, symbols, products, closes_by_fill=preview)
    unfinished = unfinished_keys(user_id)
    unconfirmed = unconfirmed_orders(user_id)
    if opened:
        ledger = {k: q for k, q in ledger.items() if (q > 0) == (opened == "B")}
    stays_open: list[Lot] = []
    if sp.lots and (symbols or opened):
        # Sold contracts the filter leaves open: a bought contract of the same underlying isn't sold beside them.
        whole = tradelab_open(user_id, section, account_ids, None, products, closes_proved=True)
        stays_open = [k for k, q in whole.items() if q < 0 and k not in ledger]
    info = _item_info(user_id, section)
    by_acc: dict[int, list[Lot]] = {}
    # A TradeLab close of today with no known outcome: the position may be open or closed, so the key is
    # reported (never "nothing left", never closed again) until the book shows it or proves it missing.
    unresolved = {k: r for k, r in unresolved_closes(user_id, section, account_ids).items()
                  if (symbols is None or k.trading_symbol in symbols) and k.product in (products or set(sp.products))
                  and (not opened or (r.action == "sell") == (opened == "B"))}
    # A TradeLab Buy with no known outcome is in no record, yet the position may exist: the same, for its key.
    unknown_buys = {k: r for k, r in unresolved_entries(user_id, section, account_ids).items()
                    if (symbols is None or k.trading_symbol in symbols) and k.product in (products or set(sp.products))
                    and (not opened or (r.action == "buy") == (opened == "B"))}
    for key in ledger.keys() | unresolved.keys() | unknown_buys.keys():
        by_acc.setdefault(key.account_id, []).append(key)
    accounts = {a.id: a for a in _all_accounts(user_id)}
    plan: list[PlannedOrder] = []

    def order(acc: _Acc, key: Lot, qty: int, skip: str | None = None, note: str | None = None,
              error: bool = False, held: int | None = None, side: str | None = None) -> PlannedOrder:
        name, lot = info.get(key.trading_symbol, (key.trading_symbol, None))
        if sp.lots and not lot:
            c = _contract(key.exchange_segment, key.trading_symbol)
            lot = c.lot_size if c else None
            name = c.display if c and name == key.trading_symbol else name
        side = side or ("S" if ledger[key] > 0 else "B")  # sell what was bought; buy back what was sold first
        return PlannedOrder(account_id=acc.id, account_name=acc.name, symbol=name, trading_symbol=key.trading_symbol,
                            exchange_segment=key.exchange_segment, side=side, quantity=qty, skip=skip, note=note,
                            error=error, product=key.product, lots=qty // lot if sp.lots and lot else None,
                            held=held)

    def prepare(acc_id: int) -> list[PlannedOrder]:
        acc = accounts.get(acc_id)
        keys = sorted(by_acc[acc_id], key=lambda k: k.trading_symbol)
        if acc is None:
            return []
        out = []
        for k in [k for k in keys if k in unresolved]:
            r = unresolved[k]
            sent = _aware(r.sent_at or r.created_at).astimezone(IST).strftime("%H:%M")
            out.append(order(acc, k, abs(ledger.get(k, 0)) or r.quantity, error=True, side="S" if r.action == "sell" else "B",
                             skip=(f"An earlier close of this (sent at {sent}) got no reply from Kotak and its order "
                                   "book doesn't show it, so TradeLab can't tell whether it went through. Nothing "
                                   "was sent; check the Kotak Neo app.")))
        keys = [k for k in keys if k not in unresolved]
        mystery = [k for k in keys if k in unknown_buys]
        if mystery:
            # (An earlier day's row only while Kotak still lists the key: otherwise there is nothing it could be.)
            old = [k for k in mystery if _aware(unknown_buys[k].created_at) < _order_book_day_start()]
            try:
                gone = {k for k in old if k not in broker_view(acc_id, old).listed} if old else set()
            except Exception:  # noqa: BLE001
                gone = set()
            keys = [k for k in keys if k not in unknown_buys or k in gone]
            for k in [k for k in mystery if k not in gone]:
                r = unknown_buys[k]
                sent = _aware(r.sent_at or r.created_at).astimezone(IST).strftime("%d %b %H:%M")
                out.append(order(acc, k, abs(ledger.get(k, 0)) or r.quantity, error=True,
                                 side="S" if r.action == "buy" else "B",
                                 skip=(f"An earlier {'buy' if r.action == 'buy' else 'sell'} of this (sent {sent}) got no "
                                       "reply from Kotak and its order book doesn't show it, so TradeLab can't tell "
                                       "whether it went through, and nothing is sent. Check the Kotak Neo app.")))
        if not keys:
            return out
        # Exit closes what TradeLab opened in every account, also one whose autopilot was switched off or
        # that was taken out of the section since (that only stops new buys): nothing it opened is stranded.
        later = "" if acc_id in chosen else f" (this account no longer trades in {sp.label})"
        try:
            broker_manager.ensure_session(acc_id)
            view = broker_view(acc_id, keys)
            have = view.held
            before = {k: ledger[k] for k in keys}
            kept = _shrink_to_broker(user_id, section, acc, ledger, view, unfinished, write=not preview)
            size, hidden, unknown = _delivery_sizes(user_id, section, acc_id, ledger, keys, view)
        except Exception as exc:  # noqa: BLE001
            return out + [order(acc, k, abs(ledger[k]), skip=f"Could not read this account's positions: {exc}", error=True)
                    for k in keys]
        for k in keys:
            if k in unconfirmed:
                mine, broker, sign = ledger[k], size.get(k, 0), (1 if ledger[k] > 0 else -1)
                shown = min(abs(mine), max(sign * broker, 0))
                if preview and shown > 0:
                    # The run checks Kotak's order book again first; this is only what the screen shows.
                    out.append(order(acc, k, shown, note=(
                        "Kotak's order book hasn't confirmed TradeLab's last order here yet: the quantity is "
                        "checked again when you confirm")))
                    continue
                skip = ("Kotak's order book hasn't confirmed TradeLab's last order for this yet, so nothing is sent "
                        "for it. Try again in a moment, or check the Kotak Neo app.")
                # What the book has confirmed TradeLab holds there, when the unconfirmed orders add to it.
                confirmed = sign * (mine - unconfirmed[k]) if sign * unconfirmed[k] > 0 else 0
                part = min(confirmed, max(sign * broker, 0)) if confirmed > 0 else 0
                if panic and part > 0:
                    # The panic button closes what is confirmed TradeLab's; the unconfirmed order is reported,
                    # never closed by what Kotak shows (that could be the holder's own units).
                    out.append(order(acc, k, part, note="Only what Kotak's order book has confirmed", held=broker))
                    out.append(order(acc, k, 0, error=True, skip=skip))
                    continue
                out.append(order(acc, k, abs(mine), error=True, skip=skip))
                continue
            changed = _contract_changed(k) if ledger[k] else None
            if changed:
                out.append(order(acc, k, abs(ledger[k]), error=True, skip=changed))
                continue
            if kept.get(k) and kept[k].unsure and ledger[k]:
                out.append(order(acc, k, abs(ledger[k]), error=True, skip=kept[k].unsure))
                continue
            if ledger[k] == 0:
                ledger[k] = before[k]  # (for the side of the row; nothing is sent)
                if not _expired_contract(k):
                    out.append(order(acc, k, 0, skip="Nothing left to close (already sold, bought back or expired)"))
                ledger[k] = 0
                continue
            mine, broker = ledger[k], have.get(k, 0)
            if k in unknown:
                out.append(order(acc, k, mine, error=True, skip=(
                    f"Can't tell which of the {unknown[k]} units Kotak shows are TradeLab's while its units of this "
                    "settle (the account holder owns some of them too), so nothing is sent. TradeLab sells once its "
                    "units arrive; check the Kotak Neo app.")))
                continue
            qty = min(mine, max(size[k], 0)) if mine > 0 else min(-mine, max(-broker, 0))
            lot = _lot_of(k)
            if qty > 0 and lot and qty % lot:
                out.append(order(acc, k, qty, error=True, skip=(
                    f"Not closed: {qty} units is not a whole number of lots ({lot} units to a lot), which Kotak "
                    "would refuse, so nothing was sent. Check the Kotak Neo app and close it there.")))
                continue
            rows = []
            if qty > 0 and mine > 0:
                extra = f" of {mine} bought by TradeLab" if qty < mine else ""
                rows.append(order(acc, k, qty, note=f"Sell {qty}{extra}{later}", held=broker))
            elif qty > 0:
                extra = f" of {-mine} sold first by TradeLab" if qty < -mine else ""
                rows.append(order(acc, k, qty, note=f"Buy back {qty}{extra}{later}", held=broker))
            if k.product == "CNC" and 0 <= qty < mine:
                # shown at Kotak, but not free to sell (the holder's own units are not TradeLab's)
                locked = min(mine, max(view.total.get(k, 0) - hidden.get(k, 0), 0)) - qty
                if locked > 0:
                    rows.append(order(acc, k, locked, error=True, skip=(
                        f"{locked} of TradeLab's units here show at Kotak but can't be sold yet (not settled, or "
                        "pledged), so they weren't sold. Sell them once Kotak shows them as free to sell.")))
            unseen = kept.get(k)
            if unseen and unseen.lagging:
                rows.append(order(acc, k, unseen.lagging, error=True, skip=(
                    f"Kotak's positions don't show {unseen.lagging} of TradeLab's units here yet (its order filled "
                    "moments ago), so nothing was sent for them. Try again in a moment, or check the Kotak Neo app.")))
            waiting = unseen.settling if unseen else 0
            if k in hidden:  # (Kotak's figure counted the holder's units as shown)
                waiting = max(0, mine - qty - max(locked if 0 <= qty < mine else 0, 0)
                              - (unseen.lagging if unseen else 0))
            if waiting:
                rows.append(order(acc, k, waiting, error=True, skip=(
                    f"{waiting} units TradeLab bought on a recent trading day don't show at Kotak yet "
                    "(still settling), so they weren't sold. Sell them once Kotak shows them.")))
            out.extend(rows or [order(acc, k, 0, skip="Nothing left to close (already sold, bought back or expired)")])
        return out

    if by_acc:
        with _pool(len(by_acc)) as ex:
            for rows in ex.map(prepare, sorted(by_acc)):
                plan.extend(_hold_hedges(rows, stays_open))
    return plan


def _hold_hedges(rows: list[PlannedOrder], stays_open: list[Lot]) -> list[PlannedOrder]:
    """Sales of bought derivative contracts beside a sold contract of the same underlying that stays open (left
    out of the plan by a filter) are held: selling the hedge alone would leave that position naked."""
    return [replace(p, skip=("Not sold: the sold leg it hedges stays open, so selling this one now could leave that "
                             "position without its hedge. Close both together, or check the Kotak Neo app."),
                    error=True, note=None)
            if p.side == "S" and p.exchange_segment in DERIVATIVE_SEGMENTS and not p.skip and p.quantity > 0
            and any(k.account_id == p.account_id and _same_underlying(p, k) for k in stays_open) else p
            for p in rows]


def _all_accounts(user_id: int) -> list[_Acc]:
    with session_scope() as db:
        return [_Acc(a.id, a.name, a.broker) for a in db.find(BrokerAccount, {"user_id": user_id}, sort=[("id", 1)])]


def section_positions(user_id: int, section: str) -> dict:
    """What `section` opened and still has open, per account, with the latest prices."""
    sp = spec(section)
    try:
        reconcile_orders(user_id)
    except Exception:  # noqa: BLE001 - shown from the records as they are
        log.exception("Settling orders before showing positions failed")
    book = tradelab_book(user_id, section, closes_by_fill=True)
    info = _item_info(user_id, section)
    accounts = {a.id: a for a in _all_accounts(user_id)}
    by_acc: dict[int, list[Lot]] = {}
    for key in book:
        by_acc.setdefault(key.account_id, []).append(key)

    def read(acc_id: int) -> tuple[int, dict, str | None]:
        try:
            positions = broker_manager.call(acc_id, lambda b: b.positions())
            holdings = broker_manager.call(acc_id, lambda b: b.holdings()) if sp.products[0] == "CNC" else []
            return acc_id, {"positions": positions, "holdings": holdings}, None
        except Exception as exc:  # noqa: BLE001
            return acc_id, {}, str(exc)

    rows = []
    settling = []
    errors = []
    if by_acc:
        with _pool(len(by_acc)) as ex:
            results = list(ex.map(read, sorted(by_acc)))
    else:
        results = []
    today = now_ist().date()
    for acc_id, data, err in results:
        acc = accounts.get(acc_id)
        if acc is None:
            continue
        if err:
            errors.append({"account_id": acc_id, "account_name": acc.name, "error": err})
            continue
        for key in sorted(by_acc[acc_id], key=lambda k: k.trading_symbol):
            held = book[key]
            short = held.qty < 0
            pos = [p for p in data["positions"] if p.trading_symbol == key.trading_symbol
                   and p.exchange_segment == key.exchange_segment and (p.product or "").upper() == key.product]
            hold = [h for h in data["holdings"] if h.trading_symbol == key.trading_symbol]
            if short:
                have = sum(max(-p.net_qty, 0) for p in pos)
            else:
                have = sum(max(p.net_qty, 0) for p in pos) + sum(h.quantity for h in hold)
            qty = min(abs(held.qty), have)
            if key.product == "CNC" and not short and held.qty > have:
                # Delivery units Kotak doesn't show yet (still settling): listed apart, never valued or sold.
                settling.append({"account_id": acc_id, "account_name": acc.name,
                                 "symbol": info.get(key.trading_symbol, (key.trading_symbol, None))[0],
                                 "trading_symbol": key.trading_symbol, "quantity": held.qty - have})
            if qty <= 0:
                continue
            # TradeLab's own fill prices; the broker's average only if everything there is TradeLab's
            # (a family member's own units of the same stock would blend into it).
            avg = held.cost
            if avg is None and have == abs(held.qty):
                avg = (next((p.sell_avg for p in pos if p.sell_avg), None) if short else
                       next((h.avg_price for h in hold if h.avg_price), None) or
                       next((p.buy_avg for p in pos if p.buy_avg), None))
            ltp = next((p.ltp for p in pos if p.ltp), None) or next((h.ltp for h in hold if h.ltp), None)
            mult = next((p.multiplier for p in pos if p.multiplier), 1.0) or 1.0
            note = None
            split = _looks_like_split(held, have, next((h.avg_price for h in hold if h.avg_price), None))
            if split:
                # A bonus or split multiplied the units and divided the price; TradeLab's records still count
                # the units it bought at the old price, so its P/L would show a fake loss.
                avg = None
                note = {"level": "warn", "text": f"Kotak shows {split}× the units TradeLab bought, at about 1/{split} "
                        "of the price — this looks like a bonus or split. TradeLab still counts only the units it "
                        f"bought ({abs(held.qty)}), so Sell sells only those; sell the rest in the Kotak app."}
            name, lot = info.get(key.trading_symbol, (key.trading_symbol, None))
            contract = _contract(key.exchange_segment, key.trading_symbol) if sp.lots else None
            if contract is not None:
                lot = lot or contract.lot_size
                if name == key.trading_symbol:
                    name = contract.display  # the item was removed from the list since
            token = next((p.token for p in pos if p.token), None) or next((h.token for h in hold if h.token), None)
            rows.append({
                "account_id": acc_id, "account_name": acc.name, "symbol": name, "trading_symbol": key.trading_symbol,
                "exchange_segment": key.exchange_segment, "product": key.product, "quantity": qty,
                "side": "S" if short else "B",
                "lots": qty // lot if sp.lots and lot else None, "avg_price": avg, "ltp": ltp,
                "multiplier": mult, "token": token,
                # Contracts are settled by the exchange on this day if they haven't been closed.
                "expiry": contract.expiry.isoformat() if contract and contract.expiry else None,
                "expiry_note": note or expiry_note(contract, today, short),
                # A future's full contract value isn't money the account has: it is left out of "value".
                "future": bool(sp.lots and not (contract.is_option if contract else key.trading_symbol[-2:] in ("CE", "PE"))),
            })
    _fill_prices(user_id, rows)
    for r in rows:
        sign = -1 if r["side"] == "S" else 1
        # A short position's "value" is what it would cost to buy back, so it isn't added to the total.
        r["value"] = (round(r["quantity"] * r["ltp"] * r["multiplier"], 2)
                      if r["ltp"] and sign > 0 and not r.pop("future") else None)
        r.pop("future", None)
        r["pnl"] = (round(sign * r["quantity"] * (r["ltp"] - r["avg_price"]) * r["multiplier"], 2)
                    if r["ltp"] and r["avg_price"] else None)
        r.pop("token", None)
    unclear = []
    for k, r in unresolved_closes(user_id, section).items():
        acc = accounts.get(k.account_id)
        sent = _aware(r.sent_at or r.created_at).astimezone(IST).strftime("%H:%M")
        unclear.append({"account_id": k.account_id, "account_name": acc.name if acc else "", "symbol":
                        info.get(k.trading_symbol, (k.trading_symbol, None))[0], "trading_symbol": k.trading_symbol,
                        "quantity": r.quantity, "note": (f"a close sent at {sent} got no reply from Kotak and its "
                                                         "order book doesn't show it, so this may still be open")})
    return {"section": section, "rows": rows, "settling": settling, "unclear": unclear, "errors": errors,
            "value": round(sum(r["value"] or 0 for r in rows), 2),
            "pnl": round(sum(r["pnl"] or 0 for r in rows), 2)}


def _looks_like_split(held: Held, have: int, broker_avg: float | None) -> int | None:
    """The bonus / split ratio when Kotak shows a whole multiple of TradeLab's units at about that fraction
    of TradeLab's cost (None otherwise)."""
    if held.qty <= 0 or not held.cost or not broker_avg or have <= held.qty or have % held.qty:
        return None
    k = have // held.qty
    return k if abs(broker_avg * k - held.cost) <= 0.05 * held.cost else None


# MCX futures settled in cash; the other commodity futures (gold, silver, base metals) go into a
# delivery ("tender") period before expiry, when Kotak closes long positions.
CASH_SETTLED_MCX = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "ELECDMBL"}


def _trading_days_until(expiry: date, today: date) -> int:
    """NSE trading days after today up to and including expiry (0 on expiry day)."""
    nse = spec("fno")
    days, d = 0, today
    while d < expiry and days < 30:
        d += timedelta(days=1)
        if is_section_day(d, nse):
            days += 1
    return days


def delivery_future(contract) -> bool:
    """An MCX future settled by delivery (gold, silver, metals), not in cash (crude oil, natural gas, indices)."""
    return (contract.exchange_segment == "mcx_fo" and contract.instrument_type == "FUTCOM"
            and contract.symbol not in CASH_SETTLED_MCX)


def expiry_note(contract, today: date, short: bool = False) -> dict | None:
    """What will happen to an open contract near its expiry, by exchange or Kotak rule (not TradeLab's).

    `short` = TradeLab sold it first. Kotak's rules (its help pages, 2026): trading in an MCX
    delivery future stops when its delivery (tender) period starts; bought positions are closed
    after 5 pm that day, sold ones after 5 pm on the second-last day of the period. Bought MCX
    options are closed on expiry evening; sold ones are left alone and, if in the money, turn
    into futures. Stock F&O held to expiry is settled by delivery of shares.
    """
    if contract is None or contract.expiry is None:
        return None
    days = (contract.expiry - today).days
    when = f"{contract.expiry.day} {contract.expiry.strftime('%b')}"
    close = "buy it back" if short else "sell it"
    if contract.exchange_segment == "mcx_fo":
        # MCX's delivery period is the last 3 trading days (Kotak's page still says 5), so warn well ahead.
        if delivery_future(contract) and days <= 10:
            if short:
                return {"level": "warn", "text": f"Kotak stops trading in {contract.symbol} futures when their delivery "
                        f"(tender) period starts, 3–5 trading days before expiry on {when}, and closes sold positions "
                        "after 5 PM on the trading day before expiry. Buy it back before the delivery period starts."}
            return {"level": "warn", "text": f"Kotak closes {contract.symbol} futures in the delivery (tender) period, "
                    f"3–5 trading days before expiry on {when}. Close it before then."}
        if contract.is_option and short and days <= 3:
            return {"level": "warn", "text": f"Expires on {when}. Kotak doesn't close sold MCX options on expiry day: if "
                    "it ends in the money it turns into a futures position (a sold call into a sold future, a sold put "
                    "into a bought future) that needs full futures margin. TradeLab doesn't track that future — Exit "
                    "and Square off won't close it. Buy the option back before expiry to avoid this."}
        if contract.is_option and days <= 1:
            return {"level": "warn", "text": "Kotak closes long MCX options on the evening of expiry day "
                    f"({when}, after about 10 PM), and may choose not to exercise one that is in the money. "
                    "Close it before then."}
    elif contract.instrument_type in ("FUTSTK", "OPTSTK") and _trading_days_until(contract.expiry, today) <= 4:
        if short:
            margin = ("Kotak blocks extra margin from 4 trading days before expiry and may close the position if "
                      "money falls short. Buy it back before expiry to avoid this.")
            if contract.option_type == "PE":
                return {"level": "warn", "text": f"A sold stock put that ends in the money at expiry ({when}) must take "
                        f"the shares and pay their full value (strike × lot size). {margin}"}
            return {"level": "warn", "text": f"Held to expiry ({when}), a sold stock future — or a sold call that ends "
                    "in the money — is settled by delivering the shares from the account's demat, including the "
                    "holder's own shares. Shares that aren't there go to the exchange's auction, which can cost 20% "
                    f"or more. {margin}"}
        return {"level": "warn", "text": f"Stock futures and options held to expiry ({when}) are settled by delivery "
                "of shares. Kotak blocks extra margin from 4 trading days before expiry and may close the position "
                "if money falls short."}
    if days <= 0:
        return {"level": "warn", "text": f"Expires today: if you don't {close}, the exchange settles it after the close."}
    if days <= 2:
        return {"level": "info", "text": f"Expires on {when}: if you don't {close}, the exchange settles it that day."}
    return None


def _contract(segment: str, trading_symbol: str):
    """Today's contract details, or None (unknown, expired, or the list can't be loaded)."""
    from . import contracts

    try:
        return contracts.contract_master.get(segment, trading_symbol)
    except Exception:  # noqa: BLE001 - details are only for display here
        return None


def _fill_prices(user_id: int, rows: list[dict]) -> None:
    """Latest prices for the rows (holdings show yesterday's close). Asked per exchange segment, since the
    same token number means different contracts on different segments."""
    wanted: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        tok = r.get("token") or scrip_master.token_for(r["trading_symbol"])
        if tok:
            wanted.setdefault((r["exchange_segment"], str(tok)), []).append(r)
    by_seg: dict[str, list[str]] = {}
    for seg, tok in wanted:
        by_seg.setdefault(seg, []).append(tok)
    for seg, tokens in by_seg.items():
        missing = set(tokens)
        for client in broker_manager.market_data_clients(user_id):
            if not missing:
                break
            try:
                prices = client.ltp([(seg, t) for t in sorted(missing)])
            except Exception as exc:  # noqa: BLE001
                log.info("No live prices for %s: %s", seg, exc)
                continue
            for tok in list(missing):
                if prices.get(tok):
                    for row in wanted[(seg, tok)]:
                        row["ltp"] = prices[tok]
                    missing.discard(tok)


# ----------------------------------------------------------------------------- Buy / Sell on command


def trade_window_error(section: str, action: str, now: datetime | None = None) -> str | None:
    """Why a Buy or Sell can't be sent right now (None if it can)."""
    sp = spec(section)
    now = now or now_ist()
    t = now.time().replace(tzinfo=None)
    until = sp.buy_until if action == "buy" else sp.sell_until
    hours = f"{sp.opens:%H:%M}–{until:%H:%M}"
    if not is_section_day(now.date(), sp):
        with session_scope() as db:
            h = db.get(Holiday, now.date()) if now.weekday() < 5 else None
            h = None if h is not None and h.source == "removed" else h
        # A closure TradeLab only guessed from quotes never stops anyone selling (Kotak refuses the order
        # if the market really is closed); it would otherwise block every Sell and Exit all day.
        if not (action == "sell" and h is not None and h.source == "detected"):
            why = h.description if h is not None else "weekend"
            return f"The market is closed today ({why}). {sp.label} can {action} on trading days, {hours}."
    if t < sp.opens:
        return f"{sp.label} opens at {sp.opens:%H:%M}. You can {action} {hours}."
    if t >= until:
        return f"Too late to {action} in {sp.label} today (allowed {hours}). Try again on the next trading day."
    return None


def _command_fingerprint(section: str, action: str, live: Live | None, symbols: set[str] | None,
                         accounts: list[int], opened: str | None = None, book: list | None = None) -> str:
    return _digest({"section": section, "action": action, "symbols": sorted(symbols) if symbols else None,
                    "accounts": accounts, **({"opened": opened} if opened else {}),
                    **({"book": book} if book is not None else {}),
                    **(_live_dict(live) if live else {})})


def _sell_book(user_id: int, section: str, symbols: set[str] | None) -> list:
    """What TradeLab holds in a section (the side of every Sell / Exit order), for its fingerprint. A working
    order counts in full here, so its fills arriving between the screen and "Yes" don't make "Yes" fail."""
    return sorted([k.account_id, k.exchange_segment, k.trading_symbol, k.product, h.qty]
                  for k, h in tradelab_book(user_id, section, None, symbols, count_fills=False).items())


def _order_dict(p: PlannedOrder) -> dict:
    return {"account_id": p.account_id, "account_name": p.account_name, "symbol": p.symbol,
            "trading_symbol": p.trading_symbol, "side": p.side, "quantity": p.quantity, "lots": p.lots,
            "price": p.price, "product": p.product, "note": p.note, "skip": p.skip, "funds_short": p.funds_short}


SELL_FIRST_WARNING = ("Selling first can lose far more than the margin Kotak blocks: a sold future or call loses more "
                      "the higher the price goes, with no limit, and a sold put loses as the price falls. Kotak can "
                      "close the position if an account runs short of margin, and SEBI requires at least half of the "
                      "margin in cash.")


DELIVERY_FUTURE_WARNING = ("Gold, silver and metal futures sold first: Kotak stops trading in the contract when its "
                           "delivery (tender) period starts — the last 3–5 trading days before expiry — and closes sold "
                           "positions after 5 PM on the trading day before expiry. One left open at expiry would have "
                           "to be delivered, so buy it back before the delivery period.")
MCX_OPTION_WARNING = ("MCX options sold first: Kotak doesn't close them on expiry day. One that ends in the money turns "
                      "into a futures position (a sold call into a sold future, a sold put into a bought future) "
                      "that needs full futures margin. TradeLab doesn't track that future: it won't show it, and Exit "
                      "and Square off won't close it — it has to be closed in the Kotak Neo app in each account. Buy "
                      "the option back before expiry to avoid this.")
STOCK_DELIVERY_WARNING = ("Stock futures and options sold first are settled by delivery of shares if held to expiry: "
                          "a sold future, or a sold call that ends in the money, delivers shares from the account's "
                          "demat — including the holder's own — and a sold put that ends in the money must pay their "
                          "full value. Shares that aren't there go to the exchange's auction, which can cost 20% or "
                          "more. Kotak blocks extra margin from 4 trading days before expiry and may close positions "
                          "if money falls short.")


def _sell_first_warnings(plan: list[PlannedOrder]) -> list[str]:
    """The risk of selling first, plus the expiry rules of the kinds of contract being sold first."""
    selling = [p for p in plan if p.side == "S" and not p.skip]
    if not selling:
        return []
    out = [SELL_FIRST_WARNING]
    contracts = [c for c in (_contract(p.exchange_segment, p.trading_symbol) for p in selling) if c is not None]
    if any(delivery_future(c) for c in contracts):
        out.append(DELIVERY_FUTURE_WARNING)
    if any(c.exchange_segment == "mcx_fo" and c.is_option for c in contracts):
        out.append(MCX_OPTION_WARNING)
    if any(c.instrument_type in ("FUTSTK", "OPTSTK") for c in contracts):
        out.append(STOCK_DELIVERY_WARNING)
    return out


# "Are you sure?" dialogs shown for Buy / Sell: confirmation id -> (user, section, action, fingerprint,
# shown at, run started from it). Each id starts at most one run; a repeat (a lost reply, a double tap,
# a second device) gets the same run back instead of trading twice.
_confirmations: dict[str, list] = {}
_confirmations_guard = threading.Lock()
CONFIRMATION_TTL = timedelta(minutes=15)


def _new_confirmation(user_id: int, section: str, action: str, fingerprint: str) -> str:
    cid = secrets.token_urlsafe(12)
    now = time.monotonic()
    with _confirmations_guard:
        for k in [k for k, v in _confirmations.items() if now - v[4] > CONFIRMATION_TTL.total_seconds()]:
            del _confirmations[k]
        _confirmations[cid] = [user_id, section, action, fingerprint, now, None]
    return cid


def _use_confirmation(user_id: int, section: str, action: str, confirm_id: str | None, fingerprint: str | None,
                      start) -> int:  # noqa: ANN001
    """Start the run a confirmation id allows, once. A repeat of the same id returns the run it started."""
    if not confirm_id or not fingerprint:
        raise SessionError("This confirmation has expired. Check the details and confirm again.")
    with _confirmations_guard:
        c = _confirmations.get(confirm_id)
        if (c is None or c[:3] != [user_id, section, action]
                or time.monotonic() - c[4] > CONFIRMATION_TTL.total_seconds()):
            raise SessionError("This confirmation has expired. Check the details and confirm again.")
        if c[5] is not None:
            return c[5]  # already started: the same run, never a second one
        if c[3] != fingerprint:
            raise SessionError("Something changed since this was shown. Check the details and confirm again.")
        run_id = start()
        c[5] = run_id
        return run_id


def command_preview(user_id: int, section: str, action: str, symbols: set[str] | None = None,
                    opened: str | None = None) -> dict:
    """What Buy or Sell would send right now (nothing is sent), for the "Are you sure?" dialog.

    `opened` (Sell only): "B" closes only what TradeLab bought, "S" only what it sold first.
    """
    sp = spec(section)
    if action == "buy":
        if sp.scheduled:
            raise SessionError(f"{sp.label} buys with Let's Trade.")
        live = live_items(user_id, section)
        items = [i for i in live.items if not symbols or i.trading_symbol in symbols]
        plan = build_entry_plan(user_id, items, live.sizing_mode, live.reserve_pct, all_prices=True,
                                account_ids=live.accounts, section=section, product=live.product,
                                max_lots=live.max_lots, preview=True)
        fp = _command_fingerprint(section, "buy", live, symbols, live.accounts)
        expired = _expired(live, now_ist().date())
    else:
        if not sp.can_sell:
            raise SessionError(f"{sp.label} is squared off instead.")
        accounts = section_accounts(user_id, section)
        plan = build_sell_plan(user_id, section, symbols, opened=opened, preview=True)
        fp = _command_fingerprint(section, "sell", None, symbols, accounts, opened, _sell_book(user_id, section, symbols))
        expired = []
    warnings = _sell_first_warnings(plan) if action == "buy" else []
    return {"section": section, "action": action, "fingerprint": fp, "blocked": trade_window_error(section, action),
            "expired": expired, "warnings": warnings, "orders": [_order_dict(p) for p in plan],
            "confirm_id": _new_confirmation(user_id, section, action, fp)}


def buy_now(user_id: int, section: str, fingerprint: str | None, symbols: set[str] | None = None,
            confirm_id: str | None = None) -> int:
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
        raise SessionError(f"Add at least one {_noun(section)} (and switch it on) before you buy.")
    expired = _expired(Live(items, live.sizing_mode, live.reserve_pct, live.product, live.max_lots, []),
                       now_ist().date())
    if expired:
        raise SessionError(f"These contracts have expired: {', '.join(expired)}. Remove them first.")
    if not live.accounts:
        raise SessionError(f"Switch on autopilot for at least one account (and choose it for {sp.label}) first.")
    if fingerprint != _command_fingerprint(section, "buy", live, symbols, live.accounts):
        raise SessionError("Something changed since this was shown (items, accounts or settings). "
                           "Check the details and try again.")
    confirmed = time.monotonic()
    return _use_confirmation(user_id, section, "buy", confirm_id, fingerprint, lambda: launch(
        "entry", user_id, section=section, trigger="command",
        job=lambda run_id: _run_buy(user_id, run_id, section, items, live, confirmed)))


def sell_now(user_id: int, section: str, fingerprint: str | None, symbols: set[str] | None = None,
             opened: str | None = None, confirm_id: str | None = None) -> int:
    """The user tapped Sell and said "Yes": sell what the section bought, in every account."""
    sp = spec(section)
    if not sp.can_sell:
        raise SessionError(f"{sp.label} is squared off instead.")
    blocked = trade_window_error(section, "sell")
    if blocked:
        raise SessionError(blocked)
    accounts = section_accounts(user_id, section)
    if fingerprint != _command_fingerprint(section, "sell", None, symbols, accounts, opened,
                                           _sell_book(user_id, section, symbols)):
        raise SessionError("Something changed since this was shown (accounts). Check the details and try again.")
    return _use_confirmation(user_id, section, "sell", confirm_id, fingerprint, lambda: launch(
        "exit", user_id, section=section, trigger="command",
        job=lambda run_id: _run_sell(user_id, run_id, section, symbols, opened)))


def _run_buy(user_id: int, run_id: int, section: str, items: list[PlanItem], live: Live,
             confirmed: float | None = None) -> None:
    started = time.monotonic() if confirmed is None else confirmed
    entries: list[LogEntry] = []
    note = None
    try:
        plan = build_entry_plan(user_id, items, live.sizing_mode, live.reserve_pct, account_ids=live.accounts,
                                section=section, product=live.product, max_lots=live.max_lots)
        blocked = trade_window_error(section, "buy")
        if blocked:
            note = f"Nothing was sent: {blocked}"
        elif not plan:
            note = "Nothing to buy (no accounts)"
        elif _stopped_at.get(user_id, -1.0) >= started:
            note = "Cancelled by “Square off everything” — nothing was sent"
        elif time.monotonic() - started > COMMAND_DEADLINE.total_seconds():
            note = ("Nothing was sent: Kotak was too slow to prepare the orders and prices have moved since you "
                    "confirmed. Check the preview and tap Buy again.")
        else:
            entries = fire_plan(plan, user_id, run_id, section)
            _write_logs(user_id, run_id, _skip_entries(plan), section)
    except Exception as exc:  # noqa: BLE001
        log.exception("Buy run failed")
        entry = LogEntry(None, "—", "buy", "failed", message=f"Buy crashed: {exc}")
        entries.append(entry)
        try:
            _write_logs(user_id, run_id, [entry], section)
        except Exception:  # noqa: BLE001
            log.exception("Recording the crashed buy failed")
    finally:
        _finish_run(run_id, entries, note=note)


def _run_sell(user_id: int, run_id: int, section: str, symbols: set[str] | None, opened: str | None = None) -> None:
    entries: list[LogEntry] = []
    note = None
    plan: list[PlannedOrder] = []
    try:
        _set_scope(run_id, [a.id for a in _all_accounts(user_id)], symbols)
        _wait_for_entries(user_id, now_ist().date(), section)
        with _exit_lock(user_id):
            # TradeLab's own orders in these contracts still working at Kotak are stopped first, so
            # nothing it sent earlier fills after the Exit; then what it holds is closed.
            cancels = _cancel_own_open_orders(user_id, _all_accounts(user_id), section, symbols)
            _write_logs(user_id, run_id, cancels, section)
            entries += cancels
            plan = build_sell_plan(user_id, section, symbols, opened=opened)
            if not plan:
                note = f"{label(section)} holds nothing bought by TradeLab"
            else:
                entries += fire_plan(plan, user_id, run_id, section, closing=True)
                _write_logs(user_id, run_id, _skip_entries(plan), section)
                strays = _stray_orders(user_id, plan)
                _write_logs(user_id, run_id, strays, section)
                entries += strays
    except Exception as exc:  # noqa: BLE001
        log.exception("Sell run failed")
        entry = LogEntry(None, "—", "sell", "failed", message=f"Sell crashed: {exc}")
        entries.append(entry)
        try:
            _write_logs(user_id, run_id, [entry], section)
        except Exception:  # noqa: BLE001
            log.exception("Recording the crashed sell failed")
    finally:
        _finish_run(run_id, entries, note=note)
    if plan:
        try:
            from .reports import capture

            capture(user_id, sorted({p.account_id for p in plan}))
        except Exception:  # noqa: BLE001
            log.exception("Saving P/L after selling failed")


class RunInProgress(Exception):
    pass


def _exit_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _exit_locks.setdefault(user_id, threading.Lock())


_exit_locks: dict[int, threading.Lock] = {}
_launch_locks: dict[int, threading.Lock] = {}


def _launch_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _launch_locks.setdefault(user_id, threading.Lock())
_locks_guard = threading.Lock()


def recover_after_restart() -> None:
    """Runs and confirmations a previous server process left half-done."""
    from .sections import SCHEDULED

    # Each step is saved as it runs, in this order; a step cut short is simply done again at the next start.
    with session_scope() as db:
        # Sections that now buy only on command keep no timed confirmations (e.g. a Holding
        # Let's Trade made by an older version): they are withdrawn, never bought.
        db.update_many(TradeSession, {"section": {"$nin": [*SCHEDULED, None]}, "status": {"$in": ["armed", "running"]}},
                       {"status": "cancelled", "note": "This section now buys only when you tap Buy"})
        db.update_many(ExecutionRun, {"status": "running"},
                       {"status": "failed", "summary": INTERRUPTED, "finished_at": utcnow()})
        db.update_many(TradeSession, {"status": "running"},
                       {"status": "skipped", "note": "Interrupted before any order was sent (server restarted)"})
        db.update_many(TradeSession, {"status": "firing"},
                       {"status": "done", "note": "The server restarted while sending orders. Check Activity and "
                                                  "the Kotak app; these accounts are still squared off."})
        # Orders recorded as about to be sent: their outcome is unknown until Kotak's order book is read
        # (by their tag); until then they count as possibly sent everywhere.
        db.update_many(OrderLog, {"status": "pending"},
                       {"status": UNKNOWN, "sent_at": utcnow(),  # its "not placed" wait starts now
                        "message": "The server restarted while sending this order: it may have "
                                   "gone through. TradeLab checks Kotak's order book for it."})


def launch(kind: str, user_id: int, account_ids: list[int] | None = None, fire_at: datetime | None = None,
           section: str = INTRADAY, trigger: str = "manual", job=None) -> int:
    """Start a run in the background and return its run id.

    Two runs of the same kind and section never overlap; Buy and Sell of an on-command
    section never overlap each other either. A square-off is never refused: it waits its turn
    behind any other square-off (they never overlap) and closes whatever is still open then.
    """
    with _launch_lock(user_id):
        if not (kind == "exit" and section in (ALL, INTRADAY)):
            if spec(section).scheduled:
                busy = _busy(user_id, kind, {section})
            else:  # Buy and Sell of an on-command section
                busy = _busy(user_id, None, {section})
            if busy:
                raise RunInProgress("A trade for this section is still being sent — wait a few seconds and try again.")
        # Created under the lock, so two taps at the same moment can't both start a run.
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
    cash_free = free_money(funds, "CNC")
    positions = [p for p in positions if (p.product or "").upper() != "CNC"]
    positions = with_live_pnl(account_id, positions)
    open_pos = [p for p in positions if p.net_qty != 0]
    pnl = sum(p.pnl or 0 for p in positions)
    return {
        "available": funds.available,
        # What a delivery buy (Holding) can use: cash only, less a little for charges.
        "cash_free": round(cash_free, 2) if cash_free is not None else None,
        "margin_used": funds.margin_used,
        "open_positions": len(open_pos),
        "pnl": round(pnl, 2),
    }
