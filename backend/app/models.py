from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Bumped to invalidate every token issued before (e.g. password change).
    token_version: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def is_owner(self) -> bool:
        """The first account created on this server looks after shared settings (market holidays)."""
        db = object_session(self)
        return db is not None and db.scalar(select(func.min(User.id))) == self.id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    automation: Mapped[AutomationSettings | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class AutomationSettings(Base):
    __tablename__ = "automation_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    entry_time: Mapped[str] = mapped_column(String(8), default="09:15:00")
    exit_time: Mapped[str] = mapped_column(String(8), default="14:30:00")
    auto_exit: Mapped[bool] = mapped_column(Boolean, default=True)
    # all = every open position; intraday = only MIS/INTRADAY positions
    squareoff_scope: Mapped[str] = mapped_column(String(16), default="all")
    # equal = split each account's available funds equally across that day's
    # stocks and buy as many shares as fit; custom = each stock's own size.
    sizing_mode: Mapped[str] = mapped_column(String(10), default="equal")
    # % of available funds kept aside (not traded) in equal-split mode.
    reserve_pct: Mapped[float] = mapped_column(Float, default=0.0)

    user: Mapped[User] = relationship(back_populates="automation")


class SectionSettings(Base):
    """Per-user settings of one trading section (intraday, holding, fno, commodity)."""

    __tablename__ = "section_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    section: Mapped[str] = mapped_column(String(16), primary_key=True)
    entry_time: Mapped[str | None] = mapped_column(String(8), nullable=True)  # None: buys on command
    exit_time: Mapped[str | None] = mapped_column(String(8), nullable=True)  # None: no automatic square-off
    auto_exit: Mapped[bool] = mapped_column(Boolean, default=True)
    product: Mapped[str] = mapped_column(String(8))
    # equal = the money free in each account split equally across the items; custom = each item's own size.
    sizing_mode: Mapped[str] = mapped_column(String(10), default="equal")
    # % of each account's free money kept aside (not traded) when splitting equally.
    reserve_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Lot sections: never more than this many lots in one order.
    max_lots: Mapped[int] = mapped_column(Integer, default=10)
    # Accounts this section trades in (None: every account with autopilot on; []: none).
    account_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    __table_args__ = {"sqlite_autoincrement": True}  # never reuse a deleted account's id

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    broker: Mapped[str] = mapped_column(String(16), default="kotak")  # kotak | paper
    # Fernet-encrypted JSON: consumer_key, mobile, ucc, mpin, totp_secret
    credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    ucc_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="connected")  # connected | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paper_funds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BasketItem(Base):
    __tablename__ = "basket_items"
    __table_args__ = (UniqueConstraint("user_id", "section", "exchange_segment", "trading_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(16), default="intraday", index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    trading_symbol: Mapped[str] = mapped_column(String(60))
    token: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exchange_segment: Mapped[str] = mapped_column(String(10), default="nse_cm")
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    side: Mapped[str] = mapped_column(String(1), default="B")  # B = buy at entry, S = short/sell at entry
    sizing_mode: Mapped[str] = mapped_column(String(10), default="qty")  # qty | amount | percent | lots
    sizing_value: Mapped[float] = mapped_column(Float, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None = every automation-enabled account; otherwise a list of account ids.
    account_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    # Derivatives (F&O, Commodity): contract details from the exchange's list.
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    instrument_type: Mapped[str | None] = mapped_column(String(12), nullable=True)
    option_type: Mapped[str | None] = mapped_column(String(4), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TradeSession(Base):
    """A trading day the user confirmed with "Let's Trade". Without one, nothing is bought.

    `items` is a snapshot of the stock list at confirmation, so exactly what was
    confirmed is traded even if the list is edited afterwards.
    """

    __tablename__ = "trade_sessions"
    __table_args__ = (UniqueConstraint("user_id", "section", "trade_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(16), default="intraday")
    trade_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="armed")  # armed | cancelled | done | skipped
    items: Mapped[list[dict]] = mapped_column(JSON)
    sizing_mode: Mapped[str] = mapped_column(String(10))
    reserve_pct: Mapped[float] = mapped_column(Float, default=0.0)  # % of free money kept aside
    product: Mapped[str] = mapped_column(String(8), default="MIS")
    # The section's accounts at confirmation; only these can be traded (None: older rows).
    account_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExecutionRun(Base):
    __tablename__ = "execution_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # entry (buy) | exit (square-off or sell)
    section: Mapped[str] = mapped_column(String(16), default="intraday")  # "all": square off everything
    trigger: Mapped[str] = mapped_column(String(12))  # scheduled | manual | catchup | command
    trading_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | done | partial | failed
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Accounts that couldn't trade for lack of money in this run (shown as a red pop-up once).
    funds_short: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    alert_seen: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderLog(Base):
    __tablename__ = "order_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_name: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(12))  # buy | sell | cancel | login
    symbol: Mapped[str | None] = mapped_column(String(60), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10))  # success | failed | skipped
    order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    exchange_segment: Mapped[str | None] = mapped_column(String(10), nullable=True)
    product: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Section whose run placed the order (None: older rows, all intraday). Sell only sells
    # what its own section bought, counted from these rows.
    section: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PnlRecord(Base):
    """One stock's profit/loss in one account on one day (saved for daily reports)."""

    __tablename__ = "pnl_records"
    __table_args__ = (UniqueConstraint("user_id", "trade_date", "account_id", "symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_name: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str] = mapped_column(String(60))
    pnl: Mapped[float] = mapped_column(Float)
    # Shares bought/sold that day and their average prices (unrounded).
    buy_qty: Mapped[int] = mapped_column(Integer, default=0)
    sell_qty: Mapped[int] = mapped_column(Integer, default=0)
    buy_avg: Mapped[float] = mapped_column(Float, default=0.0)
    sell_avg: Mapped[float] = mapped_column(Float, default=0.0)
    # False if the position was still open and no live price was available,
    # so `pnl` covers only the shares already sold.
    pnl_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Holiday(Base):
    __tablename__ = "holidays"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    description: Mapped[str] = mapped_column(String(120), default="Market holiday")
    source: Mapped[str] = mapped_column(String(10), default="manual")  # manual | nse | detected


class PaperPosition(Base):
    """Positions of simulated (paper) accounts, one row per instrument per day."""

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "trading_date", "exchange_segment", "trading_symbol", "product"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("broker_accounts.id", ondelete="CASCADE"), index=True)
    trading_date: Mapped[date] = mapped_column(Date)
    exchange_segment: Mapped[str] = mapped_column(String(10))
    trading_symbol: Mapped[str] = mapped_column(String(60))
    product: Mapped[str] = mapped_column(String(10))
    buy_qty: Mapped[int] = mapped_column(Integer, default=0)
    sell_qty: Mapped[int] = mapped_column(Integer, default=0)
    buy_amt: Mapped[float] = mapped_column(Float, default=0.0)
    sell_amt: Mapped[float] = mapped_column(Float, default=0.0)
