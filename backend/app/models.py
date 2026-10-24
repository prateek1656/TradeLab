"""Stored records (MongoDB documents). See `db.py` for how they're loaded and saved."""

from __future__ import annotations

from dataclasses import field
from datetime import date, datetime, timezone

from .db import Document, database, document


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cascade_account(db, ids: list, session) -> None:  # noqa: ANN001
    """An account is gone: its order history and P/L records stay, without the link."""
    for coll in ("order_logs", "pnl_records"):
        db[coll].update_many({"account_id": {"$in": ids}}, {"$set": {"account_id": None}}, session=session)


@document("users", indexes=[{"keys": [("email", 1)], "unique": True}])
class User(Document):
    id: int | None = None
    name: str
    email: str
    password_hash: str
    # Bumped to invalidate every token issued before (e.g. password change).
    token_version: int = 0
    created_at: datetime = field(default_factory=utcnow)
    last_login_at: datetime | None = None

    @property
    def is_owner(self) -> bool:
        """The first account created on this server looks after shared settings (market holidays)."""
        first = database()["users"].find_one({}, {"_id": 1}, sort=[("_id", 1)])
        return first is not None and first["_id"] == self.id

    @classmethod
    def _on_delete(cls, db, ids: list, session) -> None:  # noqa: ANN001
        accounts = [d["_id"] for d in db["broker_accounts"].find({"user_id": {"$in": ids}}, {"_id": 1}, session=session)]
        db["broker_accounts"].delete_many({"user_id": {"$in": ids}}, session=session)
        if accounts:
            _cascade_account(db, accounts, session)
        db["automation_settings"].delete_many({"_id": {"$in": ids}}, session=session)
        for coll in ("section_settings", "basket_items", "trade_sessions", "execution_runs", "order_logs",
                     "pnl_records"):
            db[coll].delete_many({"user_id": {"$in": ids}}, session=session)


@document("automation_settings", key=("user_id",))
class AutomationSettings(Document):
    user_id: int
    entry_time: str = "09:15:00"
    exit_time: str = "14:30:00"
    auto_exit: bool = True
    # all = every open position; intraday = only MIS/INTRADAY positions
    squareoff_scope: str = "all"
    # equal = split each account's available funds equally across that day's
    # stocks and buy as many shares as fit; custom = each stock's own size.
    sizing_mode: str = "equal"
    # % of available funds kept aside (not traded) in equal-split mode.
    reserve_pct: float = 0.0


@document("section_settings", key=("user_id", "section"), indexes=[{"keys": [("user_id", 1)]}])
class SectionSettings(Document):
    """Per-user settings of one trading section (intraday, holding, fno, commodity)."""

    user_id: int
    section: str
    entry_time: str | None = None  # None: buys on command
    exit_time: str | None = None  # None: no automatic square-off
    auto_exit: bool = True
    product: str
    # equal = the money free in each account split equally across the items; custom = each item's own size.
    sizing_mode: str = "equal"
    # % of each account's free money kept aside (not traded) when splitting equally.
    reserve_pct: float = 0.0
    # Lot sections: never more than this many lots in one order.
    max_lots: int = 10
    # Accounts this section trades in (None: every account with autopilot on; []: none).
    account_ids: list[int] | None = None


@document("broker_accounts", indexes=[{"keys": [("user_id", 1)]}])
class BrokerAccount(Document):
    # Ids come from a counter, so a deleted account's id is never reused.
    id: int | None = None
    user_id: int
    name: str
    broker: str = "kotak"  # always Kotak Neo
    # Fernet-encrypted JSON: consumer_key, mobile, ucc, mpin, totp_secret
    credentials: str | None = None
    ucc_masked: str | None = None
    client_name: str | None = None
    automation_enabled: bool = True
    status: str = "connected"  # connected | error
    last_error: str | None = None
    last_login_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def _on_delete(cls, db, ids: list, session) -> None:  # noqa: ANN001
        _cascade_account(db, ids, session)


@document("basket_items", indexes=[
    {"keys": [("user_id", 1), ("section", 1), ("exchange_segment", 1), ("trading_symbol", 1)], "unique": True},
    {"keys": [("user_id", 1), ("created_at", 1)]},
])
class BasketItem(Document):
    id: int | None = None
    user_id: int
    section: str = "intraday"
    symbol: str
    trading_symbol: str
    token: str | None = None
    exchange_segment: str = "nse_cm"
    name: str | None = None
    side: str = "B"  # B = buy at entry, S = short/sell at entry
    sizing_mode: str = "qty"  # qty | amount | percent | lots
    sizing_value: float = 1
    active: bool = True
    # None = every automation-enabled account; otherwise a list of account ids.
    account_ids: list[int] | None = None
    # Derivatives (F&O, Commodity): contract details from the exchange's list.
    lot_size: int = 1
    expiry: date | None = None
    instrument_type: str | None = None
    option_type: str | None = None
    strike: float | None = None
    created_at: datetime = field(default_factory=utcnow)


