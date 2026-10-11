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
from .base import BrokerError, Funds, Holding, LoginInfo, Order, OrderResult, Position

_order_seq = itertools.count(1)
_paper_orders: dict[int, list[Order]] = {}


def estimate_margin(trading_symbol: str, price: float, quantity: int, side: str, product: str,
                    multiplier: float = 1.0) -> float:
    """Paper margin: an option buyer pays the premium; an option writer blocks several times the
    premium (real margin follows the underlying); anything else ~15% of the contract value.
    `multiplier`: price units per order unit (MCX quotes gold per 10 g, a GOLD lot is 1 kg)."""
    option = trading_symbol.upper().endswith(("CE", "PE"))
    rate = (1.0 if side == "B" else 3.0) if option else 0.15
    return round(price * quantity * multiplier * rate, 2)


def contract_multiplier(exchange_segment: str, trading_symbol: str) -> float:
    """The contract's price multiplier from today's contract list (1 for anything else)."""
    if exchange_segment != "mcx_fo":
        return 1.0
    try:
        from ..services.contracts import contract_master

        c = contract_master.get(exchange_segment, trading_symbol)
        return (c.multiplier or 1.0) if c else 1.0
    except Exception:  # noqa: BLE001 - paper estimate only
        return 1.0


DERIVATIVES = ("nse_fo", "bse_fo", "mcx_fo")


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
            paper_funds = acc.paper_funds
        # Money spent on delivery (Holding) stays spent until the units are sold, and open
        # trades block their value (stocks) or margin (contracts) until they are closed.
        invested = sum(h.quantity * h.avg_price for h in self.holdings())
        blocked = 0.0
        for p in self._rows(today_only=False):
            if p.product == "CNC" or p.net_qty == 0:
                continue
            avg = p.buy_avg if p.net_qty > 0 else p.sell_avg
            if p.exchange_segment in DERIVATIVES:
                blocked += estimate_margin(p.trading_symbol, avg, abs(p.net_qty), "B" if p.net_qty > 0 else "S",
                                           p.product, contract_multiplier(p.exchange_segment, p.trading_symbol))
            else:
                blocked += abs(p.net_qty) * avg
        return Funds(available=round(paper_funds - invested - blocked, 2), margin_used=round(blocked, 2))

    def holdings(self) -> list[Holding]:
        """Delivery (CNC) units bought and not sold, across all days."""
        with session_scope() as db:
            rows = db.scalars(
                select(PaperPosition).where(PaperPosition.account_id == self.account_id, PaperPosition.product == "CNC")
            ).all()
            totals: dict[tuple[str, str], list[float]] = {}
            for r in rows:
                t = totals.setdefault((r.exchange_segment, r.trading_symbol), [0, 0.0, 0])
                t[0] += r.buy_qty
                t[1] += r.buy_amt
                t[2] += r.sell_qty
        out = []
        for (seg, ts), (bought, spent, sold) in totals.items():
            qty = int(bought - sold)
            if qty <= 0:
                continue
            out.append(Holding(trading_symbol=ts, symbol=ts.rsplit("-", 1)[0], exchange_segment=seg, quantity=qty,
                               sellable_qty=qty, avg_price=round(spent / bought, 4) if bought else 0.0,
                               ltp=self._price(seg, ts)))
        return out

    def margin_per_lot(self, exchange_segment: str, token: str, trading_symbol: str, side: str, product: str,
                       quantity: int, price: float | None = None) -> float:
        price = price or self._price(exchange_segment, trading_symbol, token)
        return estimate_margin(trading_symbol, price, quantity, side, product,
                               contract_multiplier(exchange_segment, trading_symbol))

    def _rows(self, today_only: bool = True) -> list[Position]:
        """Today's positions, plus carry-forward (NRML) contracts still open from earlier days."""
        today = datetime.now(IST).date()
        with session_scope() as db:
            q = select(PaperPosition).where(PaperPosition.account_id == self.account_id)
            rows = db.scalars(q).all()
            merged: dict[tuple[str, str, str], list[float]] = {}
            for r in rows:
                if r.trading_date != today and r.product != "NRML":
                    continue
                m = merged.setdefault((r.exchange_segment, r.trading_symbol, r.product), [0, 0, 0.0, 0.0, False])
                m[0] += r.buy_qty
                m[1] += r.sell_qty
                m[2] += r.buy_amt
                m[3] += r.sell_amt
                m[4] = m[4] or r.trading_date == today
        out = []
        for (seg, ts, prod), (bq, sq, ba, sa, today_row) in merged.items():
            net = int(bq - sq)
            if not today_row and net == 0:
                continue  # closed on an earlier day
            out.append(Position(exchange_segment=seg, trading_symbol=ts, product=prod, net_qty=net, buy_qty=int(bq),
                                sell_qty=int(sq), buy_avg=ba / bq if bq else 0.0, sell_avg=sa / sq if sq else 0.0))
        return out

    def positions(self) -> list[Position]:
        out = self._rows()
        for p in out:
            p.ltp = self._price(p.exchange_segment, p.trading_symbol)
            p.multiplier = contract_multiplier(p.exchange_segment, p.trading_symbol)
            p.pnl = round(((p.sell_avg * p.sell_qty - p.buy_avg * p.buy_qty) + p.net_qty * p.ltp) * p.multiplier, 2)
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
