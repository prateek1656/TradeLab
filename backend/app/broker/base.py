"""Broker-agnostic types used by the trading engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class BrokerError(Exception):
    """A broker call failed (rejected, bad credentials, network, ...)."""


class SessionExpired(BrokerError):
    """The broker session is no longer valid; logging in again should fix it."""


@dataclass
class LoginInfo:
    client_name: str | None = None


@dataclass
class Funds:
    available: float
    margin_used: float = 0.0
    collateral: float = 0.0


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    # Statuses after which an order can no longer be cancelled.
    _FINAL = ("complete", "rejected", "cancelled", "traded", "expired", "lapsed")

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
        self, exchange_segment: str, trading_symbol: str, side: str, quantity: int, product: str = "MIS"
    ) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> OrderResult: ...

    def ltp(self, instruments: list[tuple[str, str]]) -> dict[str, float]: ...
