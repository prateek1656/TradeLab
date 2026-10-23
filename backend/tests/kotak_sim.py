"""Kotak Neo's SDK, simulated account by account, answering in the formats Kotak's servers send.

Most trading tests use FakeKotak (conftest), which stands in for TradeLab's own Kotak adapter, so they
never exercise the code that reads Kotak's replies. These tests plug the real adapter
(app.broker.kotak.KotakNeoBroker) into this simulated SDK instead: every buy, sell, exit and square-off
then also runs through that code, with Kotak's empty reports, refusals and faults included.

Reply formats follow the official SDK's documentation (docs/functions/*.md of kotak-neo-python) and
what real accounts have sent: the "No Data" report (stCode 5203) for an empty order book or positions,
and "No holdings found for this client <UCC>" for an empty demat.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any

from app.broker import kotak

# Kotak's documented answer for a report with nothing in it.
NO_DATA = {"stCode": 5203, "errMsg": "No Data", "desc": "data not found", "stat": "Not_Ok"}


def no_holdings(ucc: str, shape: str = "error") -> dict:
    """Kotak's answer for an empty demat (seen from a real account), in the envelopes Kotak uses."""
    msg = f"No holdings found for this client {ucc}"
    if shape == "emsg":
        return {"stat": "Not_Ok", "emsg": msg}
    if shape == "errMsg":
        return {"stat": "Not_Ok", "errMsg": msg}
    return {"error": [{"code": "1007", "message": msg}], "stat": "Not_Ok"}


# Prices by trading symbol and by token (anything else is ₹100), shared by every account.
PRICES: dict[str, float] = {}
TOKEN_PRICES: dict[str, float] = {}
# Margin per unit: futures and options bought use 15% of the price, anything sold first 200%.
MARGIN_FACTOR = {"B": 0.15, "S": 2.0}
PLACED_BUT = "placed"  # a place_order fault that still lets the order through (its reply is lost)
NO_FAULT = object()


def price_of(trading_symbol: str) -> float:
    return PRICES.get(trading_symbol, 100.0)


@dataclass
class _Pos:
    seg: str
    ts: str
    prod: str
    buy: int = 0
    sell: int = 0
    buy_amt: float = 0.0
    sell_amt: float = 0.0
    cf_buy: int = 0
    cf_sell: int = 0
    taken: int = 0  # units today's sales took from the demat (delivery)

    @property
    def net(self) -> int:
        return self.cf_buy + self.buy - self.cf_sell - self.sell


