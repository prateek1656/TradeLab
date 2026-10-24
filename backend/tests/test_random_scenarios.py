"""Randomized scenario test: seeded small worlds driven through the real API and the real Kotak adapter.

A seed builds 1-3 family accounts on the simulated Kotak (kotak_sim), some holding shares or contracts of their
own, the four sections' lists, and a random sequence of steps over one to four trading days: TradeLab's own
actions (Let's Trade, Buy now, Sell / Exit, square-off, the panic button, the 15:40 / 23:58 true-ups, two of them at
once, a server restart or death in the middle of a run), the account holders' own trades (orders without a TradeLab
tag, also inside a TradeLab run), and Kotak's faults and lags (lost order replies, gateway pages, 5xx, stCode 1003,
unreadable books, positions that lag, part fills, late rejections, slow cancels, the T+1 gap in the demat, day orders
ending at the close, expiry, a book that does not echo TradeLab's marker).

An ownership oracle follows every order the simulator receives: orders tagged "TL..." are TradeLab's, anything
else is the holder's; a holder's sale takes the holder's own units first and TradeLab's after them. Invariants
checked after every step:

  1. one Kotak order per TradeLab tag, and no TradeLab-tagged order TradeLab has no record of; lots only;
  2. a TradeLab order never takes the holder's own units (the Intraday square-off and the panic button close every
     MIS position of the accounts in their scope by design, so MIS closes are exempt), never opens against the
     holder's opposite position in F&O / Commodity, never cancels her delivery / carry-forward orders;
  3. no TradeLab order flips a position, and no stop-loss of hers flips one after TradeLab closed beside it;
  4. a run reported "done" (Sell / Exit, panic, square-off) leaves no TradeLab units open in its scope and no working
     order of hers that would open a new position on the side it closed, otherwise it is partial / failed and the
     exit pop-up names the account; a done F&O / Commodity Buy has every order filled in full, sells a contract first
     only after every bought contract of its underlying filled, and an Exit never sells a hedge beside an open short;
  5. at quiet moments (faults cleared, orders reconciled, true-up run) TradeLab's records equal the oracle's units
     (HANDOFF's accepted cases and the designed settling allowance excepted; see the exemptions in World).

Run by file name or as part of the suite (30 default seeds, about half a minute):
    python -m pytest -q -p no:cacheprovider tests/test_random_scenarios.py
Many seeds (use 3 processes at once; each pytest process has its own database):
    TL_FUZZ_SEEDS=2000 TL_FUZZ_START=0 python -m pytest -q -p no:cacheprovider tests/test_random_scenarios.py -k fuzz -s
    TL_FUZZ_OUT=file.jsonl appends every violating seed with its spec; TL_FUZZ_PROFILE=hedge|holding|derivs|intraday|
    race|broad keeps one kind of scenario; TL_FUZZ_NOECHO=1 lets MCX books hide TradeLab's marker (finding G-2);
    TL_FUZZ_MUTANT=<name> breaks TradeLab on purpose to prove the oracle notices (see apply_mutant).
One seed with its step log:          TL_FUZZ_ONLY=123 TL_FUZZ_SEEDS=1 ... -k fuzz -s
Replay a recorded spec:              TL_FUZZ_REPLAY=file.jsonl TL_FUZZ_ONLY=123 ... -k fuzz -s
Shrink a failing scenario:           TL_FUZZ_SHRINK=123:RECORDS_TOO_LOW (or file.jsonl:123:KIND) ... -k shrink -s
(Threads make a few races timing-dependent: a seed that fails once may need several replays.)
The xfail tests at the end are the minimal reproductions of real findings (docs/audit/round2/G-random-scenarios.md).
"""

from __future__ import annotations

import copy
import json
import math
import os
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time as dtime
from types import SimpleNamespace

import pytest
from conftest import MARKET_MORNING, kotak_payload, signup

import kotak_sim
import app.broker.manager as manager_mod
import app.models as models_mod
import app.services.calendar as calendar_mod
from app.broker import kotak
from app.broker.manager import broker_manager
from app.config import IST
from app.db import clear_all, session_scope
from app.models import ExecutionRun, OrderLog
from app.routers import auth as auth_router
from app.services import contracts, trading
from kotak_sim import NO_FAULT, PLACED_BUT, KotakSim, SimAccount, _Pos, price_of

_REAL_DT = datetime
GATEWAY = {"message": "Service Unavailable"}
FINISHED = ("complete", "rejected", "cancelled")
UCCS = ["AB101", "CD202", "EF303"]
NAMES = ["Kanha", "Mummy Ji", "Papa Ji"]
DAYS = [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)]  # Wed, Thu, Fri, Mon
TIMES = ["09:20", "09:45", "10:30", "11:15", "12:00", "13:00", "14:00", "14:20", "14:31", "14:50", "15:10",
         "15:40", "18:00", "21:00", "23:10"]
DEFAULT_SEEDS = 30
_HLOCK = threading.Lock()


class RunKilled(BaseException):
    """The server process dies here: nothing after this point is written or sent (BaseException, so no
    `except Exception` in TradeLab's code can swallow it)."""


KILLED = threading.Event()
STATS: dict[str, int] = {}

# Basket choices. Contracts are looked up by query in the sample list; the key of a choice is its query.
FNO_CHOICES = [("nifty fut", "B"), ("nifty 25000 pe", "S"), ("nifty 25100 ce", "B"), ("banknifty fut", "S"),
               ("sensex 80000 pe", "S"), ("sensex 80000 ce", "B")]  # (the Sensex weeklies expire on Thursday 15 Oct)
COM_CHOICES = [("goldm", "B"), ("goldm", "S"), ("silverm", "B"), ("crudeoilm", "B")]  # (crude expires Monday 19 Oct)
ETFS = [("ITBEES", "ITBEES-EQ"), ("GOLDBEES", "GOLDBEES-EQ")]


# ===================================================================================== the simulated Kotak


