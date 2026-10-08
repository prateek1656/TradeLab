"""Simulated broker for paper accounts: orders fill instantly at a reference price."""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select

from ..config import IST
from ..db import session_scope
from ..models import BrokerAccount, PaperPosition
from .base import BrokerError, Funds, LoginInfo, Order, OrderResult, Position

_order_seq = itertools.count(1)
_paper_orders: dict[int, list[Order]] = {}


def synthetic_price(trading_symbol: str, when: datetime | None = None) -> float:
    """Deterministic, gently moving price used when no live quote is available."""
    when = when or datetime.now(IST)
    h = int(hashlib.sha256(trading_symbol.encode()).hexdigest()[:8], 16)
    base = 80 + (h % 2900)
    minute = when.hour * 60 + when.minute
    drift = 0.012 * math.sin(minute / 47 + (h % 360)) + 0.004 * math.sin(minute / 7 + (h % 17))
    return round(base * (1 + drift), 2)


class PaperBroker:
    def __init__(self, account_id: int, price_lookup: Callable[[str, str, str | None], float | None] | None = None):
        self.account_id = account_id
        self._price_lookup = price_lookup

    def _price(self, exchange_segment: str, trading_symbol: str, token: str | None = None) -> float:
        if self._price_lookup:
            try:
                live = self._price_lookup(exchange_segment, trading_symbol, token)
                if live:
                    return live
            except Exception:  # noqa: BLE001 - fall back to synthetic pricing
                pass
        return synthetic_price(trading_symbol)

    def login(self) -> LoginInfo:
        return LoginInfo(client_name="Paper trader")

    def funds(self) -> Funds:
        with session_scope() as db:
            acc = db.get(BrokerAccount, self.account_id)
            if acc is None:
                raise BrokerError("Paper account not found")
            return Funds(available=round(acc.paper_funds, 2))

    def positions(self) -> list[Position]:
        today = datetime.now(IST).date()
        with session_scope() as db:
            rows = db.scalars(
                select(PaperPosition).where(
                    PaperPosition.account_id == self.account_id, PaperPosition.trading_date == today
                )
            ).all()
            out = []
            for r in rows:
                net = r.buy_qty - r.sell_qty
                ltp = self._price(r.exchange_segment, r.trading_symbol)
                out.append(
                    Position(
                        exchange_segment=r.exchange_segment,
                        trading_symbol=r.trading_symbol,
                        product=r.product,
                        net_qty=net,
                        buy_qty=r.buy_qty,
                        sell_qty=r.sell_qty,
                        buy_avg=round(r.buy_amt / r.buy_qty, 2) if r.buy_qty else 0.0,
                        sell_avg=round(r.sell_amt / r.sell_qty, 2) if r.sell_qty else 0.0,
                        ltp=ltp,
                        pnl=round((r.sell_amt - r.buy_amt) + net * ltp, 2),
                    )
                )
            return out

    def orders(self) -> list[Order]:
        today = datetime.now(IST).strftime("%d-%b-%Y")
        return [o for o in _paper_orders.get(self.account_id, []) if (o.placed_at or "").startswith(today)]

    def place_market_order(
        self, exchange_segment: str, trading_symbol: str, side: str, quantity: int, product: str = "MIS"
    ) -> OrderResult:
        if quantity <= 0:
            return OrderResult(ok=False, message="Quantity must be positive")
        price = self._price(exchange_segment, trading_symbol)
        now = datetime.now(IST)
        with session_scope() as db:
            pos = db.scalars(
                select(PaperPosition).where(
                    PaperPosition.account_id == self.account_id,
                    PaperPosition.trading_date == now.date(),
                    PaperPosition.exchange_segment == exchange_segment,
                    PaperPosition.trading_symbol == trading_symbol,
                    PaperPosition.product == product,
                )
            ).first()
            if pos is None:
                pos = PaperPosition(
                    account_id=self.account_id,
                    trading_date=now.date(),
                    exchange_segment=exchange_segment,
                    trading_symbol=trading_symbol,
                    product=product,
                    buy_qty=0,
                    sell_qty=0,
                    buy_amt=0.0,
                    sell_amt=0.0,
                )
                db.add(pos)
            if side == "B":
                pos.buy_qty += quantity
                pos.buy_amt += quantity * price
            else:
                pos.sell_qty += quantity
                pos.sell_amt += quantity * price
        order_id = f"PAPER{now:%y%m%d}{next(_order_seq):06d}"
        _paper_orders.setdefault(self.account_id, []).append(
            Order(
                order_id=order_id,
                status="complete",
                trading_symbol=trading_symbol,
                exchange_segment=exchange_segment,
                product=product,
                side=side,
                quantity=quantity,
                filled_qty=quantity,
                avg_price=price,
                order_type="MKT",
                placed_at=now.strftime("%d-%b-%Y %H:%M:%S"),
            )
        )
        return OrderResult(ok=True, order_id=order_id, message=f"Filled at ₹{price:,.2f} (paper)")

    def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(ok=False, order_id=order_id, message="Paper orders fill instantly")

    def ltp(self, instruments: list[tuple[str, str]]) -> dict[str, float]:
        return {}