@dataclass
class SimAccount:
    """One Kotak account as its SDK client sees it."""

    ucc: str
    name: str = "SIM CLIENT"
    cash: float = 25000.0
    collateral: float = 0.0
    demat: dict[str, list] = field(default_factory=dict)  # trading symbol -> [qty, sellable, avg]
    open_positions: dict[tuple[str, str, str], _Pos] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    # SDK method -> reply to send instead (a list is used up one reply per call; a callable is called).
    # For place_order, (PLACED_BUT, reply) places the order but sends `reply` back.
    faults: dict[str, Any] = field(default_factory=dict)
    reject_in_book: str | None = None  # accept the next order, then reject it with this reason
    partial_next: int | None = None  # the next order fills only this many units and stays working ("open")
    # (both set: the next order fills partial_next units, then the rest is rejected)
    cancel_lag: int = 0  # a cancel Kotak accepts still shows the order working for this many book reads
    hide_positions: set[str] = field(default_factory=set)  # trading symbols the positions reply lags behind on
    empty_holdings_shape: str = "error"
    calls: list[tuple[str, dict]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    _ids = itertools.count(260001000)

    # -- helpers ---------------------------------------------------------------
    def _fault(self, method: str):
        """The reply to send instead of the normal one (NO_FAULT when there is none)."""
        if method not in self.faults:
            return NO_FAULT
        f = self.faults[method]
        if isinstance(f, list):
            if not f:
                return NO_FAULT
            f = f.pop(0)
        return f(self) if callable(f) else f

    def net(self, ts: str, prod: str) -> int:
        return sum(p.net for k, p in self.open_positions.items() if k[1] == ts and k[2] == prod)

    def sold(self) -> list[dict]:
        return [o for o in self.orders if o["trnsTp"] == "S" and o["ordSt"] == "complete"]

    def add_open_order(self, trading_symbol: str, segment: str, product: str, side: str, qty: int,
                       status: str = "trigger pending") -> str:
        """An order the holder placed in the Kotak app (a stop-loss, say), still working."""
        oid = str(next(self._ids))
        with self.lock:
            self.orders.append({"nOrdNo": oid, "ordSt": status, "trdSym": trading_symbol, "exSeg": segment,
                                "prod": product, "trnsTp": side, "qty": qty, "fldQty": 0, "avgPrc": "0.00",
                                "prcTp": "SL-M", "rejRsn": "--", "GuiOrdId": "", "ordDtTm": "22-Oct-2026 10:00:00"})
        return oid

    def order(self, oid: str) -> dict:
        return next(o for o in self.orders if o["nOrdNo"] == oid)

    def clear_day_positions(self) -> None:
        """Kotak clears the day's positions before delivery buys show in the demat (the T+1 gap)."""
        with self.lock:
            self.open_positions = {k: p for k, p in self.open_positions.items() if p.prod == "NRML"}
            self.orders = []

    def settle_overnight(self) -> None:
        """The next trading day: delivery buys reach the demat, carry-forward positions carry, the books clear."""
        carried: dict[tuple[str, str, str], _Pos] = {}
        for k, p in self.open_positions.items():
            if p.prod == "CNC":
                net = p.buy - p.sell + p.taken  # what today's sales took from the demat already left it
                if net > 0:
                    h = self.demat.setdefault(p.ts, [0, 0, 0.0])
                    h[0] += net
                    h[1] += net
                    h[2] = p.buy_amt / p.buy if p.buy else h[2]
            elif p.prod == "NRML" and p.net:
                carried[k] = _Pos(p.seg, p.ts, p.prod, cf_buy=max(p.net, 0), cf_sell=max(-p.net, 0))
        self.open_positions = carried
        self.orders = []

    # -- the SDK's methods, called with keyword arguments as TradeLab calls them ---------------------
    def totp_login(self, mobile_number: str, ucc: str, totp: str) -> dict:
        self.calls.append(("totp_login", {"ucc": ucc}))
        f = self._fault("totp_login")
        if f is not NO_FAULT:
            return f
        return {"data": {"token": "view-token", "sid": "sid", "greetingName": self.name}}

    def totp_validate(self, mpin: str) -> dict:
        f = self._fault("totp_validate")
        if f is not NO_FAULT:
            return f
        return {"data": {"token": "trade-token", "sid": "sid", "greetingName": self.name}}

    def limits(self, **kw) -> dict:
        self.calls.append(("limits", kw))
        f = self._fault("limits")
        if f is not NO_FAULT:
            return f
        return {"Net": f"{self.cash:.2f}", "MarginUsed": "0.00", "Collateral": f"{self.collateral:.2f}",
                "CollateralValue": f"{self.cash:.2f}", "stat": "Ok", "stCode": 200}

    def positions(self, **kw) -> dict:
        self.calls.append(("positions", kw))
        f = self._fault("positions")
        if f is not NO_FAULT:
            return f
        with self.lock:
            rows = [{
                "exSeg": p.seg, "trdSym": p.ts, "prod": p.prod,
                "cfBuyQty": str(p.cf_buy), "cfSellQty": str(p.cf_sell),
                "flBuyQty": str(p.buy), "flSellQty": str(p.sell),
                "buyAmt": f"{p.buy_amt:.2f}", "sellAmt": f"{p.sell_amt:.2f}",
                "cfBuyAmt": "0.00", "cfSellAmt": "0.00", "multiplier": "1", "genNum": "1", "genDen": "1",
                "prcNum": "1", "prcDen": "1", "ltp": f"{price_of(p.ts):.2f}", "tok": "1",
            } for p in self.open_positions.values() if p.ts not in self.hide_positions]
        return {"stat": "ok", "stCode": 200, "data": rows} if rows else dict(NO_DATA)

    def holdings(self, **kw) -> dict:
        self.calls.append(("holdings", kw))
        f = self._fault("holdings")
        if f is not NO_FAULT:
            return f
        with self.lock:
            rows = []
            for ts, (qty, sellable, avg) in self.demat.items():
                if qty <= 0:
                    continue
                sym, _, series = ts.rpartition("-")
                rows.append({"displaySymbol": sym, "symbol": sym, "series": series, "exchangeSegment": "nse_cm",
                             "exchangeIdentifier": "1", "quantity": qty, "sellableQuantity": sellable,
                             "averagePrice": avg, "closingPrice": price_of(ts), "instrumentType": "Equity"})
        return {"data": rows} if rows else no_holdings(self.ucc, self.empty_holdings_shape)

    def order_report(self, **kw) -> dict:
        self.calls.append(("order_report", kw))
        f = self._fault("order_report")
        if f is not NO_FAULT:
            return f
        with self.lock:
            for o in self.orders:  # a lagging cancel shows up after a few reads
                if o.get("_cancel_in") is not None:
                    if o["_cancel_in"] <= 0:
                        o["ordSt"], o["_cancel_in"] = "cancelled", None
                    else:
                        o["_cancel_in"] -= 1
            rows = [{k: v for k, v in o.items() if not k.startswith("_")} for o in self.orders]
        return {"stat": "Ok", "stCode": 200, "data": rows} if rows else dict(NO_DATA)

    def margin_required(self, exchange_segment, price, order_type, product, quantity, instrument_token,
                        transaction_type, **kw) -> dict:
        f = self._fault("margin_required")
        if f is not NO_FAULT:
            return f
        margin = float(price) * int(quantity) * MARGIN_FACTOR[transaction_type]
        return {"data": {"ordMrgn": f"{margin:.2f}", "stat": "Ok"}, "stat": "Ok", "stCode": 200}

    def quotes(self, instrument_tokens, quote_type="ltp", **kw):
        f = self._fault("quotes")
        if f is not NO_FAULT:
            return f
        return [{"exchange_token": t["instrument_token"], "ltp": f"{TOKEN_PRICES.get(t['instrument_token'], 100.0):.2f}"}
                for t in instrument_tokens]

    def scrip_master(self, **kw) -> dict:
        return {"Error Message": "offline in tests"}

    def place_order(self, exchange_segment, product, price, order_type, quantity, validity, trading_symbol,
                    transaction_type, amo="NO", tag=None, **kw) -> dict:
        self.calls.append(("place_order", {"trading_symbol": trading_symbol, "side": transaction_type,
                                           "quantity": int(quantity), "product": product, "tag": tag}))
        f = self._fault("place_order")
        lost_reply = None
        if f is not NO_FAULT:
            if isinstance(f, tuple) and f[0] == PLACED_BUT:
                lost_reply = f[1]
            else:
                return f
        qty, side, px = int(quantity), transaction_type, price_of(trading_symbol)
        oid = str(next(self._ids))
        with self.lock:
            row = {"nOrdNo": oid, "ordSt": "complete", "trdSym": trading_symbol, "exSeg": exchange_segment,
                   "prod": product, "trnsTp": side, "qty": qty, "fldQty": qty, "avgPrc": f"{px:.2f}",
                   "prcTp": order_type, "rejRsn": "--", "GuiOrdId": tag or "", "ordDtTm": "22-Oct-2026 10:00:00"}
            if self.reject_in_book and self.partial_next is None:
                row.update(ordSt="rejected", fldQty=0, avgPrc="0.00", rejRsn=self.reject_in_book)
                self.reject_in_book = None
                self.orders.append(row)
                return lost_reply or {"stat": "Ok", "nOrdNo": oid, "stCode": 200}
            if self.partial_next is not None:  # a thin market: part fills, the rest keeps working
                qty = min(qty, self.partial_next)
                row.update(ordSt="open", fldQty=qty)
                self.partial_next = None
                if self.reject_in_book:  # part filled, then the rest rejected (an exchange limit, say)
                    row.update(ordSt="rejected", rejRsn=self.reject_in_book)
                    self.reject_in_book = None
            p = self.open_positions.setdefault((exchange_segment, trading_symbol, product),
                                          _Pos(exchange_segment, trading_symbol, product))
            if side == "B":
                p.buy += qty
                p.buy_amt += qty * px
                if product == "CNC":
                    self.cash -= qty * px
            else:
                p.sell += qty
                p.sell_amt += qty * px
                if product == "CNC":
                    self.cash += qty * px
                    # Selling delivery units bought on an earlier day takes them from the demat.
                    h = self.demat.get(trading_symbol)
                    if h and p.sell - p.taken > p.buy:
                        take = min(h[1], p.sell - p.taken - p.buy)
                        h[0] -= take
                        h[1] -= take
                        p.taken += take
            self.orders.append(row)
        return lost_reply or {"stat": "Ok", "nOrdNo": oid, "stCode": 200}

    def cancel_order(self, order_id, amo="NO", isVerify=False, **kw) -> dict:
        f = self._fault("cancel_order")
        if f is not NO_FAULT:
            return f
        with self.lock:
            for o in self.orders:
                if o["nOrdNo"] == order_id and o["ordSt"] not in ("complete", "rejected", "cancelled"):
                    if self.cancel_lag:
                        o["_cancel_in"] = self.cancel_lag
                    else:
                        o["ordSt"] = "cancelled"
        return {"stat": "Ok", "nOrdNo": order_id, "stCode": 200}


class KotakSim:
    """Every simulated account, by UCC; plugs the real adapter into TradeLab's broker manager."""

    def __init__(self) -> None:
        self.accounts: dict[str, SimAccount] = {}

    def account(self, ucc: str, **kw) -> SimAccount:
        ucc = ucc.upper()
        if ucc not in self.accounts:
            self.accounts[ucc] = SimAccount(ucc, **kw)
        else:
            for k, v in kw.items():
                setattr(self.accounts[ucc], k, v)
        return self.accounts[ucc]

    def factory(self, creds: dict[str, str]) -> kotak.KotakNeoBroker:
        """A real KotakNeoBroker whose SDK client is the simulated account."""
        b = kotak.KotakNeoBroker.__new__(kotak.KotakNeoBroker)
        b._creds = creds
        b._client = self.account(creds["ucc"])
        b._lock = threading.Lock()
        b._last_totp_step = None
        b.logged_in = False
        return b