@document("trade_sessions", indexes=[
    {"keys": [("user_id", 1), ("section", 1), ("trade_date", 1)], "unique": True},
    {"keys": [("trade_date", 1), ("status", 1)]},
])
class TradeSession(Document):
    """A trading day the user confirmed with "Let's Trade". Without one, nothing is bought.

    `items` is a snapshot of the stock list at confirmation, so exactly what was
    confirmed is traded even if the list is edited afterwards.
    """

    id: int | None = None
    user_id: int
    section: str = "intraday"
    trade_date: date
    status: str = "armed"  # armed | cancelled | done | skipped
    items: list[dict] = field(default_factory=list)
    sizing_mode: str
    reserve_pct: float = 0.0  # % of free money kept aside
    product: str = "MIS"
    # The section's accounts at confirmation; only these can be traded (None: older rows).
    account_ids: list[int] | None = None
    confirmed_at: datetime = field(default_factory=utcnow)
    note: str | None = None


@document("execution_runs", indexes=[
    {"keys": [("user_id", 1), ("trading_date", 1)]},
    {"keys": [("status", 1)]},
])
class ExecutionRun(Document):
    id: int | None = None
    user_id: int
    kind: str  # entry (buy) | exit (square-off or sell)
    section: str = "intraday"  # "all": square off everything
    trigger: str  # scheduled | manual | catchup | command
    trading_date: date
    status: str = "running"  # running | done | partial | failed
    summary: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    # Accounts that couldn't trade for lack of money in this run (shown as a red pop-up once).
    funds_short: list[str] | None = None
    alert_seen: bool = False

    @classmethod
    def _on_delete(cls, db, ids: list, session) -> None:  # noqa: ANN001
        db["order_logs"].update_many({"run_id": {"$in": ids}}, {"$set": {"run_id": None}}, session=session)


@document("order_logs", indexes=[
    {"keys": [("user_id", 1), ("created_at", 1)]},
    {"keys": [("run_id", 1)]},
    {"keys": [("user_id", 1), ("section", 1)]},
    {"keys": [("account_id", 1)]},
])
class OrderLog(Document):
    id: int | None = None
    user_id: int
    run_id: int | None = None
    account_id: int | None = None
    account_name: str
    action: str  # buy | sell | cancel | login
    symbol: str | None = None
    quantity: int | None = None
    status: str  # success | failed | skipped
    order_id: str | None = None
    message: str | None = None
    exchange_segment: str | None = None
    product: str | None = None
    # Section whose run placed the order (None: older rows, all intraday). Sell only sells
    # what its own section bought, counted from these rows.
    section: str | None = None
    # Average fill price from the broker's order book (None until known), for TradeLab's own P/L.
    price: float | None = None
    # The marker sent with the order (Kotak echoes it as GuiOrdId), to find it when its reply was lost.
    tag: str | None = None
    # True once Kotak's order book showed the order finished (filled, rejected, cancelled or lapsed),
    # or showed it was never placed: status and quantity are then what really happened.
    final: bool = False
    # Units Kotak's order book showed filled while the order was still working (None until the book has
    # shown it). A working order counts by this, not by the quantity asked.
    filled: int | None = None
    # When the send finished (its reply came back or was given up on): "not placed" is judged from here.
    # Derivatives: the contract's lot size and expiry (ISO date) when the order was sent, to notice a bonus or
    # split adjusting the contract later.
    lot_size: int | None = None
    expiry: str | None = None
    sent_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@document("pnl_records", indexes=[
    # Like SQL, the rule ignores rows whose account was deleted (account_id None).
    {"keys": [("user_id", 1), ("trade_date", 1), ("account_id", 1), ("symbol", 1)], "unique": True,
     "partialFilterExpression": {"account_id": {"$type": "number"}}},
    {"keys": [("trade_date", 1)]},
])
class PnlRecord(Document):
    """One stock's profit/loss in one account on one day (saved for daily reports)."""

    id: int | None = None
    user_id: int
    trade_date: date
    account_id: int | None = None
    account_name: str
    symbol: str
    pnl: float
    # Shares bought/sold that day and their average prices (unrounded).
    buy_qty: int = 0
    sell_qty: int = 0
    buy_avg: float = 0.0
    sell_avg: float = 0.0
    # False if the position was still open and no live price was available,
    # so `pnl` covers only the shares already sold.
    pnl_complete: bool = True
    captured_at: datetime = field(default_factory=utcnow)


@document("holidays", key=("day",))
class Holiday(Document):
    day: date
    description: str = "Market holiday"
    source: str = "manual"  # manual | nse | detected | removed (an NSE holiday the owner took out)


@document("clearing_holidays", key=("day",))
class ClearingHoliday(Document):
    """A weekday NSE's clearing corporation and the banks are closed (trades made before it settle later)."""

    day: date


@document("clearing_years", key=("year",))
class ClearingYear(Document):
    """A year whose clearing holidays were downloaded (so a missing day in it is a settlement day)."""

    year: int