@dataclass
class FuzzAccount(SimAccount):
    """SimAccount plus an ownership oracle, later fills of working orders, the T+1 gap and call hooks."""

    own: dict = field(default_factory=dict)  # (segment, symbol, product) -> [TradeLab units, holder units] (signed)
    pend: dict = field(default_factory=dict)  # CNC key -> TradeLab units bought earlier, not in the demat yet
    arrivals: list = field(default_factory=list)  # [when, key, units]
    placed: dict = field(default_factory=dict)  # tag -> orders that reached Kotak under it
    events: list = field(default_factory=list)
    hooks: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    touched: set = field(default_factory=set)  # working holder orders a TradeLab order filled beside
    pos_lag: float = 0.0  # seconds Kotak's positions show a fill late (its order book shows it at once)
    book_lag: int = 0  # reads of the order book an order placed from now on stays out of
    snaps: list = field(default_factory=list)  # [(monotonic time, positions)] after every change
    hidden: dict = field(default_factory=dict)  # order id -> reads of the book it is still missing from
    bought_on: dict = field(default_factory=dict)  # CNC key -> last day TradeLab bought it (scenario clock)
    settled_on: dict = field(default_factory=dict)  # CNC key -> days TradeLab's units reached the demat
    ambiguous: set = field(default_factory=set)  # keys where the holder traded TradeLab's units away and then opened her own
    took: dict = field(default_factory=dict)  # key -> side of TradeLab units a holder's order took
    jitter: float = 0.0  # max seconds a call to Kotak may take, at random: shakes up the order threads interleave in
    no_echo: set = field(default_factory=set)  # segments whose order book doesn't echo TradeLab's tags (GuiOrdId)
    tainted: set = field(default_factory=set)  # delivery keys sold from the demat and bought on the same day
    pend_tl: dict = field(default_factory=dict)  # CNC key -> TradeLab's part of `pend` (the rest is the holder's own buys)
    tl_today: dict = field(default_factory=dict)  # CNC key -> TradeLab units bought today (in positions, not the demat)
    down: set = field(default_factory=set)  # methods Kotak keeps failing until the next trading day (round 5)
    flat_nodata: bool = False  # positions leave out rows of closed contracts: an all-flat account gets "No Data"
    tnow: object = field(default=time.time)
    today: object = field(default=lambda: date.min)  # the scenario's clock (seconds)
    kind_of: object = field(default=lambda tag: None)  # tag -> "entry" / "exit" (what run sent the order)

    # -- hooks: something happens at a chosen moment inside a TradeLab run --------------------------
    def _hook(self, method: str, when: str) -> None:
        if KILLED.is_set():
            raise RunKilled()
        if self.jitter and when == "before":
            time.sleep(random.random() * self.jitter)
        with _HLOCK:
            if when == "before":
                self.counts[method] = self.counts.get(method, 0) + 1
            n = self.counts.get(method, 0)
            due = [h for h in self.hooks if not h["done"] and h["m"] == method and h["w"] == when and h["nth"] <= n]
            for h in due:
                h["done"] = True
        for h in due:
            if h["fn"](self) is False:  # not the moment after all: the hook stays for the next call
                h["done"] = False

    def positions(self, **kw):
        self._hook("positions", "before")
        if self.pos_lag > 0 and self.snaps:
            self.calls.append(("positions", kw))
            f = self._fault("positions")
            if f is not NO_FAULT:
                return f
            horizon = self.tnow() - self.pos_lag
            with self.lock:
                old = [sn for t, sn in self.snaps if t <= horizon]
                shown = old[-1] if old else self.snaps[0][1]
                rows = [self._row(p) for p in shown.values()]
            out = {"stat": "ok", "stCode": 200, "data": rows} if rows else dict(kotak_sim.NO_DATA)
        else:
            out = super().positions(**kw)
        if self.flat_nodata and isinstance(out, dict) and isinstance(out.get("data"), list):
            rows = [r for r in out["data"] if not (r["prod"] != "CNC" and int(r["cfBuyQty"]) + int(r["flBuyQty"])
                                                   == int(r["cfSellQty"]) + int(r["flSellQty"]))]
            if len(rows) != len(out["data"]):
                STATS["flat_rows_hidden"] = STATS.get("flat_rows_hidden", 0) + 1
            out = dict(out, data=rows) if rows else dict(kotak_sim.NO_DATA)
            if not rows:
                STATS["no_data_positions"] = STATS.get("no_data_positions", 0) + 1
        self._hook("positions", "after")
        return out

    @staticmethod
    def _row(p) -> dict:
        return {"exSeg": p.seg, "trdSym": p.ts, "prod": p.prod, "cfBuyQty": str(p.cf_buy), "cfSellQty": str(p.cf_sell),
                "flBuyQty": str(p.buy), "flSellQty": str(p.sell), "buyAmt": f"{p.buy_amt:.2f}",
                "sellAmt": f"{p.sell_amt:.2f}", "cfBuyAmt": "0.00", "cfSellAmt": "0.00", "multiplier": "1",
                "genNum": "1", "genDen": "1", "prcNum": "1", "prcDen": "1", "ltp": f"{price_of(p.ts):.2f}", "tok": "1"}

    def _snapshot(self) -> None:
        self.snaps.append((self.tnow(), copy.deepcopy(self.open_positions)))
        del self.snaps[:-40]

    def holdings(self, **kw):
        self._hook("holdings", "before")
        out = super().holdings(**kw)
        self._hook("holdings", "after")
        return out

    def order_report(self, **kw):
        self._hook("order_report", "before")
        if "order_report" in self.down:
            self.calls.append(("order_report", kw))
            return dict(GATEWAY)
        out = super().order_report(**kw)
        if self.hidden and isinstance(out, dict) and isinstance(out.get("data"), list):
            rows = [r for r in out["data"] if self.hidden.get(r["nOrdNo"], 0) <= 0]
            for k in list(self.hidden):
                self.hidden[k] -= 1
            out = dict(out, data=rows) if rows else dict(kotak_sim.NO_DATA)
        self._hook("order_report", "after")
        return out

    def limits(self, **kw):
        self._hook("limits", "before")
        return super().limits(**kw)

    def quotes(self, instrument_tokens, quote_type="ltp", **kw):
        self._hook("quotes", "before")
        return super().quotes(instrument_tokens, quote_type, **kw)

    def margin_required(self, *a, **kw):
        self._hook("margin_required", "before")
        return super().margin_required(*a, **kw)

    def totp_login(self, *a, **kw):
        self._hook("totp_login", "before")
        return super().totp_login(*a, **kw)

    def totp_validate(self, *a, **kw):
        self._hook("totp_validate", "before")
        return super().totp_validate(*a, **kw)

    def cancel_order(self, order_id, amo="NO", isVerify=False, **kw):
        self._hook("cancel_order", "before")
        with self.lock:
            row = next((o for o in self.orders if o["nOrdNo"] == order_id), None)
            if row is not None and row["ordSt"] not in FINISHED and row["prod"] != "MIS" \
                    and (row.get("_tag") or "")[:2] not in ("TL", "SQ"):
                # (only TradeLab cancels in this simulation) a holder's delivery / carry-forward order
                self.events.append({"kind": "CANCELLED_HOLDERS_ORDER", "tag": row.get("_tag"), "n": row["qty"],
                                    "key": (row["exSeg"], row["trdSym"], row["prod"]), "side": row["trnsTp"],
                                    "prod": row["prod"], "oid": order_id, "units": row["qty"]})
        out = super().cancel_order(order_id, amo, isVerify, **kw)
        self._hook("cancel_order", "after")
        return out

    # -- physical effects ---------------------------------------------------------------------------
    def _apply_fill(self, seg: str, ts: str, prod: str, side: str, n: int, px: float) -> None:
        """What a fill of n units does to positions, cash and the demat (the same rules as SimAccount.place_order)."""
        p = self.open_positions.setdefault((seg, ts, prod), _Pos(seg, ts, prod))
        if side == "B":
            p.buy += n
            p.buy_amt += n * px
            if prod == "CNC":
                self.cash -= n * px
        else:
            p.sell += n
            p.sell_amt += n * px
            if prod == "CNC":
                self.cash += n * px
                h = self.demat.get(ts)
                if h and p.sell - p.taken > p.buy:
                    take = min(h[1], p.sell - p.taken - p.buy)
                    h[0] -= take
                    h[1] -= take
                    p.taken += take
        if prod == "CNC" and p.taken > 0 and p.buy > 0:
            # HANDOFF's open question: if Kotak's holdings quantity drops when units are sold from the demat, today's
            # positions net (buy - sell) counts that sale twice, and TradeLab forgets units. Not judged here.
            self.tainted.add((seg, ts, prod))
        self._snapshot()

    @staticmethod
    def lot_of(seg: str, ts: str) -> int:
        if seg not in ("nse_fo", "bse_fo", "mcx_fo"):
            return 1
        c = contracts.contract_master.get(seg, ts)
        return (c.lot_size if c is not None and c.lot_size else 1) or 1

    def tl_sellable_in_demat(self, key) -> int:
        """TradeLab's units that sit in the demat (not today's buys, not the T+1 gap)."""
        return max(self.tl_sellable(key) - self.tl_today.get(key, 0), 0)

    def cnc_available(self, seg: str, ts: str) -> int:
        """Units a delivery sale of this symbol can take right now: today's buys not sold yet plus the sellable demat."""
        p = self.open_positions.get((seg, ts, "CNC"))
        h = self.demat.get(ts)
        return (max(p.buy - p.sell + p.taken, 0) if p else 0) + (h[1] if h else 0)

    def phys_net(self, key) -> int:
        p = self.open_positions.get(key)
        return p.net if p else 0

    def phys_total(self, key) -> int:
        """Units physically on the account on this key, the T+1 gap included."""
        seg, ts, prod = key
        if prod != "CNC":
            return self.phys_net(key)
        p = self.open_positions.get(key)
        h = self.demat.get(ts)
        return (h[0] if h else 0) + ((p.buy - p.sell + p.taken) if p else 0) + self.pend.get(key, 0)

    def tl_sellable(self, key) -> int:
        """TradeLab's delivery units that are on the account and not in the T+1 gap (the part of the pool it can sell)."""
        return max(max(self.own.get(key, [0, 0])[0], 0) - self.pend_tl.get(key, 0), 0)

    def her_sellable(self, key) -> int:
        """The holder's own delivery units a sale can take now: the pool's sellable units beyond TradeLab's. Her pledged
        units and the ones in the T+1 gap are not in it."""
        seg, ts, prod = key
        if prod != "CNC":
            return abs(self.own.get(key, [0, 0])[1])
        return max(self.cnc_available(seg, ts) - self.tl_sellable(key), 0)

    # -- the oracle ---------------------------------------------------------------------------------
    def _attribute(self, tag: str | None, key, side: str, n: int, pre_net: int, oid: str, hs: int | None = None) -> None:
        if n <= 0:
            return
        s = 1 if side == "B" else -1
        prod = key[2]
        own = self.own.setdefault(key, [0, 0])
        tl, h = own
        is_tl = bool(tag) and tag[:2] in ("TL", "SQ")
        ev = {"tag": tag, "key": key, "side": side, "n": n, "prod": prod, "oid": oid}
        if is_tl:
            # Units still in the T+1 gap are not on the account yet: a sale can only take the holder's (since 2969ce6 the
            # Holding Sell leaves them alone; before it, the arriving units replaced them, so final holdings were equal).
            avail = abs(tl) - (self.pend_tl.get(key, 0) if prod == "CNC" else 0)
            closing = self.kind_of(tag) == "exit"
            if not closing:
                if prod == "CNC" and s > 0:
                    self.bought_on[key] = self.today()
                    self.tl_today[key] = self.tl_today.get(key, 0) + n
                if h != 0 and (h > 0) != (s > 0):
                    self.events.append({**ev, "kind": "OPEN_AGAINST_HOLDER", "units": min(n, abs(h))})
                own[0] += s * n
            else:
                close = min(n, max(avail, 0)) if tl * s < 0 else 0
                own[0] += s * close
                if prod == "CNC" and close:
                    self.tl_today[key] = max(self.tl_today.get(key, 0) - close, 0)
                rest = n - close
                if rest:
                    take = min(rest, abs(h)) if h * s < 0 else 0
                    if take:
                        own[1] += s * take
                        rest -= take
                        self.events.append({**ev, "kind": "HOLDER_UNITS", "units": take})
                    if rest:
                        own[0] += s * rest
                        self.events.append({**ev, "kind": "OVERSOLD", "units": rest})
            # Any working holder order on this key (a stop-loss) now sits beside a TradeLab fill.
            for o in self.orders:
                if o["nOrdNo"] != oid and (o["exSeg"], o["trdSym"], o["prod"]) == key and o["ordSt"] not in FINISHED \
                        and not (o.get("_tag") or "")[:2] in ("TL", "SQ"):
                    self.touched.add(o["nOrdNo"])
        else:
            if h * s > 0:
                own[1] += s * n
            else:
                rest = n
                if h != 0:
                    # (a delivery sale takes only her free units in the demat or bought today: not the pledged ones, not
                    # the ones in the T+1 gap)
                    c = min(rest, abs(h) if hs is None else hs)
                    own[1] += s * c
                    rest -= c
                if rest and tl != 0 and (tl > 0) != (s > 0):
                    avail = abs(tl) - (self.pend_tl.get(key, 0) if prod == "CNC" else 0)
                    c = min(rest, max(avail, 0))
                    own[0] += s * c
                    rest -= c
                if rest:
                    own[1] += s * rest
            if own[0] != tl and tl * (own[0] - tl) < 0:
                self.took[key] = 1 if tl > 0 else -1
            if key in self.took and own[1] * self.took[key] > 0:
                self.ambiguous.add(key)  # Kotak's net can't tell TradeLab's units from hers any more
        if prod != "CNC":
            post = self.phys_net(key)
            if pre_net * post < 0:
                self.events.append({**ev, "kind": "FLIP", "tl": is_tl, "pre": pre_net, "post": post})

    # -- Kotak's side -------------------------------------------------------------------------------
    def place_order(self, exchange_segment, product, price, order_type, quantity, validity, trading_symbol,
                    transaction_type, amo="NO", tag=None, **kw) -> dict:
        self._hook("place_order", "before")
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
        key = (exchange_segment, trading_symbol, product)
        oid = str(next(self._ids))
        reply = lost_reply or {"stat": "Ok", "nOrdNo": oid, "stCode": 200}
        with self.lock:
            if tag:
                self.placed[tag] = self.placed.get(tag, 0) + 1
            row = {"nOrdNo": oid, "ordSt": "complete", "trdSym": trading_symbol, "exSeg": exchange_segment,
                   "prod": product, "trnsTp": side, "qty": qty, "fldQty": qty, "avgPrc": f"{px:.2f}",
                   "prcTp": order_type, "rejRsn": "--", "GuiOrdId": "" if exchange_segment in self.no_echo else (tag or ""),
                   "_tag": tag or "", "ordDtTm": "22-Oct-2026 10:00:00"}
            if product == "CNC" and side == "S" and qty > self.cnc_available(exchange_segment, trading_symbol):
                row.update(ordSt="rejected", fldQty=0, avgPrc="0.00", rejRsn="RMS:Insufficient holding: Check T1 holding")
                self.orders.append(row)
                return reply
            if self.reject_in_book and self.partial_next is None:
                row.update(ordSt="rejected", fldQty=0, avgPrc="0.00", rejRsn=self.reject_in_book)
                self.reject_in_book = None
                self.orders.append(row)
                return reply
            if self.partial_next is not None:
                lot = self.lot_of(exchange_segment, trading_symbol)
                # (an exchange fills whole lots of a contract: 0, 1 or 2 lots here)
                part = self.partial_next if lot == 1 else (self.partial_next % 3) * lot
                if part < qty:  # (an order that fills whole is simply complete)
                    row.update(ordSt="open", fldQty=part)
                qty = min(qty, part)
                if row["ordSt"] == "complete":
                    row["fldQty"] = qty
                self.partial_next = None
                if self.reject_in_book:
                    if row["ordSt"] == "open":
                        row.update(ordSt="rejected", rejRsn=self.reject_in_book)
                    self.reject_in_book = None
            pre = self.phys_net(key)
            hs = self.her_sellable(key) if (product == "CNC" and side == "S") else None
            if tag and tag[:2] in ("TL", "SQ") and exchange_segment in ("nse_fo", "bse_fo", "mcx_fo"):
                c = contracts.contract_master.get(exchange_segment, trading_symbol)
                if c is not None and c.lot_size and int(quantity) % c.lot_size:
                    self.events.append({"kind": "NON_LOT_QUANTITY", "tag": tag, "key": key, "side": side, "n": int(quantity),
                                        "prod": product, "oid": oid, "units": c.lot_size})
            if qty > 0:
                self._apply_fill(exchange_segment, trading_symbol, product, side, qty, px)
            self.orders.append(row)
            if self.book_lag:
                self.hidden[oid] = self.book_lag
            self._attribute(tag, key, side, qty, pre, oid, hs)
        self._hook("place_order", "after")
        return reply

    def fill_working(self, row: dict, n: int) -> int:
        """Kotak fills up to n more units of a working order (a part-filled order, a stop-loss that triggers)."""
        with self.lock:
            if row["ordSt"] in FINISHED:
                return 0
            n = min(n, row["qty"] - row["fldQty"])
            lot = self.lot_of(row["exSeg"], row["trdSym"])
            if n < row["qty"] - row["fldQty"]:
                n = n // lot * lot
            if n <= 0:
                return 0
            key = (row["exSeg"], row["trdSym"], row["prod"])
            if row["prod"] == "CNC" and row["trnsTp"] == "S" and n > self.cnc_available(row["exSeg"], row["trdSym"]):
                return 0
            pre = self.phys_net(key)
            hs = self.her_sellable(key) if (key[2] == "CNC" and row["trnsTp"] == "S") else None
            row["fldQty"] += n
            if row["fldQty"] >= row["qty"]:
                row["ordSt"] = "complete"
            row["avgPrc"] = f"{price_of(row['trdSym']):.2f}"
            self._apply_fill(key[0], key[1], key[2], row["trnsTp"], n, price_of(row["trdSym"]))
            self._attribute(row.get("_tag") or None, key, row["trnsTp"], n, pre, row["nOrdNo"], hs)
            return n

    # -- the passing of days --------------------------------------------------------------------------
    def rollover(self, arrive_at: datetime | None, new_day: date | None = None) -> None:
        """A new trading day: intraday positions and the books are cleared, carry-forward positions carry, delivery
        buys leave the positions and reach the demat only at `arrive_at` (the T+1 gap)."""
        with self.lock:
            carried = {}
            for k, p in self.open_positions.items():
                if p.prod == "CNC":
                    net = p.buy - p.sell + p.taken
                    if net > 0 and arrive_at is not None:
                        mine = min(net, self.tl_today.get(k, 0), max(self.own.get(k, [0, 0])[0], 0))
                        self.arrivals.append([arrive_at, k, net, mine])
                        self.pend[k] = self.pend.get(k, 0) + net
                        self.pend_tl[k] = self.pend_tl.get(k, 0) + mine
                elif p.prod == "NRML" and p.net:
                    c = contracts.contract_master.get(p.seg, p.ts)
                    if new_day is not None and c is not None and c.expiry and c.expiry < new_day:
                        self.own.pop(k, None)  # expired: the exchange settled it in cash overnight
                        continue
                    carried[k] = _Pos(p.seg, p.ts, p.prod, cf_buy=max(p.net, 0), cf_sell=max(-p.net, 0))
            self.open_positions = carried
            self.orders = []
            self.tl_today.clear()
            self.down.clear()  # (Kotak is back by the next trading day)
            for k in [k for k in self.own if k[2] == "MIS"]:
                del self.own[k]
            self.touched.clear()
            self.hidden.clear()
            self.snaps.clear()
            self._snapshot()

    def deliver(self, now: datetime) -> None:
        with self.lock:
            for a in [a for a in self.arrivals if a[0] <= now]:
                self.arrivals.remove(a)
                _, key, n, mine = a
                h = self.demat.setdefault(key[1], [0, 0, 100.0])
                h[0] += n
                h[1] += n
                self.pend[key] = self.pend.get(key, 0) - n
                self.pend_tl[key] = self.pend_tl.get(key, 0) - mine
                if self.pend_tl[key] <= 0:
                    del self.pend_tl[key]
                self.settled_on.setdefault(key, set()).add(now.date())
                if self.pend[key] <= 0:
                    del self.pend[key]

    def close_markets(self, t: dtime) -> None:
        """Day orders still working when their exchange closes end there (NSE 15:30, MCX 23:30)."""
        with self.lock:
            for o in self.orders:
                if o["ordSt"] in ("open", "trigger pending"):
                    seg = o["exSeg"]
                    if (seg == "mcx_fo" and t >= dtime(23, 30)) or (seg != "mcx_fo" and t >= dtime(15, 30)):
                        o["ordSt"] = "cancelled"

    def reset_faults(self) -> None:
        self.faults.clear()
        self.hide_positions.clear()
        self.pos_lag = 0.0
        self.book_lag = 0
        self.hidden.clear()
        self.cancel_lag = 0
        self.reject_in_book = None
        self.partial_next = None
        self.hooks.clear()


class FuzzSim(KotakSim):
    def account(self, ucc: str, **kw) -> FuzzAccount:
        ucc = ucc.upper()
        if ucc not in self.accounts:
            self.accounts[ucc] = FuzzAccount(ucc, **kw)
        else:
            for k, v in kw.items():
                setattr(self.accounts[ucc], k, v)
        return self.accounts[ucc]


# ===================================================================================== the scenarios


