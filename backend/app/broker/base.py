"""Broker-agnostic types used by the trading engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class BrokerError(Exception):
    """A broker call failed (rejected, bad credentials, network, ...)."""


class SessionExpired(BrokerError):
    """The broker session is no longer valid; logging in again should fix it."""


class LoginFailed(BrokerError):
    """Logging in to the account failed, so the request it was for was never sent."""


class OrderOutcomeUnknown(BrokerError):
    """The order request went out but no clear reply came back (timeout, dropped connection,
    a gateway error page): the broker may or may not have the order."""


@dataclass
class LoginInfo:
    client_name: str | None = None


@dataclass
class Funds:
    available: float  # free to trade now, across every segment (includes pledge margin)
    margin_used: float = 0.0
    collateral: float = 0.0  # part of `available` that comes from pledged shares (not usable for delivery buys)


@dataclass
class Position:
    exchange_segment: str
    trading_symbol: str
    product: str
    net_qty: int
    buy_qty: int = 0
    sell_qty: int = 0
    buy_avg: float = 0.0
    sell_avg: float = 0.0
    ltp: float | None = None
    pnl: float | None = None
    token: str | None = None  # exchange token, when the broker reports it
    multiplier: float = 1.0  # contract size for derivatives (prices are per unit)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Holding:
    """Delivery holding (demat), e.g. an ETF bought by the Holding section."""

    trading_symbol: str
    symbol: str
    exchange_segment: str
    quantity: int
    sellable_qty: int
    avg_price: float = 0.0
    ltp: float | None = None
    token: str | None = None

    @property
    def value(self) -> float | None:
        return None if self.ltp is None else round(self.quantity * self.ltp, 2)

    @property
    def pnl(self) -> float | None:
        return None if self.ltp is None else round(self.quantity * (self.ltp - self.avg_price), 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["value"], d["pnl"] = self.value, self.pnl
        return d


@dataclass
class Order:
    order_id: str
    status: str
    trading_symbol: str
    exchange_segment: str
    product: str
    side: str
    quantity: int
    filled_qty: int = 0
    avg_price: float = 0.0
    order_type: str = ""
    placed_at: str | None = None
    message: str | None = None
    tag: str | None = None  # the marker TradeLab sent with the order (Kotak's GuiOrdId)

    # Statuses after which an order can no longer be cancelled.
    # Anything else (put order req received, validation pending, trigger pending, not cancelled, …) is
    # still working: an unknown status is never taken for a finished one.
    _FINAL = ("complete", "rejected", "cancelled", "canceled", "traded", "expired", "lapsed")

    @property
    def is_open(self) -> bool:
        s = (self.status or "").lower()
        return not any(s.startswith(f) for f in self._FINAL)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_open"] = self.is_open
        return d


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class Broker(Protocol):
    def login(self) -> LoginInfo: ...

    def funds(self) -> Funds: ...

    def positions(self) -> list[Position]: ...

    def orders(self) -> list[Order]: ...

    def place_market_order(
        self, exchange_segment: str, trading_symbol: str, side: str, quantity: int, product: str = "MIS",
        tag: str | None = None,
    ) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> OrderResult: ...

    def ltp(self, instruments: list[tuple[str, str]]) -> dict[str, float]: ...

    def holdings(self) -> list[Holding]: ...

    def margin_per_lot(self, exchange_segment: str, token: str, trading_symbol: str, side: str, product: str,
                       quantity: int, price: float | None = None) -> float: ...