def gen_spec(seed: int) -> dict:
    """Everything a seed decides, as plain data (so a failing scenario can be shrunk and replayed)."""
    r = random.Random(seed)
    n_acc = r.choice([1, 2, 2, 3])
    profile = r.choice(["broad", "broad", "holding", "derivs", "intraday", "hedge", "race"])
    sections = {s for s in ("intraday", "holding", "fno", "commodity") if r.random() < 0.6} or {r.choice(
        ["intraday", "holding", "fno", "commodity"])}
    if profile == "holding":
        sections = {"holding"}
    elif profile == "derivs":
        sections = {"fno", "commodity"} if r.random() < 0.5 else {r.choice(["fno", "commodity"])}
    elif profile == "intraday":
        sections = {"intraday"}
    elif profile == "hedge":
        sections = {"fno"}
    elif profile == "race":
        sections = {s for s in ("intraday", "holding", "fno", "commodity") if r.random() < 0.7} or {"fno", "commodity"}
    items = {
        "intraday": [("SBIN", "B")] + ([("INFY", "S")] if r.random() < 0.25 else []),
        "holding": [e for e in ETFS if r.random() < 0.6] or [ETFS[0]],
        "fno": r.sample(FNO_CHOICES, r.randint(1, 2)),
        "commodity": [r.choice(COM_CHOICES)],
    }
    if profile == "hedge":  # a bought call against a written put of the same underlying (a hedged pair)
        items["fno"] = [("nifty 25000 pe", "S"), ("nifty 25100 ce", "B")] if r.random() < 0.7 else \
            [("nifty fut", "B"), ("nifty 25000 pe", "S")]
    if len({q for q, _ in items["fno"]}) < len(items["fno"]):
        items["fno"] = items["fno"][:1]
    accounts = []
    for _ in range(n_acc):
        cash = [300.0, 5000.0, 25000.0, 25000.0, 168000.0]
        if profile in ("hedge", "derivs", "race"):  # (selling first needs real margin)
            cash = [25000.0, 168000.0, 168000.0, 168000.0]
        a = {"cash": r.choice(cash), "demat": {}, "mis": 0, "nrml": {}}
        if r.random() < (0.9 if profile == "holding" else 0.55):
            q = r.choice([3, 40, 1801])
            a["demat"]["ITBEES-EQ"] = [q, q if r.random() < 0.8 else q // 2, 30.0]
        if r.random() < (0.6 if profile == "holding" else 0.25):
            q = r.choice([10, 901])
            a["demat"]["GOLDBEES-EQ"] = [q, q, 60.0]
        if r.random() < (0.75 if profile == "intraday" else 0.3):
            a["mis"] = r.choice([-20, 15, 40])
        for sec in ("fno", "commodity"):
            for q, _ in items[sec]:
                if r.random() < (0.6 if profile in ("derivs", "hedge") else 0.3):
                    a["nrml"][q] = r.choice([-2, -1, 1, 2])
        accounts.append(a)
    days = r.choice([2, 3, 3]) if profile == "holding" else r.choice([1, 1, 2, 2, 3, 3, 4])
    steps: list[dict] = []
    clearing_day = 1 if r.random() < 0.15 else None
    open_: set = set()
    for d in range(days):
        open_.discard("intraday")
        times = sorted(r.choice(TIMES) for _ in range(r.randint(5, 13)))
        today = [gen_step(r, d, t, sections, n_acc, open_, profile) for t in times]
        if "intraday" in sections and r.random() < 0.3:
            today.insert(0, {"op": "ibs", "day": d, "t": "09:00", "acct": 0, "u": r.random(), "u2": r.random()})
        if d < days - 1:
            # (The scheduler runs both true-ups every trading day; HANDOFF accepts the days it doesn't.)
            today += [{"op": "quiet" if r.random() < 0.5 else "trueup", "day": d, "t": "15:40"},
                      {"op": "quiet" if r.random() < 0.5 else "trueup", "day": d, "t": "23:58"}]
            today.sort(key=lambda x: x["t"])
        if profile == "hedge":  # make the first order of a hedged pair fail or fill in part, often
            extra = []
            for st in today:
                if st["op"] in ("buy", "sell") and r.random() < 0.6:
                    extra.append({"op": "toggle", "day": d, "t": st["t"], "acct": r.randrange(n_acc), "u": r.random(),
                                  "u2": r.random(), "what": r.choice(["reject", "partial", "partial_reject", "cancel_lag"])})
            today = sorted(today + extra, key=lambda x: x["t"]) if not extra else \
                sorted([*extra, *today], key=lambda x: (x["t"], 0 if x in extra else 1))
        steps += today
        if d < days - 1:
            steps.append({"op": "overnight", "day": d + 1, "t": f"0{r.randint(1, 8)}:{r.choice(['05', '30', '55'])}",
                          "u": r.random()})
            if r.random() < 0.3:
                steps.append({"op": "trueup", "day": d + 1, "t": "08:58"})
    steps.append({"op": "quiet", "day": days - 1, "t": "23:20"})
    no_echo = ["mcx_fo"] if r.random() < 0.25 else (["nse_fo"] if r.random() < 0.05 else [])
    if not os.environ.get("TL_FUZZ_NOECHO"):  # finding G-2: a book without tag echo leaves unknown buys unresolved
        no_echo = []
    spec = {"seed": seed, "profile": profile, "no_echo": no_echo, "jitter": 0.02 if profile == "race" else 0.0, "sections": sorted(sections), "items": items, "accounts": accounts, "steps": steps,
            "max_lots": r.choice([1, 2, 3, 3, 30, 60]), "clearing_day": clearing_day, "clearing_list": r.random() < 0.85}
    add_round5_events(spec)
    return spec


def add_round5_events(spec: dict) -> None:
    """Round 5: pledges, delivery buys by the holder, "No Data" positions on flat accounts, and order books that stay
    unreadable until the next day (old unknown entries and closes). Drawn from a stream of their own, so the rest of a
    seed's scenario is what it was in round 4."""
    r = random.Random(spec["seed"] * 7919 + 17)
    secs = set(spec["sections"])
    n_acc = len(spec["accounts"])
    for a in spec["accounts"]:
        a["flat_nodata"] = r.random() < (0.5 if spec["profile"] in ("derivs", "hedge") else 0.3)
    days = max(st["day"] for st in spec["steps"]) + 1
    new = []
    for d in range(days):
        for _ in range(r.choice([0, 1, 1, 2, 3])):
            t = r.choice([x for x in TIMES if "09:20" <= x <= "15:10"])
            base = {"day": d, "t": t, "acct": r.randrange(n_acc), "u": r.random(), "u2": r.random()}
            kinds = ["blackout"] * (3 if d < days - 1 else 1)
            if "holding" in secs:
                kinds += ["pledge", "pledge", "release", "holderbuy"]
            k = r.choice(kinds)
            if k == "blackout":
                lost = r.choice(["placed", "placed", "gateway", None])
                new.append({**base, "op": "blackout", "lost": lost, "n": r.randint(1, 3)})
                if lost:  # an order goes out while the book is unreadable and its reply is lost: no known outcome
                    sec = r.choice(sorted(secs - {"intraday"})) if secs - {"intraday"} else None
                    if sec:
                        new.append({**base, "op": r.choice(["buy", "sell"]), "sec": sec, "sub": False, "opened": None,
                                    "acct": base["acct"]})
                    elif "intraday" in secs and t <= "14:20":
                        new.append({**base, "op": "ib"})
            elif k == "holderbuy":
                new.append({**base, "op": "holder", "act": "buy"})
            else:
                new.append({**base, "op": "pledge", "act": "pledge" if k == "pledge" else "release"})
    if any(n["op"] == "blackout" for n in new):  # on the last day Kotak's book is back before the final check
        new.append({"op": "heal", "day": days - 1, "t": "23:10"})
    spec["steps"] = sorted(spec["steps"] + new, key=lambda x: (x["day"], x["t"]))


def gen_sub(r: random.Random, day: int, t: str, ops: list, n_acc: int) -> dict:
    op = r.choice(ops)
    st = {"op": op, "day": day, "t": t, "acct": r.randrange(n_acc), "u": r.random(), "u2": r.random()}
    if ":" in op:
        st["op"], st["sec"] = op.split(":")
        st["sub"] = r.random() < 0.3
        if st["op"] == "sell":
            st["opened"] = r.choice([None, None, "B", "S"])
    if st["op"] == "isq":
        st["how"] = r.choice(["api", "sched", "catchup"])
    return st


def gen_step(r: random.Random, day: int, t: str, sections: set, n_acc: int, open_: set | None = None,
             profile: str = "broad") -> dict:
    hh, mm = int(t[:2]), int(t[3:])
    minutes = hh * 60 + mm
    ops = []

    def add(op, w):
        ops.append((op, w))

    open_ = open_ if open_ is not None else set()  # sections the generated steps have (probably) left positions in
    if "intraday" in sections and 9 * 60 + 15 <= minutes <= 14 * 60 + 20:
        add("ib", 7 if "intraday" not in open_ else 1)
    add("isq", 30 if "intraday" in open_ else 1)
    add("panic", 14 if open_ else 1)
    for sec, (lo, hi) in (("holding", (555, 915)), ("fno", (555, 925)), ("commodity", (540, 1405))):
        if sec in sections and lo <= minutes < hi:
            add("buy:" + sec, 6 if sec not in open_ else 2)
            add("sell:" + sec, 60 if sec in open_ else 1)
    add("holder", 16 if profile in ("holding", "derivs", "intraday") else (3 if profile == "race" else 9))
    add("sl", 5 if profile in ("derivs", "intraday", "hedge") else 2)
    add("slfire", 2)
    add("fill", 3)
    add("fault", 7)
    add("toggle", 6)
    add("inject", 5)
    add("trueup", 1)
    add("quiet", 3)
    add("restart", 2)
    add("kill", 3)
    add("par", 22 if profile == "race" else 3)
    add("autopilot", 2)
    add("remove", 1)
    total = sum(w for _, w in ops)
    x = r.random() * total
    op = ops[-1][0]
    for o, w in ops:
        x -= w
        if x < 0:
            op = o
            break
    st = {"op": op, "day": day, "t": t, "acct": r.randrange(n_acc), "u": r.random(), "u2": r.random()}
    if op == "ib":
        open_.add("intraday")
    elif op == "isq":
        open_.discard("intraday")
    elif op == "panic":
        open_ &= {"holding"}
    elif ":" in op:
        (open_.add if op.startswith("buy") else open_.discard)(op.split(":")[1])
    if ":" in op:
        st["op"], st["sec"] = op.split(":")
        st["sub"] = r.random() < 0.3
        if st["op"] == "sell":
            st["opened"] = r.choice([None, None, None, "B", "S"])
    if st["op"] == "par":
        subs = [o for o, _ in ops if o.split(":")[0] in ("buy", "sell", "panic", "isq", "ib")] + ["trueup", "trueup"]
        st["subs"] = [gen_sub(r, day, t, subs, n_acc) for _ in range(2)]
    if st["op"] == "autopilot":
        st["on"] = r.random() < 0.4
    if st["op"] == "remove":
        st["sec"] = r.choice(sorted(sections))
    if st["op"] == "isq":
        st["how"] = r.choice(["api", "api", "sched", "catchup"])
    if st["op"] == "holder":
        st["act"] = r.choice(["sell", "sell", "buy", "all", "takeover", "takeover"])
    if st["op"] == "fault":
        st["m"] = r.choice(["place_order", "place_order", "order_report", "order_report", "positions", "holdings",
                            "limits", "cancel_order", "margin_required", "quotes", "totp_login"])
        st["kind"] = r.choice(["lost", "lost", "gateway", "http500", "1003", "refuse", "timeout"])
        st["n"] = r.randint(1, 8)
        st["skip"] = r.randint(0, 2)
    if st["op"] == "toggle":
        st["what"] = r.choice(["poslag", "poslag", "booklag", "partial", "partial", "reject", "partial_reject",
                               "cancel_lag", "expire"])
    if st["op"] == "kill":
        st["m"] = r.choice(["place_order", "place_order", "positions", "order_report", "cancel_order", "holdings"])
        st["when"] = r.choice(["before", "after"])
        st["nth"] = r.randint(1, 5)
    if st["op"] == "inject":
        st["m"] = r.choice(["positions", "positions", "order_report", "place_order", "holdings"])
        st["when"] = r.choice(["before", "after"])
        st["nth"] = r.randint(1, 4)
        st["do"] = {"op": "holder", "acct": st["acct"], "u": r.random(), "u2": r.random(),
                    "act": r.choice(["sell", "sell", "buy", "all", "fill", "takeover"]), "day": day, "t": t}
    return st


# ===================================================================================== the world


class Violation(Exception):
    pass


def fake_datetime(clock):
    class FakeDT(_REAL_DT):
        @classmethod
        def now(cls, tz=None):
            n = clock.now()
            return n.astimezone(tz) if tz is not None else n.replace(tzinfo=None)

    return FakeDT


def patch_clock(monkeypatch, clock):
    fake = fake_datetime(clock)
    for mod in (models_mod, trading, manager_mod, calendar_mod):
        monkeypatch.setattr(mod, "datetime", fake)


def guard_kill(monkeypatch) -> None:
    """After the simulated process death (KILLED) TradeLab's own writers do nothing, like a dead process."""
    for name in ("_finish_run", "_finish_session", "_settle_logs", "_write_logs", "_reserve_logs", "reconcile_orders"):
        orig = getattr(trading, name)

        def wrapper(*a, _o=orig, _n=name, **kw):
            if KILLED.is_set():
                if _n == "_reserve_logs":
                    raise RunKilled()
                return 0 if _n == "reconcile_orders" else None
            return _o(*a, **kw)

        monkeypatch.setattr(trading, name, wrapper)
    previous = threading.excepthook
    monkeypatch.setattr(threading, "excepthook", lambda args: None if issubclass(args.exc_type, RunKilled)
                        else previous(args))


def reset_state(clock) -> None:
    KILLED.clear()
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.name.startswith(("entry-", "exit-")):
            t.join(15)
    clear_all()
    broker_manager._entries.clear()
    auth_router._attempts.clear()
    trading._squared.clear()
    trading._live.clear()
    trading._confirmations.clear()
    trading._stopped_at.clear()
    kotak_sim.PRICES.clear()
    kotak_sim.TOKEN_PRICES.clear()
    clock.set(MARKET_MORNING)


class World:
    def __init__(self, client, clock, sim: FuzzSim, spec: dict, verbose: bool = False):
        self.client, self.clock, self.sim, self.spec, self.verbose = client, clock, sim, spec, verbose
        self.violations: list[dict] = []
        self.notes: list[str] = []
        self.trace: list[str] = []
        self.step_no = -1
        self.accts: list[FuzzAccount] = []
        self.ids: list[int] = []
        self.keys: dict[str, list[tuple]] = {}  # section -> [(segment, trading symbol, product, lot)]
        self.item_ts: dict[str, str] = {}  # query -> trading symbol
        self.day = 0
        self.hdr: dict = {}
        self.user = 0
        self.last: dict | None = None
        self.seen_tags: dict[int, set] = {}
        self.interfered = False
        self.restart_next = False
        self.restarted = False
        self.par_ran = False
        self.skip_known_races = True  # G-4 / G-5 have xfail tests of their own
        self._kinds: dict[str, str] = {}

    # -- helpers ----------------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{self.step_no}] {msg}"
        self.trace.append(line)
        if self.verbose:
            print(line, flush=True)

    def snap(self) -> str:
        out = []
        for a in self.accts:
            ks = {k: (v, a.pend.get(k, 0)) for k, v in a.own.items() if v != [0, 0] or a.pend.get(k)}
            dm = {k: v[:2] for k, v in a.demat.items()}
            pos = {k[1] + "/" + k[2]: (p.cf_buy - p.cf_sell, p.buy, p.sell, p.taken) for k, p in a.open_positions.items()}
            out.append(f"{a.ucc}: own[tl,holder]/pend={ {k[1] + '/' + k[2]: v for k, v in ks.items()} } demat={dm} pos(cf,buy,sell,taken)={pos}")
        return "\n        ".join(out)

    RACY = {"OVERSOLD", "HOLDER_UNITS", "TL_FLIP", "STOPLOSS_FLIP", "OPEN_AGAINST_HOLDER", "DONE_BUT_OPEN"}

    def violate(self, kind: str, detail: str, **extra) -> None:
        if self.interfered and kind in self.RACY:  # the holder traded in the middle of the run: an inherent race
            self.notes.append(f"[{self.step_no}] race, {kind}: {detail}")
            STATS[f"race:{kind}"] = STATS.get(f"race:{kind}", 0) + 1
            return
        v = {"kind": kind, "detail": detail, "step": self.step_no, **extra}
        self.violations.append(v)
        self.log(f"VIOLATION {kind}: {detail}\n        {self.snap()}")

    def go(self, day: int, t: str) -> None:
        h, m = int(t[:2]), int(t[3:])
        when = datetime(DAYS[day].year, DAYS[day].month, DAYS[day].day, h, m, tzinfo=IST)
        if when > self.clock.now() or self.day != day:
            self.clock.set(when)
        else:  # (steps are never in the same second: a fill that Kotak's positions show late has long been shown)
            self.clock.set(self.clock.now() + timedelta(seconds=5))
        self.day = day
        now = self.clock.now()
        for a in self.accts:
            a.deliver(now)
            a.close_markets(now.time().replace(tzinfo=None))

    def api(self, method: str, url: str, **kw):
        return getattr(self.client, method)(url, headers=self.hdr, **kw)

    def wait_idle(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if KILLED.is_set():
                break
            with session_scope() as db:
                if not db.count(ExecutionRun, {"status": "running"}):
                    break
            time.sleep(0.02)
        else:
            self.violate("HANG", "a run did not finish within the time allowed")
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.name.startswith(("entry-", "exit-")):
                t.join(max(0.0, deadline - time.monotonic()))

    def wait_run(self, run_id: int) -> ExecutionRun:
        self.wait_idle()
        with session_scope() as db:
            run = db.get(ExecutionRun, run_id)
            db.expunge(run)
        return run

    # -- setup ---------------------------------------------------------------------------------------
    def setup(self) -> None:
        spec = self.spec
        self.clock.set(datetime(2026, 10, 14, 9, 0, tzinfo=IST))
        yrs = {2025, 2026, 2027}
        if spec["clearing_list"]:
            days = {DAYS[spec["clearing_day"]]} if spec["clearing_day"] is not None else set()
            calendar_mod.save_clearing_holidays(days, yrs)
        r = signup(self.client, f"fz{spec['seed']}@example.com", "Owner")
        assert r.status_code == 201, r.text
        self.hdr = {"Authorization": f"Bearer {r.json()['token']}"}
        self.user = self.api("get", "/api/auth/me").json()["id"]
        for i, a in enumerate(spec["accounts"]):
            acct = self.sim.account(UCCS[i], cash=a["cash"], demat=copy.deepcopy(a["demat"]))
            acct.kind_of = self.tag_kind
            acct.no_echo = set(spec.get("no_echo", []))
            acct.flat_nodata = bool(a.get("flat_nodata"))
            acct.jitter = spec.get("jitter", 0.0)
            acct.tnow = lambda: self.clock.now().timestamp()
            acct.today = lambda: self.clock.now().date()
            for ts, (q, *_rest) in a["demat"].items():
                acct.own[("nse_cm", ts, "CNC")] = [0, q]
            r = self.api("post", "/api/accounts", json=kotak_payload(name=NAMES[i], ucc=UCCS[i]))
            assert r.status_code == 201, r.text
            self.ids.append(r.json()["id"])
            self.accts.append(acct)
        for sec in spec["sections"]:
            if sec in ("fno", "commodity"):
                self.api("put", f"/api/sections/{sec}", json={"max_lots": spec["max_lots"]})
        keys: dict[str, list] = {}
        if "intraday" in spec["sections"]:
            for sym, side in spec["items"]["intraday"]:
                r = self.api("post", "/api/basket", json={"symbol": sym, "trading_symbol": f"{sym}-EQ", "token": "3045",
                                                          "exchange_segment": "nse_cm", "side": side})
                assert r.status_code == 201, r.text
            keys["intraday"] = [("nse_cm", f"{sym}-EQ", "MIS", 1) for sym, _ in spec["items"]["intraday"]]
        if "holding" in spec["sections"]:
            for sym, ts in spec["items"]["holding"]:
                r = self.api("post", "/api/basket", json={"section": "holding", "symbol": sym, "trading_symbol": ts,
                                                          "exchange_segment": "nse_cm"})
                assert r.status_code == 201, r.text
            keys["holding"] = [("nse_cm", ts, "CNC", 1) for _, ts in spec["items"]["holding"]]
        for sec in ("fno", "commodity"):
            if sec not in spec["sections"]:
                continue
            keys[sec] = []
            for q, side in spec["items"][sec]:
                c = self.api("get", "/api/stocks/contracts", params={"section": sec, "q": q}).json()["results"][0]
                r = self.api("post", "/api/basket", json={
                    "section": sec, "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                    "exchange_segment": c["exchange_segment"], "side": side, "sizing_mode": "lots", "sizing_value": 1})
                assert r.status_code == 201, r.text
                self.item_ts[q] = c["trading_symbol"]
                keys[sec].append((c["exchange_segment"], c["trading_symbol"], "NRML", r.json()["lot_size"]))
        self.keys = keys
        for i, a in enumerate(spec["accounts"]):
            acct = self.accts[i]
            if a["mis"] and "intraday" in spec["sections"]:
                side = "B" if a["mis"] > 0 else "S"
                acct.place_order("nse_cm", "MIS", "0", "MKT", str(abs(a["mis"])), "DAY", "SBIN-EQ", side)
            for q, lots in a["nrml"].items():
                ts = self.item_ts.get(q)
                if ts is None:
                    continue
                seg, _, _, lot = next(k for sec in ("fno", "commodity") for k in keys.get(sec, []) if k[1] == ts)
                key = (seg, ts, "NRML")
                acct.open_positions[key] = _Pos(seg, ts, "NRML", cf_buy=max(lots, 0) * lot, cf_sell=max(-lots, 0) * lot)
                acct.own[key] = [0, lots * lot]
        for acct in self.accts:
            acct._snapshot()
        self.check_physical()

    # -- the oracle's view -------------------------------------------------------------------------------
    def check_physical(self) -> None:
        for a in self.accts:
            for key, (tl, h) in list(a.own.items()):
                phys = a.phys_total(key)
                if tl + h != phys:
                    self.violate("HARNESS_PHYS", f"{a.ucc} {key}: oracle TL {tl} + holder {h} != physical {phys}")
                    a.own[key] = [phys - h, h]  # resync so one slip doesn't repeat
            for k in set(a.open_positions) | {("nse_cm", ts, "CNC") for ts in a.demat}:
                if k not in a.own and a.phys_total(k):
                    self.violate("HARNESS_PHYS", f"{a.ucc} {k}: physical {a.phys_total(k)} with no oracle entry")
                    a.own[k] = [0, a.phys_total(k)]

    def tl_units(self, a: FuzzAccount, key) -> int:
        return a.own.get(key, [0, 0])[0]

    # -- drain events ---------------------------------------------------------------------------------
    def drain(self, exempt_mis_exit: bool = True) -> None:
        for a in self.accts:
            events, a.events = a.events, []
            for ev in events:
                kind, tag = ev["kind"], ev.get("tag") or ""
                if kind == "FLIP":
                    if ev["tl"] and ev["prod"] == "MIS" and self.tag_kind(tag) != "exit":
                        continue  # an Intraday buy nets against the holder's own MIS short: by design
                    if ev["tl"]:
                        self.violate("TL_FLIP", f"{a.ucc} {ev['key'][1]} {ev['prod']}: {ev['tag']} {ev['side']} {ev['n']} "
                                                f"took the net from {ev['pre']} to {ev['post']}", ucc=a.ucc, key=ev["key"])
                    elif ev["oid"] in a.touched:
                        self.violate("STOPLOSS_FLIP", f"{a.ucc} {ev['key'][1]}: a stop-loss TradeLab closed beside filled "
                                                      f"{ev['n']} and flipped the net from {ev['pre']} to {ev['post']}",
                                     ucc=a.ucc, key=ev["key"])
                    continue
                if kind == "HOLDER_UNITS" and ev["prod"] == "MIS" and self.is_exit_tag(a, tag):
                    continue  # the square-off / panic button closes every MIS position in scope, by design
                if kind == "OPEN_AGAINST_HOLDER" and ev["prod"] == "MIS":
                    continue  # Intraday doesn't check the holder's own MIS positions before it buys
                if ev["key"] in a.ambiguous or (ev["key"] in a.tainted and kind in ("HOLDER_UNITS", "OVERSOLD")):
                    # (tainted: HANDOFF's open question about the holdings quantity; TradeLab's figure of her own units
                    # is then wrong, and it can sell a unit of hers while its own are in the T+1 gap -- the units she
                    # sold that day are counted twice in today's positions)
                    STATS[f"ambiguous:{kind}"] = STATS.get(f"ambiguous:{kind}", 0) + 1
                    continue  # her units and TradeLab's cancel out in Kotak's net: nobody can tell them apart
                self.violate(kind, f"{a.ucc} {ev['key'][1]} {ev['prod']}: {tag} {ev['side']} {ev['n']} "
                                   f"({ev.get('units')} units)", ucc=a.ucc, key=ev["key"], tag=tag)
            for tag, cnt in a.placed.items():
                if cnt > 1:
                    self.violate("DUPLICATE_ORDER", f"{a.ucc}: tag {tag} reached Kotak {cnt} times", ucc=a.ucc, tag=tag)
                if tag not in self.seen_tags.setdefault(id(a), set()):
                    self.seen_tags[id(a)].add(tag)
                    if not self.tag_known(a, tag):
                        self.violate("UNKNOWN_TL_ORDER", f"{a.ucc}: order tagged {tag} has no TradeLab record", ucc=a.ucc,
                                     tag=tag)

    def tag_known(self, a: FuzzAccount, tag: str) -> bool:
        if not tag.startswith("TL"):
            return not tag  # untagged = holder's
        try:
            log_id = int(tag[2:])
        except ValueError:
            return False
        with session_scope() as db:
            row = db.get(OrderLog, log_id)
            return row is not None and row.account_id == self.ids[self.accts.index(a)]

    def tag_kind(self, tag: str) -> str | None:
        """The kind ("entry" / "exit") of the run that sent a TradeLab order; None if unknown."""
        if tag in self._kinds:
            return self._kinds[tag]
        kind = None
        if tag.startswith("TL") and tag[2:].isdigit():
            with session_scope() as db:
                row = db.get(OrderLog, int(tag[2:]))
                if row is not None and row.run_id is not None:
                    run = db.get(ExecutionRun, row.run_id)
                    kind = run.kind if run is not None else None
        elif tag.startswith("SQ"):
            kind = "exit"
        if kind is not None:
            self._kinds[tag] = kind
        return kind

    def is_exit_tag(self, a: FuzzAccount, tag: str) -> bool:
        return self.tag_kind(tag) == "exit"

    # -- the steps --------------------------------------------------------------------------------------
    def run(self) -> list[dict]:
        self.setup()
        for i, st in enumerate(self.spec["steps"]):
            self.step_no = i
            try:
                try:
                    self.do(st)
                except RunKilled:
                    pass
                if KILLED.is_set():
                    self.wait_idle()
                    self.after_kill()
            except Exception as exc:  # noqa: BLE001
                self.violate("HARNESS_ERROR", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                break
            self.wait_idle()
            if self.restart_next and self.last is not None and self.last.get("run") is not None:
                self.restart_next = False
                self.crash_and_restart(self.last["run"].id)
                with session_scope() as db:
                    run = db.get(ExecutionRun, self.last["run"].id)
                    db.expunge(run)
                self.last["run"] = run
            if self.last is not None:  # a fault or lag lasts for one TradeLab action
                for a in self.accts:
                    a.reset_faults()
            self.drain()
            self.check_physical()
            self.after(st)
        return self.violations

    def do(self, st: dict) -> None:
        op = st["op"]
        self.go(st["day"], st["t"])
        self.last = None
        self.interfered = False
        self.restarted = False
        a = self.accts[st["acct"] % len(self.accts)] if "acct" in st else None
        if self.verbose and op in ("buy", "sell", "panic", "isq", "ib"):
            self.log(f"before {op}: {self.snap()}")
        getattr(self, "op_" + op)(st, a)

    # ---- TradeLab actions
    def op_ib(self, st, a):
        stt = self.api("get", "/api/automation/status").json()["lets_trade"]
        r = self.api("post", "/api/automation/lets-trade",
                     json={"trade_date": stt["trade_date"], "fingerprint": stt["fingerprint"], "section": "intraday"})
        self.log(f"intraday Let's Trade at {self.clock.now():%H:%M} -> {r.status_code}")
        if r.status_code == 200 and r.json().get("run_id"):
            run = self.wait_run(r.json()["run_id"])
            self.log(f"   run {run.status}: {run.summary}")
            self.last = {"kind": "entry", "run": run}

    def op_ibs(self, st, a):
        """Let's Trade confirmed before the open; the scheduler buys at the entry time."""
        stt = self.api("get", "/api/automation/status").json()["lets_trade"]
        r = self.api("post", "/api/automation/lets-trade",
                     json={"trade_date": stt["trade_date"], "fingerprint": stt["fingerprint"], "section": "intraday"})
        self.log(f"intraday Let's Trade confirmed at {self.clock.now():%H:%M} -> {r.status_code}")
        if r.status_code != 200:
            return
        self.clock.set(self.clock.now().replace(hour=9, minute=15, second=0, microsecond=0))
        run_id = trading.run_entry(self.user, "scheduled", fire_at=self.clock.now())
        self.wait_idle()
        if run_id is not None:
            with session_scope() as db:
                run = db.get(ExecutionRun, run_id)
                db.expunge(run)
            self.log(f"   scheduled entry: run {run.status}: {run.summary}")
            self.last = {"kind": "entry", "run": run}

    def op_buy(self, st, a):
        sec = st["sec"]
        syms = self.pick_symbols(sec, st)
        params = {"action": "buy"} | ({"symbols": syms} if syms else {})
        pv = self.api("get", f"/api/sections/{sec}/preview", params=params)
        if pv.status_code != 200:
            self.log(f"buy {sec}: preview {pv.status_code}")
            return
        body = {"fingerprint": pv.json()["fingerprint"], "confirm_id": pv.json()["confirm_id"], "symbols": syms}
        r = self.api("post", f"/api/sections/{sec}/buy", json=body)
        self.log(f"buy {sec} {syms or ''} at {self.clock.now():%H:%M} -> {r.status_code}")
        self.double_tap(r, f"/api/sections/{sec}/buy", body, st)
        if r.status_code == 200:
            run = self.wait_run(r.json()["run_id"])
            self.log(f"   run {run.status}: {run.summary}")
            self.last = {"kind": "buy", "run": run, "symbols": set(syms) if syms else None}

    def double_tap(self, first, url: str, body: dict, st: dict) -> None:
        """"Yes" tapped twice: the second tap must answer with the same run (or refuse), never start another."""
        if first.status_code != 200 or st["u2"] > 0.2:
            return
        second = self.api("post", url, json=body)
        if second.status_code == 200 and second.json()["run_id"] != first.json()["run_id"]:
            self.violate("DOUBLE_TAP_TWO_RUNS", f"{url}: the same confirmation started run {first.json()['run_id']} and "
                                                f"run {second.json()['run_id']}")

    def pick_symbols(self, sec: str, st: dict):
        if not st.get("sub"):
            return None
        ks = self.keys.get(sec) or []
        if not ks:
            return None
        return [ks[int(st["u2"] * len(ks)) % len(ks)][1]]

    def op_sell(self, st, a):
        sec = st["sec"]
        syms = self.pick_symbols(sec, st)
        opened = st.get("opened")
        params = {"action": "sell"} | ({"symbols": syms} if syms else {}) | ({"opened": opened} if opened else {})
        pv = self.api("get", f"/api/sections/{sec}/preview", params=params)
        if pv.status_code != 200:
            self.log(f"sell {sec}: preview {pv.status_code}")
            return
        body = {"fingerprint": pv.json()["fingerprint"], "confirm_id": pv.json()["confirm_id"], "symbols": syms,
                "opened": opened}
        r = self.api("post", f"/api/sections/{sec}/sell", json=body)
        self.log(f"sell {sec} {syms or ''} opened={opened} at {self.clock.now():%H:%M} -> {r.status_code}")
        self.double_tap(r, f"/api/sections/{sec}/sell", body, st)
        if r.status_code == 200:
            run = self.wait_run(r.json()["run_id"])
            self.log(f"   run {run.status}: {run.summary}")
            self.last = {"kind": "sell", "run": run, "section": sec, "symbols": set(syms) if syms else None,
                         "opened": opened}

    def op_isq(self, st, a):
        how = st["how"]
        if how == "api":
            r = self.api("post", "/api/automation/square-off", params={"section": "intraday"})
            if r.status_code != 200:
                self.log(f"square-off -> {r.status_code}")
                return
            run = self.wait_run(r.json()["run_id"])
            accounts = None
        else:
            run_id = trading.run_exit(self.user, how, section="intraday")
            self.wait_idle()
            if run_id is None:
                self.log(f"square-off ({how}) did nothing")
                return
            with session_scope() as db:
                run = db.get(ExecutionRun, run_id)
                db.expunge(run)
            # (the accounts the automatic square-off looks at: those where an entry order was sent today)
            accounts = trading.accounts_traded_today(self.user, self.clock.now().date(), "intraday")
        self.log(f"square-off ({how}) at {self.clock.now():%H:%M} -> run {run.status}: {run.summary}")
        self.last = {"kind": "isq", "run": run, "accounts": accounts}

    def op_panic(self, st, a):
        r = self.api("post", "/api/automation/square-off-all")
        if r.status_code != 200:
            self.log(f"panic -> {r.status_code}")
            return
        run = self.wait_run(r.json()["run_id"])
        self.log(f"PANIC at {self.clock.now():%H:%M} -> run {run.status}: {run.summary}")

        self.last = {"kind": "panic", "run": run}

    def op_trueup(self, st, a):
        # (A true-up that Kotak's fault stopped is HANDOFF's accepted "true-up didn't run" case: faults wait for the
        # next TradeLab action instead.)
        saved = [(ac, dict(ac.faults)) for ac in self.accts]
        for ac in self.accts:
            ac.faults.clear()
        trading.true_up_all()
        for ac, f in saved:
            ac.faults.update(f)
        self.log(f"true-up at {self.clock.now():%H:%M}")

    def op_overnight(self, st, a):
        # (The 15:40 / 23:58 true-ups are steps of their own.) Kotak starts its new day; the units bought for
        # delivery are in neither positions nor demat until their settlement day's afternoon.
        old = DAYS[st["day"] - 1]
        arrive = self.settlement_arrival(old)
        with session_scope() as db:
            n_unk = db.count(OrderLog, {"status": "unknown", "final": {"$ne": True}, "action": {"$in": ["buy", "sell"]}})
        if n_unk:
            STATS["old_unknown_rows_at_overnight"] = STATS.get("old_unknown_rows_at_overnight", 0) + n_unk
        for ac in self.accts:
            ac.rollover(arrive, DAYS[st["day"]])
        self.log(f"overnight to {DAYS[st['day']]:%a %d %b}; delivery buys of {old:%d %b} arrive {arrive}")

    def settlement_arrival(self, bought: date) -> datetime:
        d = bought + timedelta(days=1)
        clearing = {DAYS[self.spec["clearing_day"]]} if self.spec["clearing_day"] is not None else set()
        while d.weekday() >= 5 or d in clearing:
            d += timedelta(days=1)
        return datetime(d.year, d.month, d.day, 16, 0, tzinfo=IST)

    def op_quiet(self, st, a):
        for ac in self.accts:
            ac.reset_faults()
            for o in ac.orders:
                o.pop("_cancel_in", None)
                if o["ordSt"] == "open":  # a market order's unfilled rest doesn't stay for ever: Kotak ends it
                    o["ordSt"] = "cancelled"
        self.wait_idle()
        if self.clock.now().time() < dtime(9, 5):
            self.clock.set(self.clock.now().replace(hour=9, minute=5))
        trading.reconcile_today()
        trading.true_up_all()
        self.last = {"kind": "quiet"}
        self.log(f"quiet moment at {self.clock.now():%H:%M}")

    def op_restart(self, st, a):
        self.restart_next = True
        self.log("the server will restart at the end of the next TradeLab action")

    def op_kill(self, st, a):
        def fn(acct):
            with session_scope() as db:
                if not db.count(ExecutionRun, {"status": "running"}):
                    return False  # (a preview or a screen refresh, not a run: a dead server there changes nothing)
            KILLED.set()
            raise RunKilled()

        m, nth = st["m"], st["nth"]
        a.hooks.append({"m": m, "w": st["when"], "nth": a.counts.get(m, 0) + nth, "fn": fn, "done": False,
                        "kill": True})
        self.log(f"the server will die {st['when']} {a.ucc}.{m} #{nth} of the next TradeLab action")

    def after_kill(self) -> None:
        """The process is gone: what it was doing stops where it was, then a new process starts."""
        deadline = time.monotonic() + 30
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.name.startswith(("entry-", "exit-", "par")):
                t.join(max(0.0, deadline - time.monotonic()))
        KILLED.clear()
        with trading._live_guard:
            trading._live.clear()
        trading._squared.clear()
        trading._confirmations.clear()
        trading._stopped_at.clear()
        broker_manager._entries.clear()
        trading.recover_after_restart()
        self.restarted = True
        self.log("SERVER DIED mid-run and restarted")
        if self.last is not None and self.last.get("run") is not None:
            with session_scope() as db:
                run = db.get(ExecutionRun, self.last["run"].id)
                db.expunge(run)
            self.last["run"] = run

    def op_par(self, st, a):
        if any(h.get("kill") for ac in self.accts for h in ac.hooks):
            return  # (a death inside a request thread would take the test client down with it)
        errors = []

        def one(sub):
            try:
                getattr(self, "op_" + sub["op"])(sub, self.accts[sub["acct"] % len(self.accts)])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

        self.log(f"two actions at once: {[x['op'] + ':' + str(x.get('sec', '')) for x in st['subs']]}")
        threads = [threading.Thread(target=one, args=(sub,), name="par") for sub in st["subs"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        for e in errors:  # e.g. two Let's Trade taps at the same instant: the second one's request fails with a 500
            self.notes.append(f"[{self.step_no}] a request of two at once raised: {e.splitlines()[0][:200]}")
        self.last = {"kind": "par"}
        self.par_ran = True

    def op_autopilot(self, st, a):
        i = st["acct"] % len(self.accts)
        r = self.api("patch", f"/api/accounts/{self.ids[i]}", json={"automation_enabled": st["on"]})
        self.log(f"autopilot of {UCCS[i]} {'on' if st['on'] else 'off'} -> {r.status_code}")

    def op_remove(self, st, a):
        sec = st["sec"]
        items = self.api("get", "/api/basket", params={"section": sec}).json()
        if not items:
            return
        it = items[int(st["u"] * 997) % len(items)]
        r = self.api("delete", f"/api/basket/{it['id']}")
        self.log(f"removed {it['trading_symbol']} from the {sec} list -> {r.status_code}")

    def crash_and_restart(self, run_id: int) -> None:
        """The server dies after the orders went out but before their replies were recorded, and starts again."""
        with session_scope() as db:
            run = db.get(ExecutionRun, run_id)
            run.status, run.summary, run.finished_at = "running", None, None
            kind, section = run.kind, run.section
            for r in db.find(OrderLog, {"run_id": run_id, "tag": {"$ne": None}}):
                if r.action in ("buy", "sell") and r.status in ("success", "unknown", "failed"):
                    r.status, r.order_id, r.final, r.filled, r.sent_at, r.message = "pending", None, False, None, None, "Sending…"
        if kind == "entry" and section == "intraday":
            from app.models import TradeSession
            with session_scope() as db:
                db.update_many(TradeSession, {"user_id": self.user, "section": section, "status": "done"},
                               {"status": "firing"})
        with trading._live_guard:
            trading._live.clear()
        trading._squared.clear()
        trading._confirmations.clear()
        trading._stopped_at.clear()
        broker_manager._entries.clear()
        trading.recover_after_restart()
        self.restarted = True
        self.log(f"SERVER RESTART after run {run_id} ({kind} {section})")

    # ---- the holders and Kotak
    def trade_keys(self, a):
        ks = []
        for sec in ("holding", "fno", "commodity", "intraday"):
            ks += self.keys.get(sec, [])
        return ks

    def op_holder(self, st, a):
        self.holder_trade(st, a)

    def holder_trade(self, st, a):
        ks = self.trade_keys(a)
        if not ks:
            return None
        seg, ts, prod, lot = ks[int(st["u"] * 997) % len(ks)]
        key = (seg, ts, prod)
        own = a.own.setdefault(key, [0, 0])
        tl, h = own
        act = st["act"]
        u = st["u2"]
        if act == "fill":
            self.fill_some(a, u)
            return None
        if act == "takeover":  # the holder (or Kotak's risk system) closes units TradeLab opened
            cand = [k for k in ks if a.own.get((k[0], k[1], k[2]), [0, 0])[0] != 0]
            if not cand:
                return None
            seg, ts, prod, lot = cand[int(st["u"] * 997) % len(cand)]
            key = (seg, ts, prod)
            tl, h = a.own[key]
            mine_side = 1 if tl > 0 else -1
            side = "S" if tl > 0 else "B"
            if prod == "CNC":
                tl_now = a.tl_sellable(key)
                take = max(1, math.ceil(u * tl_now)) if tl_now > 0 else 0
                qty = min(a.her_sellable(key) + take, a.cnc_available(seg, ts))  # (her pledged / settling units can't go)
                if qty <= 0 or take <= 0:
                    return None
            else:
                take = max(1, math.ceil(u * abs(tl) / lot)) * lot
                own_same = max(h * mine_side, 0)  # her own units on that side go first
                qty = own_same + min(take, abs(tl))
            STATS["holder_takeover"] = STATS.get("holder_takeover", 0) + 1
            res = a.place_order(seg, prod, "0", "MKT", str(int(qty)), "DAY", ts, side)
            self.log(f"holder {a.ucc} takes TradeLab's side: {side} {qty} {ts} {prod} -> {res.get('stat') if isinstance(res, dict) else res}")
            return key
        if prod == "CNC":
            if act == "buy":  # she buys more of it herself (it shows in today's positions, then in the demat)
                qty, side = max(1, math.ceil(u * 12)), "B"
            else:
                n = min(a.her_sellable(key), max(h, 0))
                if n <= 0:
                    return None
                qty = n if act == "all" else max(1, math.ceil(u * n))
                side = "S"
        else:
            unit = lot
            net = a.phys_net(key)
            if act == "all":
                if net == 0:
                    return None
                side, qty = ("S", net) if net > 0 else ("B", -net)
            elif act == "sell":
                side = "S"
                qty = max(1, math.ceil(u * 3)) * unit if h <= 0 else min(h, max(1, math.ceil(u * h / unit)) * unit)
            else:
                side = "B"
                qty = max(1, math.ceil(u * 3)) * unit if h >= 0 else min(-h, max(1, math.ceil(-u * h / unit)) * unit)
            if prod == "MIS" and unit == 1:
                qty = max(1, math.ceil(u * 30)) if act != "all" else qty
        STATS["holder_trade"] = STATS.get("holder_trade", 0) + 1
        res = a.place_order(seg, prod, "0", "MKT", str(int(qty)), "DAY", ts, side)
        self.log(f"holder {a.ucc} {side} {qty} {ts} {prod} -> {res.get('stat') if isinstance(res, dict) else res}")
        return key

    def op_pledge(self, st, a):
        """The holder pledges some of her free units in the demat, or releases some pledged ones (round 5)."""
        keys = [("nse_cm", ts, "CNC") for ts, d in a.demat.items() if d[0] > 0]
        if not keys:
            return
        key = keys[int(st["u"] * 997) % len(keys)]
        d = a.demat[key[1]]
        if st["act"] == "pledge":
            free = max(d[1] - a.tl_sellable_in_demat(key), 0)
            n = math.ceil(st["u2"] * free) if free else 0
            d[1] -= n
            self.log(f"holder {a.ucc} pledges {n} of {key[1]} (sellable now {d[1]} of {d[0]})")
        else:
            n = math.ceil(st["u2"] * (d[0] - d[1])) if d[0] > d[1] else 0
            d[1] += n
            self.log(f"holder {a.ucc} releases {n} pledged {key[1]} (sellable now {d[1]} of {d[0]})")
        if n:
            STATS["pledge_" + st["act"]] = STATS.get("pledge_" + st["act"], 0) + 1

    def op_blackout(self, st, a):
        """Kotak's order book is unreadable from now until the next trading day, and the next orders' replies get lost: an
        order with no known outcome that is still unresolved when the book clears overnight (round 5)."""
        a.down.add("order_report")
        if st.get("lost"):
            reply = (PLACED_BUT, GATEWAY) if st["lost"] == "placed" else GATEWAY
            a.faults["place_order"] = [reply] * st.get("n", 1)
        STATS["blackout"] = STATS.get("blackout", 0) + 1
        self.log(f"{a.ucc}: Kotak's order book is unreadable until the next day; place_order lost={st.get('lost')}")

    def op_heal(self, st, a):
        for ac in self.accts:
            ac.down.clear()
        self.log("Kotak's order book reads again")

    def fill_some(self, a, u):
        rows = [o for o in a.orders if o["ordSt"] not in FINISHED and o["qty"] > o["fldQty"]]
        if not rows:
            return
        row = rows[int(u * 997) % len(rows)]
        n = max(1, math.ceil(u * (row["qty"] - row["fldQty"])))
        got = a.fill_working(row, n)
        self.log(f"Kotak fills {got} more of {row['trdSym']} {row['trnsTp']} order {row['nOrdNo']} ({row.get('_tag') or 'holder'})")

    def op_fill(self, st, a):
        self.fill_some(a, st["u"])

    def op_sl(self, st, a):
        ks = [k for sec in ("intraday", "fno", "commodity") for k in self.keys.get(sec, [])]
        if not ks:
            return
        seg, ts, prod, _ = ks[int(st["u"] * 997) % len(ks)]
        key = (seg, ts, prod)
        net = a.phys_net(key)
        h = a.own.get(key, [0, 0])[1]
        # An intraday stop-loss covers the whole position the holder sees; one on a contract covers her own lots.
        qty = net if (prod == "MIS" or st["u2"] < 0.4) else h  # (a holder looking at the whole net position)
        if qty == 0:
            return
        oid = a.add_open_order(ts, seg, prod, "S" if qty > 0 else "B", abs(qty))
        self.log(f"holder {a.ucc} sets a stop-loss {oid} on {ts} ({'S' if qty > 0 else 'B'} {abs(qty)})")

    def op_slfire(self, st, a):
        rows = [o for o in a.orders if o["ordSt"] == "trigger pending"]
        if not rows:
            return
        row = rows[int(st["u"] * 997) % len(rows)]
        got = a.fill_working(row, row["qty"] - row["fldQty"])
        self.log(f"stop-loss {row['nOrdNo']} on {a.ucc} {row['trdSym']} fires ({got})")

    def op_fault(self, st, a):
        m, kind, n = st["m"], st["kind"], st["n"]
        if m == "place_order":
            reply = {
                "lost": (PLACED_BUT, GATEWAY),
                "timeout": (PLACED_BUT, {"message": "Endpoint request timed out"}),
                "gateway": GATEWAY,
                "http500": {"stat": "Not_Ok", "emsg": "Unexpected error", "stCode": 500},
                "1003": {"stat": "Not_Ok", "emsg": "Invalid session, please re-login", "stCode": 1003},
                "refuse": {"stat": "Not_Ok", "emsg": "RMS:Margin Exceeds,Required:500.00", "stCode": 1009},
            }[kind]
        else:
            reply = GATEWAY if kind in ("lost", "gateway", "timeout") else {"stat": "Not_Ok", "emsg": "Unexpected error",
                                                                              "stCode": 500}
        q = [lambda _a: NO_FAULT] * st["skip"] + [reply] * n
        a.faults[m] = q
        self.log(f"fault on {a.ucc}.{m}: {kind} x{n} after {st['skip']} good calls")

    def op_toggle(self, st, a):
        w = st["what"]
        if w == "poslag":
            a.pos_lag = 0.2 + st["u"] * 1.3
            self.log(f"{a.ucc}: positions show a fill {a.pos_lag:.1f} s late")
        elif w == "booklag":
            a.book_lag = 1 + int(st["u"] * 3)
            self.log(f"{a.ucc}: new orders miss the first {a.book_lag} order-book reads")
        elif w == "partial":
            a.partial_next = int(st["u"] * 30)
            self.log(f"{a.ucc}: next order fills only {a.partial_next}")
        elif w == "reject":
            a.reject_in_book = "RMS:Margin Exceeds,Required:100.00,Available:0.00"
            self.log(f"{a.ucc}: next order is rejected in the book")
        elif w == "partial_reject":
            a.partial_next = int(st["u"] * 30)
            a.reject_in_book = "RMS:Quantity exceeds freeze limit"
            self.log(f"{a.ucc}: next order fills {a.partial_next}, then the rest is rejected")
        elif w == "expire":
            entry = broker_manager._entries.get(self.ids[st["acct"] % len(self.accts)])
            if entry is not None:
                entry.logged_in_at = None  # the next call needs a fresh login
            self.log(f"{a.ucc}: the Kotak session is gone")
        elif w == "cancel_lag":
            a.cancel_lag = 1 + int(st["u"] * 3)
            self.log(f"{a.ucc}: cancels lag {a.cancel_lag} reads")

    def op_inject(self, st, a):
        m, nth = st["m"], st["nth"]
        do = dict(st["do"])

        def fn(acct, do=do):
            self.interfered = True
            tgt = self.accts[do["acct"] % len(self.accts)]
            key = self.holder_trade(do, tgt)
            if key is not None:  # (what a trade in the middle of a run did to TradeLab's units can't be told apart)
                tgt.ambiguous.add(key)

        a.hooks.append({"m": m, "w": st["when"], "nth": a.counts.get(m, 0) + nth, "fn": fn, "done": False})
        self.log(f"holder action planned {st['when']} {a.ucc}.{m} #{nth}: {do['act']}")

    # ---- checks after a step
    def after(self, st) -> None:
        last = self.last
        if last is None:
            return
        kind = last["kind"]
        if "run" in last:
            key = f"{kind}:{last['run'].status}"
            STATS[key] = STATS.get(key, 0) + 1
            placed = (last["run"].summary or "").split(" order")[0].split()[-1:]
            if placed and placed[0].isdigit() and int(placed[0]) > 0:
                STATS[f"{kind}:with_orders"] = STATS.get(f"{kind}:with_orders", 0) + 1
        if kind == "quiet":
            self.check_records()
            return
        if kind == "buy" and not self.interfered and not self.restarted:
            self.check_short_after_hedge(last["run"], last.get("symbols"))
            self.check_buy_done(last["run"])
        if kind not in ("sell", "panic", "isq"):
            return
        run = last["run"]
        if (kind == "panic" or (kind == "sell" and not last.get("opened") and not last.get("symbols"))) \
                and not self.interfered and not self.restarted:
            self.check_hedges(run)
        left = self.units_left(last)
        if kind == "sell" and any(k[2] == "CNC" and tl > 0 and a.demat.get(k[1], [0, 0])[0] > a.demat.get(k[1], [0, 0])[1]
                                  for a in self.accts for k, (tl, h) in a.own.items()):
            STATS["sell_beside_pledged_units"] = STATS.get("sell_beside_pledged_units", 0) + 1
        names = self.alert_names(run.id)
        if run.status == "done" and not self.interfered and not self.restarted:
            self.check_stray_orders(run)
        if run.status == "done":
            if left and not self.interfered:
                self.violate("DONE_BUT_OPEN", f"{kind} run {run.id} reported done but TradeLab units remain: {left}",
                             run_kind=kind)
        elif not self.restarted:  # (a run cut short by a restart names no account)
            for uc, key, n in left:
                if NAMES[UCCS.index(uc)] not in names:
                    self.violate("OPEN_NOT_REPORTED", f"{kind} run {run.id} ({run.status}) left {n} of {key[1]} in {uc} "
                                                      f"open, pop-up names {names}", run_kind=kind)

    def check_buy_done(self, run) -> None:
        """A Buy of F&O / Commodity that reports done has every order of the run filled in full."""
        if run.section not in ("fno", "commodity") or run.status != "done":
            return
        with session_scope() as db:
            tags = {r.tag for r in db.find(OrderLog, {"run_id": run.id, "tag": {"$ne": None}}, track=False)}
        for a in self.accts:
            for o in a.orders:
                if o.get("_tag") in tags and (o["ordSt"] != "complete" or o["fldQty"] < o["qty"]):
                    self.violate("BUY_DONE_BUT_SHORT", f"{a.ucc}: run {run.id} is done but order {o['nOrdNo']} "
                                                       f"{o['trdSym']} {o['trnsTp']} ended {o['ordSt']} "
                                                       f"{o['fldQty']}/{o['qty']}", ucc=a.ucc)

    def check_short_after_hedge(self, run, symbols=None) -> None:
        """A Buy of a lots section sells a contract first only after every bought contract of its underlying (in that
        account) filled in full: a refused, skipped or part-filled hedge never leaves the sold leg naked."""
        sec = run.section
        if sec not in ("fno", "commodity"):
            return
        items = self.api("get", "/api/basket", params={"section": sec}).json()
        with session_scope() as db:
            tags = {r.tag for r in db.find(OrderLog, {"run_id": run.id, "tag": {"$ne": None}}, track=False)}

        def underlying(seg, ts):
            c = contracts.contract_master.get(seg, ts)
            return c.symbol if c else ts

        for a in self.accts:
            mine = [(i, o) for i, o in enumerate(a.orders) if o.get("_tag") in tags]
            for i, o in mine:
                if o["trnsTp"] != "S":
                    continue
                und = underlying(o["exSeg"], o["trdSym"])
                wanted = [it for it in items if it["side"] == "B" and it["active"]
                          and (symbols is None or it["trading_symbol"] in symbols)
                          and it["exchange_segment"] == o["exSeg"] and underlying(it["exchange_segment"], it["trading_symbol"]) == und
                          and (it["account_ids"] is None or self.ids[self.accts.index(a)] in it["account_ids"])]
                for it in wanted:
                    got = [(j, h) for j, h in mine if h["trnsTp"] == "B" and h["trdSym"] == it["trading_symbol"]]
                    if not any(j < i and h["ordSt"] == "complete" and h["fldQty"] >= h["qty"] for j, h in got):
                        self.violate("SHORT_BEFORE_HEDGE", f"{a.ucc}: run {run.id} sold {o['trdSym']} first although its "
                                                           f"hedge {it['trading_symbol']} was not fully bought "
                                                           f"({[(h['ordSt'], h['fldQty'], h['qty']) for _, h in got]})",
                                     ucc=a.ucc)

    def check_stray_orders(self, run) -> None:
        """C3: a run that closed a position and calls itself done leaves no other working order that would open
        a new position when it fills."""
        with session_scope() as db:
            rows = db.find(OrderLog, {"run_id": run.id, "action": {"$in": ["buy", "sell"]}, "tag": {"$ne": None},
                                      "status": {"$in": ["success", "unknown"]}}, track=False)
        touched = {(self.ids.index(r.account_id), r.exchange_segment, r.symbol, (r.product or "").upper(),
                    "S" if r.action == "sell" else "B") for r in rows if r.account_id in self.ids}
        for i, seg, ts, prod, closed_by in touched:
            a = self.accts[i]
            key = (seg, ts, prod)
            net = a.phys_net(key) if prod != "CNC" else None
            if net is None or key in a.ambiguous:
                continue
            work = [o for o in a.orders if (o["exSeg"], o["trdSym"], o["prod"]) == key and o["ordSt"] in
                    ("open", "trigger pending") and not (o.get("_tag") or "")[:2] in ("TL", "SQ")]
            # (only a working order on the side TradeLab just closed with matters: a sale that closed a long leaves a
            # holder's sell order that would now sell short; the other side was no doing of this run)
            sells = sum(o["qty"] - o["fldQty"] for o in work if o["trnsTp"] == "S") if closed_by == "S" else 0
            buys = sum(o["qty"] - o["fldQty"] for o in work if o["trnsTp"] == "B") if closed_by == "B" else 0
            if sells > max(net, 0) or buys > max(-net, 0):
                self.violate("DONE_BESIDE_STRAY_ORDER", f"{a.ucc} {ts} {prod}: run {run.id} is done, net {net}, but a "
                                                        f"working order of the holder (sell {sells} / buy {buys}) would "
                                                        f"open a new position", ucc=a.ucc, key=key)

    def check_hedges(self, run) -> None:
        """C5: a run never sells a bought contract while a sold-first contract of the same underlying stays open
        in that account."""
        with session_scope() as db:
            rows = db.find(OrderLog, {"run_id": run.id, "action": "sell", "status": {"$in": ["success", "unknown"]},
                                      "exchange_segment": {"$in": ["nse_fo", "bse_fo", "mcx_fo"]}}, track=False)
        sold = {(r.account_id, r.symbol) for r in rows if r.tag}
        for i, a in enumerate(self.accts):
            legs = {}
            for (seg, ts, prod), (tl, _h) in a.own.items():
                if prod == "NRML" and seg in ("nse_fo", "bse_fo", "mcx_fo"):
                    c = contracts.contract_master.get(seg, ts)
                    legs[ts] = (c.symbol if c else ts, tl, seg)
            for ts, (und, tl, seg) in legs.items():
                if (self.ids[i], ts) not in sold:
                    continue
                naked = [t2 for t2, (u2, tl2, seg2) in legs.items() if u2 == und and seg2 == seg and tl2 < 0]
                if naked and not any(k in a.ambiguous for k in a.own):
                    self.violate("HEDGE_SOLD_NAKED", f"{a.ucc}: run {run.id} sold {ts} while the sold-first {naked} of the "
                                                     f"same underlying stays open", ucc=a.ucc)

    def alert_names(self, run_id: int) -> set[str]:
        out: set[str] = set()
        for al in self.api("get", "/api/alerts").json():
            if al["run_id"] == run_id and al["kind"] == "exit":
                out |= set(al["accounts"])
        return out

    def units_left(self, last: dict) -> list:
        kind = last["kind"]
        out = []
        for a in self.accts:
            for key, (tl, h) in a.own.items():
                seg, ts, prod = key
                if key in a.tainted or key in a.ambiguous:
                    continue
                if prod == "MIS":
                    tl = a.phys_net(key)  # the square-off closes every MIS position of the account in scope
                if tl == 0:
                    continue
                if kind == "sell":
                    sec = last["section"]
                    if (sec == "holding") != (prod == "CNC") or prod == "MIS":
                        continue
                    if prod == "NRML" and (seg == "mcx_fo") != (sec == "commodity"):
                        continue
                    if last["symbols"] is not None and ts not in last["symbols"]:
                        continue
                    if last["opened"] and (tl > 0) != (last["opened"] == "B"):
                        continue
                elif kind == "panic":
                    if prod == "CNC":
                        continue
                elif kind == "isq":
                    if prod != "MIS" or seg not in ("nse_cm", "bse_cm"):
                        continue
                    if last.get("accounts") is not None and self.ids[self.accts.index(a)] not in last["accounts"]:
                        continue
                out.append((a.ucc, key, tl))
        return out

    def rows_for(self, account_id: int, symbol: str) -> str:
        with session_scope() as db:
            rows = db.find(OrderLog, {"account_id": account_id, "symbol": symbol}, sort=[("id", 1)], track=False)
        from app.db import database
        doc = database()["holder_units"].find_one({"_id": f"{account_id}:{symbol}"})
        lines = [f"#{r.id} run{r.run_id} {r.created_at:%d %H:%M} {r.action} {r.quantity} {r.status} filled={r.filled} "
                 f"final={r.final} :: {(r.message or '')[:110]}" for r in rows]
        if doc:
            lines.append(f"holder_units: {doc}")
        return "\n          ".join(lines)

    def known_race(self, k) -> bool:
        """Findings G-4 / G-5: an adjustment written while a close of the same units was in flight (inside an exit run), or
        written twice at the same moment. Those keys are judged by their own xfail tests."""
        with session_scope() as db:
            adj = db.find(OrderLog, {"account_id": k[0], "symbol": k[2], "status": "adjusted"}, sort=[("id", 1)], track=False)
            runs = db.find(ExecutionRun, {"kind": "exit"}, track=False)
        for i, a in enumerate(adj):
            t = trading._aware(a.created_at)
            for b in adj[i + 1:]:
                if abs((trading._aware(b.created_at) - t).total_seconds()) < 3 and b.message == a.message:
                    return True
            for r in runs:
                end = trading._aware(r.finished_at) if r.finished_at else None
                if trading._aware(r.started_at) <= t and (end is None or t <= end):
                    return True
        return False

    def check_records(self) -> None:
        recs: dict[tuple, int] = {}
        for sec in ("holding", "fno", "commodity"):
            for k, q in trading.tradelab_open(self.user, sec).items():
                recs[(k.account_id, k.exchange_segment, k.trading_symbol, k.product)] = q
        oracle: dict[tuple, int] = {}
        for i, a in enumerate(self.accts):
            for (seg, ts, prod), (tl, h) in a.own.items():
                if prod != "MIS" and tl:
                    oracle[(self.ids[i], seg, ts, prod)] = tl
        tainted = {(self.ids[i], *key) for i, a in enumerate(self.accts) for key in a.tainted | a.ambiguous}
        # A close whose outcome is unknown counts as closed (so nothing is ever closed twice): records below the oracle
        # are expected while one is unresolved; the next Exit must report it (checked as DONE_BUT_OPEN). An opening order
        # with no known outcome is in no record either (G-2), and Exit reports its key as well.
        with session_scope() as db:
            unknown_closes = db.find(OrderLog, {"status": "unknown", "final": {"$ne": True}, "tag": {"$ne": None},
                                                "action": {"$in": ["buy", "sell"]}}, track=False)
        tainted |= {(r.account_id, r.exchange_segment, r.symbol, (r.product or "").upper())
                    for r in unknown_closes}
        for k in sorted(set(recs) | set(oracle)):
            if k in tainted:
                STATS["records_check_skipped_tainted_or_ambiguous"] = STATS.get("records_check_skipped_tainted_or_ambiguous", 0) + 1
                continue
            if k[3] == "CNC" and abs(recs.get(k, 0)) > abs(oracle.get(k, 0)):
                a = self.accts[self.ids.index(k[0])]
                key = (k[1], k[2], k[3])
                last = a.bought_on.get(key)
                if a.pend.get(key) or self.clock.now().date() in a.settled_on.get(key, ()) \
                        or (last is not None and (self.clock.now().date() - last).days <= 3):
                    # TradeLab keeps units it bought on a recent trading day until their settlement day is over
                    # (it can't know when they arrive): by design, it corrects itself the next trading day.
                    STATS["records_check_settling"] = STATS.get("records_check_settling", 0) + 1
                    continue
            STATS["records_checked"] = STATS.get("records_checked", 0) + 1
            if recs.get(k, 0) != oracle.get(k, 0) and self.skip_known_races and self.known_race(k):
                STATS["records_race_g4_g5"] = STATS.get("records_race_g4_g5", 0) + 1
                continue
            if recs.get(k, 0) != oracle.get(k, 0):
                uc = UCCS[self.ids.index(k[0])]
                kind = "RECORDS_TOO_HIGH" if abs(recs.get(k, 0)) > abs(oracle.get(k, 0)) else "RECORDS_TOO_LOW"
                self.violate(kind, f"{uc} {k[2]} {k[3]}: TradeLab's records say {recs.get(k, 0)}, the oracle says "
                                   f"{oracle.get(k, 0)}\n          " + self.rows_for(k[0], k[2]), ucc=uc, key=k)


# ===================================================================================== running seeds


def run_seed(client, clock, sim: FuzzSim, seed: int, spec: dict | None = None, verbose: bool = False):
    spec = spec or gen_spec(seed)
    w = World(client, clock, sim, spec, verbose)
    try:
        w.run()
    except Exception as exc:  # noqa: BLE001
        w.violate("HARNESS_ERROR", f"setup: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    return w


def apply_mutant(monkeypatch, name: str) -> None:
    """Break TradeLab on purpose (TL_FUZZ_MUTANT=name) to prove the oracle notices: every mutant must be caught."""
    if name == "no_writedown":  # records are never brought down to what Kotak shows
        monkeypatch.setattr(trading, "_shrink_to_broker", lambda *a, **k: {})
    elif name == "oversell":  # every close is a lot (or a share) bigger than it should be
        orig = trading.build_sell_plan

        def build_sell_plan(*a, **kw):
            plan = orig(*a, **kw)
            for p in plan:
                if not p.skip and p.quantity > 0:
                    p.quantity += (p.quantity // p.lots) if p.lots else 1
            return plan

        monkeypatch.setattr(trading, "build_sell_plan", build_sell_plan)
    elif name == "resend":  # an order whose reply was lost is sent again
        orig_place = kotak.KotakNeoBroker.place_market_order

        def place(self, *a, **kw):
            try:
                return orig_place(self, *a, **kw)
            except kotak.OrderOutcomeUnknown:
                return orig_place(self, *a, **kw)

        monkeypatch.setattr(kotak.KotakNeoBroker, "place_market_order", place)
    elif name == "panic_skips_contracts":  # the panic button forgets the carry-forward contracts
        orig = trading.build_sell_plan

        def build_sell_plan(*a, **kw):
            return [] if kw.get("panic") else orig(*a, **kw)

        monkeypatch.setattr(trading, "build_sell_plan", build_sell_plan)
    elif name == "forget_close":  # Sell and Exit report done without sending anything for contracts
        orig = trading.fire_plan

        def fire_plan(plan, *a, **kw):
            if kw.get("closing") or (len(a) > 3 and a[3]):
                plan = [p for p in plan if p.exchange_segment not in ("nse_fo", "mcx_fo", "bse_fo")]
            return orig(plan, *a, **kw)

        monkeypatch.setattr(trading, "fire_plan", fire_plan)
    elif name == "hedge_not_first":  # Buy sends a sold-first contract together with its hedge
        monkeypatch.setattr(trading, "_sales_to_wait_for", lambda plan: [])
    elif name == "exit_hedge_first":  # Exit sells a hedge together with the buy-back of the leg it covers
        monkeypatch.setattr(trading, "_hedges_to_wait_for", lambda plan, unclosed: [])
    elif name == "ignore_unconfirmed":  # sizing from records of orders Kotak's book has not confirmed
        monkeypatch.setattr(trading, "unconfirmed_orders", lambda *a, **k: {})
    elif name == "no_settling_guard":  # units bought yesterday are written off while they settle
        monkeypatch.setattr(trading, "_previous_day_delivery_buys", lambda *a, **k: {})
    elif name == "ignore_unknown_entries":  # round 4: Exit / Buy ignore opening orders with no known outcome
        monkeypatch.setattr(trading, "unresolved_entries", lambda *a, **k: {})
    elif name == "ignore_unknown_closes":  # round 4: Exit / Sell / panic ignore closing orders with no known outcome
        monkeypatch.setattr(trading, "unresolved_closes", lambda *a, **k: {})
    elif name == "pledged_as_free":  # round 5: the holder's pledged units are taken for free ones (TradeLab sells them)
        orig = trading._holders_noted

        def noted(key):
            out = orig(key)
            return None if out is None else (out[0], out[0])

        monkeypatch.setattr(trading, "_holders_noted", noted)
    elif name == "nodata_is_evidence":  # round 5: a "No Data" positions reply proves zero even beside a TradeLab order of today
        monkeypatch.setattr(trading, "_traded_today", lambda key: False)
    elif name:
        raise ValueError(f"unknown mutant {name}")


@pytest.fixture
def env(monkeypatch, clock, client):
    s = FuzzSim()
    broker_manager.kotak_factory = s.factory
    monkeypatch.setattr(kotak, "time", SimpleNamespace(time=time.time, sleep=lambda _s: None))
    patch_clock(monkeypatch, clock)
    guard_kill(monkeypatch)
    apply_mutant(monkeypatch, os.environ.get("TL_FUZZ_MUTANT", ""))
    kotak_sim.PRICES.clear()
    kotak_sim.TOKEN_PRICES.clear()
    return SimpleNamespace(client=client, clock=clock, sim=s)


def _seeds() -> list[int]:
    n = int(os.environ.get("TL_FUZZ_SEEDS", "0") or 0)
    start = int(os.environ.get("TL_FUZZ_START", "0") or 0)
    return list(range(start, start + n))


def fresh_run(env, seed: int, spec: dict | None = None, verbose: bool = False) -> World:
    reset_state(env.clock)
    env.sim.accounts.clear()
    return run_seed(env.client, env.clock, env.sim, seed, spec, verbose)


def summarize(w: World) -> str:
    return "\n".join(w.trace[-60:]) + "\n" + "\n".join(f"  {v['kind']}: {v['detail']}" for v in w.violations)


@pytest.mark.parametrize("seed", range(DEFAULT_SEEDS))
def test_default_seeds(env, seed):
    w = fresh_run(env, seed, verbose=bool(os.environ.get("TL_FUZZ_VERBOSE")))
    assert not w.violations, f"seed {seed}\n" + summarize(w)


def shrink_spec(env, spec: dict, kind: str, budget: int = 400) -> dict:
    """Drop steps, accounts, sections and options while a violation of `kind` still happens."""
    runs = 0

    def bad(sp: dict) -> bool:
        nonlocal runs
        runs += 1
        w = fresh_run(env, sp["seed"], sp)
        return any(v["kind"] == kind for v in w.violations)

    spec = copy.deepcopy(spec)
    changed = True
    while changed and runs < budget:
        changed = False
        # 1. whole steps, from the end
        i = len(spec["steps"]) - 1
        while i >= 0 and runs < budget:
            trial = copy.deepcopy(spec)
            del trial["steps"][i]
            if bad(trial):
                spec, changed = trial, True
            i -= 1
        # 2. accounts
        for i in range(len(spec["accounts"]) - 1, -1, -1):
            if len(spec["accounts"]) > 1 and runs < budget:
                trial = copy.deepcopy(spec)
                del trial["accounts"][i]
                if bad(trial):
                    spec, changed = trial, True
        # 3. simplify the accounts' own holdings and cash
        for i in range(len(spec["accounts"])):
            for field_ in ("demat", "nrml"):
                if spec["accounts"][i][field_] and runs < budget:
                    trial = copy.deepcopy(spec)
                    trial["accounts"][i][field_] = {}
                    if bad(trial):
                        spec, changed = trial, True
            if spec["accounts"][i]["mis"] and runs < budget:
                trial = copy.deepcopy(spec)
                trial["accounts"][i]["mis"] = 0
                if bad(trial):
                    spec, changed = trial, True
        # 4. step options
        for i, st in enumerate(spec["steps"]):
            for k in ("sub", "opened"):
                if st.get(k) and runs < budget:
                    trial = copy.deepcopy(spec)
                    trial["steps"][i][k] = None if k == "opened" else False
                    if bad(trial):
                        spec, changed = trial, True
    spec["_shrink_runs"] = runs
    return spec


def test_shrink(env):
    """TL_FUZZ_SHRINK=seed:KIND (or file.jsonl:seed:KIND) minimises a failing scenario and prints it."""
    arg = os.environ.get("TL_FUZZ_SHRINK")
    if not arg:
        pytest.skip("set TL_FUZZ_SHRINK=seed:KIND or file.jsonl:seed:KIND")
    parts = arg.split(":")
    if len(parts) == 3:
        recs = [json.loads(line) for line in open(parts[0])]
        spec = next(r["spec"] for r in recs if r["seed"] == int(parts[1]))
        seed, kind = int(parts[1]), parts[2]
    else:
        seed, kind = int(parts[0]), parts[1]
        spec = gen_spec(seed)
    small = shrink_spec(env, spec, kind)
    out = os.environ.get("TL_FUZZ_SHRINK_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(small, f, indent=1, default=str)
    w = fresh_run(env, seed, small, verbose=True)
    print("MINIMAL", json.dumps(small, default=str))
    print(summarize(w))


def test_fuzz(env):
    only = os.environ.get("TL_FUZZ_ONLY")
    replay = os.environ.get("TL_FUZZ_REPLAY")  # file.jsonl: replay the recorded specs (seed = TL_FUZZ_ONLY)
    if replay:
        recs = [json.loads(line) for line in open(replay)]
        recs = [r for r in recs if only is None or r["seed"] == int(only)]
        for rec in recs:
            fresh_run(env, rec["seed"], rec["spec"], verbose=True)
        return
    seeds = [int(only)] if only else _seeds()
    prof = os.environ.get("TL_FUZZ_PROFILE")  # only seeds of one profile: broad, holding, derivs, intraday, hedge
    if prof:
        seeds = [sd for sd in seeds if gen_spec(sd)["profile"] == prof]
    if not seeds:
        pytest.skip("set TL_FUZZ_SEEDS")
    out = os.environ.get("TL_FUZZ_OUT")
    bad = 0
    for seed in seeds:
        w = fresh_run(env, seed, verbose=bool(only))
        if any("portal is not running" in v["detail"] for v in w.violations):
            pytest.fail(f"the test client died at seed {seed}")
        if w.violations:
            bad += 1
            print(f"seed {seed}: " + "; ".join(sorted({v["kind"] for v in w.violations})), flush=True)
            if out:
                with open(out, "a") as f:
                    f.write(json.dumps({"seed": seed, "kinds": sorted({v["kind"] for v in w.violations}),
                                        "violations": w.violations, "trace": w.trace[-80:], "spec": w.spec}, default=str) + "\n")
    print("STATS", json.dumps(STATS, sort_keys=True))
    assert not bad, f"{bad} of {len(seeds)} seeds violated an invariant"


# ===================================================================================== real findings, kept as xfail


def mini_spec(**over) -> dict:
    """One account, an Intraday list with SBIN, and nothing else."""
    spec = {"seed": 7, "profile": "broad", "sections": ["intraday"],
            "items": {"intraday": [("SBIN", "B")], "holding": [ETFS[0]], "fno": [FNO_CHOICES[0]],
                      "commodity": [COM_CHOICES[0]]},
            "accounts": [{"cash": 25000.0, "demat": {}, "mis": 0, "nrml": {}}], "steps": [], "max_lots": 1,
            "clearing_day": None, "clearing_list": True}
    spec.update(over)
    return spec


def test_G1_a_second_lets_trade_tap_at_the_same_instant_is_refused_not_a_crash(env, monkeypatch):
    from app.db import Session
    from app.models import TradeSession

    reset_state(env.clock)
    env.sim.accounts.clear()
    w = World(env.client, env.clock, env.sim, mini_spec())
    w.setup()
    w.go(0, "08:30")  # before the open: the confirmation waits for the entry time
    stt = w.api("get", "/api/automation/status").json()["lets_trade"]
    body = {"trade_date": stt["trade_date"], "fingerprint": stt["fingerprint"], "section": "intraday"}
    real_find_one = Session.find_one
    raced = []

    def find_one(self, cls, filter=None, sort=None):
        out = real_find_one(self, cls, filter, sort)
        if cls is TradeSession and out is None and not raced:
            raced.append(1)  # the other tap has confirmed the day just now
            first = w.api("post", "/api/automation/lets-trade", json=body)
            assert first.status_code == 200
        return out

    monkeypatch.setattr(Session, "find_one", find_one)
    second = w.api("post", "/api/automation/lets-trade", json=body)  # raises the IntegrityError inside the app today
    assert second.status_code == 409, second.text



def _step(op, day, t, **kw):
    return {"op": op, "day": day, "t": t, "acct": 0, "u": 0.1, "u2": 0.5, **kw}


def test_G2_an_unresolvable_unknown_buy_is_not_ignored_by_exit(env):
    spec = mini_spec(sections=["commodity"], no_echo=["mcx_fo"], max_lots=2,
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     steps=[_step("fault", 0, "09:50", m="place_order", kind="lost", n=1, skip=0),
                            _step("buy", 0, "10:00", sec="commodity", sub=False),
                            _step("sell", 0, "11:05", sec="commodity", sub=False, opened=None),
                            _step("quiet", 0, "11:20")])
    w = fresh_run(env, 7, spec)
    assert not [v for v in w.violations if v["kind"] in ("DONE_BUT_OPEN", "RECORDS_TOO_LOW")], summarize(w)


def test_G2_the_next_days_exit_still_reports_an_unknown_buy_of_a_carried_contract(env):
    spec = mini_spec(sections=["commodity"], no_echo=["mcx_fo"], max_lots=2,
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     steps=[_step("fault", 0, "09:50", m="place_order", kind="lost", n=1, skip=0),
                            _step("buy", 0, "10:00", sec="commodity", sub=False),
                            _step("trueup", 0, "23:58"), _step("overnight", 1, "02:00"),
                            _step("sell", 1, "11:05", sec="commodity", sub=False, opened=None)])
    w = fresh_run(env, 7, spec)
    assert not [v for v in w.violations if v["kind"] in ("DONE_BUT_OPEN", "OPEN_NOT_REPORTED")], summarize(w)
    with session_scope() as db:
        run = db.find(ExecutionRun, {"kind": "exit"}, sort=[("id", -1)], limit=1, track=False)[0]
    assert run.status != "done", run.status


# (G-3 was found at 2f84ec9 and fixed by e99649d: the durable tag proof now turns the never-placed order into "Not placed".)
def test_G3_a_sell_still_reports_units_settling_beside_an_unconfirmed_order_of_today(env):
    spec = mini_spec(sections=["holding"], profile="holding",
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     steps=[_step("buy", 0, "10:00", sec="holding", sub=False),
                            _step("trueup", 0, "15:40"), _step("trueup", 0, "23:58"),
                            _step("overnight", 1, "02:00"),
                            # the next morning's Buy: a gateway page for the order, which never reached Kotak
                            _step("fault", 1, "11:50", m="place_order", kind="gateway", n=1, skip=0),
                            _step("buy", 1, "12:00", sec="holding", sub=False),
                            _step("sell", 1, "14:00", sec="holding", sub=False, opened=None)])
    w = fresh_run(env, 7, spec)
    assert not [v for v in w.violations if v["kind"] in ("DONE_BUT_OPEN", "OPEN_NOT_REPORTED")], summarize(w)


def _live_kinds() -> set:
    with trading._live_guard:
        return {v[1] for v in trading._live.values()}


def test_G4_a_buy_during_the_panic_button_does_not_write_off_the_units_the_panic_is_closing(env, monkeypatch):
    monkeypatch.setattr(trading, "ENTRY_WAIT", timedelta(seconds=5))
    spec = mini_spec(sections=["commodity"], max_lots=2,
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     items={"intraday": [("SBIN", "B")], "holding": [ETFS[0]], "fno": [FNO_CHOICES[0]],
                            "commodity": [("goldm", "B")]},
                     steps=[_step("buy", 0, "10:00", sec="commodity", sub=False)])
    reset_state(env.clock)
    env.sim.accounts.clear()
    w = World(env.client, env.clock, env.sim, spec)
    w.skip_known_races = False
    w.setup()
    w.step_no = 0
    w.do(spec["steps"][0])  # TradeLab holds 2 lots
    w.wait_idle()
    acct = w.accts[0]
    w.go(0, "14:00")
    phases = {name: threading.Event() for name in ("buy_reads_done", "panic_closed")}

    def start_buy_from_the_panic(_a):  # the panic's first call to Kotak: the user taps Buy now a moment later
        if not _live_kinds() >= {"exit"} or phases["buy_reads_done"].is_set():
            return False
        pv = w.api("get", "/api/sections/commodity/preview", params={"action": "buy"}).json()
        r = w.api("post", "/api/sections/commodity/buy",
                  json={"fingerprint": pv["fingerprint"], "confirm_id": pv["confirm_id"], "symbols": None})
        assert r.status_code == 200, r.text
        assert phases["buy_reads_done"].wait(20), "the Buy never reached Kotak"

    def buy_waits_for_the_close(_a):  # the Buy has read TradeLab's records and orders; now it asks Kotak for funds
        if "entry" not in _live_kinds():
            return False  # (the preview screen asks for funds too)
        phases["buy_reads_done"].set()
        phases["panic_closed"].wait(20)

    def close_went_through(_a):
        if "exit" not in _live_kinds():
            return False
        phases["panic_closed"].set()

    acct.hooks += [
        {"m": "order_report", "w": "before", "nth": acct.counts.get("order_report", 0) + 1, "fn": start_buy_from_the_panic, "done": False},
        {"m": "limits", "w": "before", "nth": acct.counts.get("limits", 0) + 1, "fn": buy_waits_for_the_close, "done": False},
        {"m": "place_order", "w": "after", "nth": acct.counts.get("place_order", 0) + 1, "fn": close_went_through, "done": False},
    ]
    r = w.api("post", "/api/automation/square-off-all")
    assert r.status_code == 200
    w.wait_idle()
    for ac in w.accts:
        ac.hooks.clear()
    w.step_no = 2
    w.op_quiet({}, None)
    w.drain()
    w.after({})
    w.check_records()
    assert not [v for v in w.violations if v["kind"].startswith("RECORDS_")], summarize(w)


def test_G5_two_true_ups_at_once_write_a_closed_position_off_once(env):
    spec = mini_spec(sections=["fno"], max_lots=2, profile="derivs",
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     steps=[_step("buy", 0, "10:00", sec="fno", sub=False),
                            _step("holder", 0, "11:00", act="takeover", u2=0.999)])
    reset_state(env.clock)
    env.sim.accounts.clear()
    w = World(env.client, env.clock, env.sim, spec)
    w.skip_known_races = False
    w.setup()
    for i, st in enumerate(spec["steps"]):
        w.step_no = i
        w.do(st)
        w.wait_idle()
    w.go(0, "15:40")
    acct = w.accts[0]
    held = [k for k, v in acct.own.items() if v[0]]
    assert not held, "the holder's takeover should have closed what TradeLab bought"
    barrier = threading.Barrier(2, timeout=20)
    acct.hooks += [{"m": "positions", "w": "before", "nth": acct.counts.get("positions", 0) + n, "fn": lambda _a: barrier.wait(),
                    "done": False} for n in (1, 2)]
    # (the scheduler's 15:40 job and, say, a second job or a Sell at the same moment: both read TradeLab's records first, then
    # Kotak's positions)
    threads = [threading.Thread(target=trading.true_up_all) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    acct.hooks.clear()
    w.step_no = 3
    w.check_records()
    with session_scope() as db:
        rows = [(r.action, r.quantity) for r in db.find(OrderLog, {"status": "adjusted"}, track=False)]
    assert len(rows) == 1 and not [v for v in w.violations if v["kind"].startswith("RECORDS_")], \
        f"adjusted rows {rows}; {summarize(w)}"


# ===================================================================================== round 4 (docs/audit/round4/G-random-scenarios.md)


def test_R4_a_partly_closed_contract_whose_units_times_lot_equal_the_opened_units_is_still_closed(env):
    """Seed 350, shrunk. TradeLab buys 10 lots SILVERM (50 units, 5 to a lot); the holder (or Kotak) closes 8 lots.
    Kotak shows 10 units (2 lots), a real position. 10 * 5 == 50, so `_units_doubt` takes it for a lot count:
    the records stay at 50 for ever and Sell / Exit / panic send nothing ("can't be a real position")."""
    spec = mini_spec(sections=["commodity"], max_lots=30,
                     items={"intraday": [("SBIN", "B")], "holding": [ETFS[0]], "fno": [FNO_CHOICES[0]],
                            "commodity": [("silverm", "B")]},
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     steps=[_step("buy", 0, "15:10", sec="commodity", sub=False),
                            _step("holder", 0, "21:00", act="takeover", u2=0.757),
                            _step("quiet", 0, "21:10"),
                            _step("sell", 0, "21:30", sec="commodity", sub=False, opened=None)])
    w = fresh_run(env, 7, spec)
    assert not w.violations, summarize(w)
    assert w.last["kind"] == "sell" and w.last["run"].status == "done", summarize(w)


def _unknown_close_spec():
    return mini_spec(sections=["commodity"], no_echo=["mcx_fo"], max_lots=2,
                     accounts=[{"cash": 168000.0, "demat": {}, "mis": 0, "nrml": {}}],
                     steps=[_step("buy", 0, "10:00", sec="commodity", sub=False),
                            _step("fault", 0, "10:50", m="place_order", kind="lost", n=1, skip=0),
                            _step("toggle", 0, "10:55", what="poslag"),
                            _step("sell", 0, "11:00", sec="commodity", sub=False, opened=None),
                            _step("sell", 0, "11:10", sec="commodity", sub=False, opened=None)])


def test_R4_a_close_with_a_lost_reply_is_never_sent_twice_nor_called_closed(env):
    """Guards `unresolved_closes` (mutant ignore_unknown_closes): the second Sell and the panic button must neither
    resend the close (OVERSOLD / TL_FLIP / duplicate tag) nor report done while the first close has no known outcome."""
    w = fresh_run(env, 7, _unknown_close_spec())
    assert not w.violations, summarize(w)
    assert w.last["kind"] == "sell" and w.last["run"].status != "done", summarize(w)  # first close: outcome unknown

