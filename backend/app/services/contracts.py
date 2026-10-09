"""Futures and options contract lists (Kotak Neo scrip master for nse_fo, bse_fo, mcx_fo) with search.

Without a connected Kotak account a small sample list is served, so paper accounts can
try the F&O and Commodity sections.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from ..config import IST, get_settings
from ..money import inr

log = logging.getLogger(__name__)

SEGMENTS = ("nse_fo", "bse_fo", "mcx_fo")
# nse_fo expiries count seconds from 1980-01-01; bse_fo and mcx_fo use plain Unix time.
NSE_EPOCH_OFFSET = 315511200
# BSE names its instrument types differently.
_TYPE_ALIASES = {"IO": "OPTIDX", "IF": "FUTIDX", "SO": "OPTSTK", "SF": "FUTSTK"}
_TRADABLE = {
    "nse_fo": {"OPTIDX", "FUTIDX", "OPTSTK", "FUTSTK"},
    "bse_fo": {"OPTIDX", "FUTIDX", "OPTSTK", "FUTSTK"},
    "mcx_fo": {"FUTCOM", "OPTFUT", "FUTIDX", "OPTIDX"},  # COM (spot) and IN (index) rows can't be traded
}
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_WORDS = {m: i + 1 for i, m in enumerate(_MONTHS)} | {
    w: i + 1 for i, w in enumerate(("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
                                    "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"))}
SAMPLE_NOTE = "Sample contracts — connect a Kotak Neo account for the full list."


@dataclass
class Contract:
    exchange_segment: str
    trading_symbol: str
    token: str
    symbol: str                 # underlying, e.g. NIFTY, GOLDM
    instrument_type: str        # OPTIDX, FUTIDX, OPTSTK, FUTSTK, FUTCOM, OPTFUT
    option_type: str | None     # CE / PE; None for futures
    strike: float | None        # rupees
    expiry: date | None
    lot_size: int               # order units per lot (quantity = lots x lot_size)
    tick_size: float = 0.05     # rupees
    multiplier: float = 1.0     # price units per order unit (MCX: lMultiplier x dGenNum / dGenDen)
    sample: bool = False

    @property
    def is_option(self) -> bool:
        return self.option_type in ("CE", "PE")

    @property
    def is_future(self) -> bool:
        return not self.is_option

    @property
    def kind(self) -> str:
        return "option" if self.is_option else "future"

    @property
    def display(self) -> str:
        """Short label, e.g. "NIFTY 27 OCT 25000 CE" or "GOLDM 05 NOV FUT"."""
        when = f" {self.expiry.day:02d} {_MONTHS[self.expiry.month - 1]}" if self.expiry else ""
        if self.is_option:
            return f"{self.symbol}{when} {_num(self.strike)} {self.option_type}"
        return f"{self.symbol}{when} FUT"

    @property
    def description(self) -> str:
        parts = [f"{self.symbol} {'option' if self.is_option else 'future'}"]
        if self.expiry:
            parts.append(f"{self.expiry.day} {_MONTHS[self.expiry.month - 1].title()} {self.expiry.year}")
        if self.is_option:
            parts.append(f"strike {inr(self.strike or 0, 0 if (self.strike or 0) == int(self.strike or 0) else 2)}")
            parts.append("Call" if self.option_type == "CE" else "Put")
        parts.append(f"lot {self.lot_size}")
        return " · ".join(parts)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat() if self.expiry else None
        d.update(kind=self.kind, display=self.display, description=self.description)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Contract:
        fields_ = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        if fields_.get("expiry"):
            fields_["expiry"] = date.fromisoformat(fields_["expiry"])
        return cls(**fields_)


def _today() -> date:
    return datetime.now(IST).date()


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value == int(value) else f"{value:g}"


def _col(row: dict[str, str], *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def decode_expiry(raw: str, exchange_segment: str) -> date | None:
    """Expiry date from lExpiryDate (values <= 0 mean no expiry). Kotak stamps nse_fo at 14:30
    UTC and MCX at 23:59:59 UTC, so the UTC date is the expiry date."""
    value = _float(raw, -1)
    if value <= 0:
        return None
    if exchange_segment == "nse_fo":
        value += NSE_EPOCH_OFFSET
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def parse_contract_csv(content: bytes, exchange_segment: str, today: date | None = None) -> list[Contract]:
    today = today or _today()
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    # Kotak headers carry stray spaces and a trailing ';' (e.g. "dStrikePrice;", "lExpiryDate ").
    reader.fieldnames = [f.strip().rstrip(";").strip() for f in (reader.fieldnames or [])]
    allowed = _TRADABLE.get(exchange_segment, set())
    out: list[Contract] = []
    seen: set[str] = set()
    for row in reader:
        token = _col(row, "pSymbol").split(".")[0]
        trading_symbol = _col(row, "pTrdSymbol")
        if not token.isdigit() or not trading_symbol or trading_symbol in seen:
            continue  # calendar-spread rows carry two tokens ("53032 72263")
        inst = _col(row, "pInstType").upper()
        inst = _TYPE_ALIASES.get(inst, inst)
        if inst not in allowed:
            continue
        expiry = decode_expiry(_col(row, "lExpiryDate", "pExpiryDate"), exchange_segment)
        if expiry is None or expiry < today:
            continue
        opt = _col(row, "pOptionType").upper()
        option_type = opt if opt in ("CE", "PE") else None
        if inst.startswith("OPT") and option_type is None:
            continue
        strike = _float(_col(row, "dStrikePrice"), -1)
        lot = int(_float(_col(row, "lLotSize", "iLotSize"), 1))
        mult = _float(_col(row, "lMultiplier"), -1)
        num, den = _float(_col(row, "dGenNum"), 1) or 1, _float(_col(row, "dGenDen"), 1) or 1
        seen.add(trading_symbol)
        out.append(Contract(
            exchange_segment=exchange_segment,
            trading_symbol=trading_symbol,
            token=token,
            symbol=(_col(row, "pSymbolName") or re.split(r"\d", trading_symbol, maxsplit=1)[0]).upper(),
            instrument_type=inst,
            option_type=option_type,
            strike=strike / 100 if option_type and strike > 0 else None,
            expiry=expiry,
            lot_size=max(1, lot),
            tick_size=(_float(_col(row, "dTickSize"), 5) or 5) / 100,
            multiplier=round((mult if mult > 0 else 1) * num / den, 6),
        ))
    return out


# ----------------------------------------------------------------------------- sample list


def _weekday_on_or_before(d: date, weekday: int) -> date:
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    return _weekday_on_or_before(date(year, month, monthrange(year, month)[1]), weekday)


def _business_day_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _months(today: date, n: int) -> list[tuple[int, int]]:
    out, y, m = [], today.year, today.month
    for _ in range(n + 1):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def sample_contracts(today: date | None = None) -> dict[str, list[Contract]]:
    """A small, realistic-looking list for paper trading (expiries move with the calendar)."""
    today = today or _today()
    seq = iter(range(100001, 999999))
    out: dict[str, list[Contract]] = {seg: [] for seg in SEGMENTS}

    def add(seg: str, sym: str, inst: str, expiry: date, lot: int, opt: str | None = None,
            strike: float | None = None, mult: float = 1.0) -> None:
        if expiry < today:
            return
        tag = f"{expiry:%y}{_MONTHS[expiry.month - 1]}"
        ts = f"{sym}{tag}{expiry:%d}{_num(strike)}{opt}" if opt else f"{sym}{tag}FUT"
        out[seg].append(Contract(seg, ts, f"S{next(seq)}", sym, inst, opt, strike, expiry, lot, 0.05, mult, True))

    def monthly(weekday: int, count: int = 3) -> list[date]:
        days = [_last_weekday(y, m, weekday) for y, m in _months(today, count)]
        return [d for d in days if d >= today][:count]

    def weekly(weekday: int, count: int = 2) -> list[date]:
        first = today + timedelta(days=(weekday - today.weekday()) % 7)
        return [first + timedelta(weeks=i) for i in range(count)]

    nse_monthly = monthly(1)  # NSE: last Tuesday of the month
    for sym, lot in (("NIFTY", 65), ("BANKNIFTY", 30), ("FINNIFTY", 60)):
        for d in nse_monthly:
            add("nse_fo", sym, "FUTIDX", d, lot)
    for d in weekly(1):
        for k in range(24000, 26001, 100):
            for opt in ("CE", "PE"):
                add("nse_fo", "NIFTY", "OPTIDX", d, 65, opt, float(k))
    for k in range(54000, 58001, 500):
        for opt in ("CE", "PE"):
            add("nse_fo", "BANKNIFTY", "OPTIDX", nse_monthly[0], 30, opt, float(k))
    for sym, lot in (("RELIANCE", 500), ("HDFCBANK", 550), ("TCS", 175), ("INFY", 400), ("SBIN", 750)):
        for d in nse_monthly:
            add("nse_fo", sym, "FUTSTK", d, lot)
    for d in monthly(3):  # BSE: Thursdays
        add("bse_fo", "SENSEX", "FUTIDX", d, 20)
    for d in weekly(3):
        for k in range(80000, 84001, 500):
            for opt in ("CE", "PE"):
                add("bse_fo", "SENSEX", "OPTIDX", d, 20, opt, float(k))
    # MCX: Kotak's units per lot (lLotSize) and price multipliers as seen in its mcx_fo file.
    mcx = (("GOLD", 1, 100.0, 5), ("GOLDM", 100, 0.1, 5), ("SILVER", 30, 1.0, 0), ("SILVERM", 5, 1.0, 0),
           ("CRUDEOIL", 100, 1.0, 19), ("CRUDEOILM", 10, 1.0, 19), ("NATURALGAS", 1250, 1.0, 26),
           ("COPPER", 2500, 1.0, 0))
    for sym, lot, mult, day_of_month in mcx:
        for y, m in _months(today, 3):
            d = date(y, m, day_of_month) if day_of_month else date(y, m, monthrange(y, m)[1])
            add("mcx_fo", sym, "FUTCOM", _business_day_on_or_before(d), lot, mult=mult)
    for seg in SEGMENTS:
        out[seg].sort(key=lambda c: (c.symbol, c.expiry, c.option_type or "", c.strike or 0))
    return out


# ----------------------------------------------------------------------------- the master


@dataclass
class _Segment:
    contracts: list[Contract] = field(default_factory=list)
    by_symbol: dict[str, Contract] = field(default_factory=dict)
    by_underlying: dict[str, list[Contract]] = field(default_factory=dict)
    source: str = "none"  # kotak | sample | none
    loaded_on: str | None = None
    last_error: str | None = None
    last_attempt: float = 0.0


class ContractMaster:
    def __init__(self) -> None:
        self._locks = {seg: threading.Lock() for seg in SEGMENTS}
        self._segs = {seg: _Segment() for seg in SEGMENTS}

    def _cache_dir(self):
        d = get_settings().data_dir / "contracts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _set(self, seg: str, contracts: list[Contract], source: str, loaded_on: str | None) -> None:
        s = self._segs[seg]
        by_und: dict[str, list[Contract]] = {}
        for c in contracts:
            by_und.setdefault(c.symbol, []).append(c)
        for rows in by_und.values():
            rows.sort(key=lambda c: (c.expiry or date.max, c.is_option, c.strike or 0, c.option_type or ""))
        s.contracts, s.by_symbol, s.by_underlying = contracts, {c.trading_symbol: c for c in contracts}, by_und
        s.source, s.loaded_on = source, loaded_on

    def _load_from_disk(self, seg: str, today: str) -> bool:
        files = sorted(self._cache_dir().glob(f"{seg}-*.json"), reverse=True)
        for f in files:
            try:
                rows = json.loads(f.read_text())
                self._set(seg, [Contract.from_dict(r) for r in rows], "kotak", f.stem.removeprefix(f"{seg}-"))
                return self._segs[seg].loaded_on == today
            except Exception:  # noqa: BLE001 - corrupted cache, keep looking
                continue
        return False

    def ensure_loaded(self, segment: str, force: bool = False) -> None:
        if segment not in self._segs:
            raise ValueError(f"Unknown segment {segment!r}")
        s = self._segs[segment]
        today = _today().isoformat()
        if not force and s.source == "kotak" and s.loaded_on == today:
            return
        with self._locks[segment]:
            if not force and s.source == "kotak" and s.loaded_on == today:
                return
            if not force and s.source != "kotak" and self._load_from_disk(segment, today):
                return
            from ..broker.manager import broker_manager

            client = broker_manager.market_data_client()
            if client is None:
                if s.source != "kotak" and (s.source != "sample" or s.loaded_on != today):
                    self._set(segment, sample_contracts()[segment], "sample", today)  # no account: the sample
                    s.last_error = SAMPLE_NOTE
                return
            # Don't hammer the broker when downloads keep failing.
            if not force and s.last_attempt and time.monotonic() - s.last_attempt < 600:
                return
            s.last_attempt = time.monotonic()
            try:
                url = client.scrip_master_url(segment)
                resp = httpx.get(url, timeout=120, follow_redirects=True)
                resp.raise_for_status()
                contracts = parse_contract_csv(resp.content, segment)
                if len(contracts) < 10:
                    raise ValueError(f"contract list looks incomplete ({len(contracts)} rows)")
            except Exception as exc:  # noqa: BLE001
                s.last_error = f"Could not download the {segment} contract list: {exc}"
                log.warning(s.last_error)
                if s.source == "none":
                    self._set(segment, sample_contracts()[segment], "sample", today)
                return
            self._set(segment, contracts, "kotak", today)
            s.last_error = None
            path = self._cache_dir() / f"{segment}-{today}.json"
            path.write_text(json.dumps([c.to_dict() for c in contracts]))
            for old in sorted(self._cache_dir().glob(f"{segment}-*.json"))[:-3]:
                old.unlink(missing_ok=True)

    def get(self, segment: str, trading_symbol: str) -> Contract | None:
        """A contract that can still be traded (unknown or expired: None)."""
        if segment not in self._segs:
            return None
        self.ensure_loaded(segment)
        c = self._segs[segment].by_symbol.get(trading_symbol)
        if c is None or (c.expiry and c.expiry < _today()):
            return None
        return c

    def search(self, query: str, segments: tuple[str, ...] | list[str] | None = None, kind: str | None = None,
               limit: int = 40) -> list[Contract]:
        """Contracts matching every word of `query`: underlying ("nifty", "gold"), strike ("25000"),
        CE/PE/FUT, month ("oct") or expiry day ("28"). Nearest expiry first."""
        segments = [s for s in (segments or SEGMENTS) if s in self._segs]
        for seg in segments:
            self.ensure_loaded(seg)
        words = [w for w in re.split(r"[\s,/]+", query.strip().upper()) if w]
        opt, fut, months, numbers, texts = None, False, set(), [], []
        for w in words:
            if w in ("CE", "CALL", "CALLS"):
                opt = "CE"
            elif w in ("PE", "PUT", "PUTS"):
                opt = "PE"
            elif w in ("FUT", "FUTS", "FUTURE", "FUTURES"):
                fut = True
            elif w in _MONTH_WORDS:
                months.add(_MONTH_WORDS[w])
            elif re.fullmatch(r"\d+(\.\d+)?", w):
                numbers.append(float(w))
            else:
                texts.append(w)
        if kind == "future":
            fut = True
        today = _today()
        scored: list[tuple[tuple, Contract]] = []
        for seg in segments:
            s = self._segs[seg]
            for und, rows in s.by_underlying.items():
                if texts:
                    head = texts[0]
                    if und == head:
                        u_score = 0
                    elif und.startswith(head):
                        u_score = 1
                    elif head in und:
                        u_score = 2
                    else:
                        continue
                    rest = texts[1:]
                else:
                    u_score, rest = 3, []
                for c in rows:
                    if c.expiry and c.expiry < today:
                        continue
                    if (fut or kind == "future") and c.is_option:
                        continue
                    if (opt or kind == "option") and not c.is_option:
                        continue
                    if opt and c.option_type != opt:
                        continue
                    if months and (c.expiry is None or c.expiry.month not in months):
                        continue
                    ok = True
                    for n in numbers:
                        if not ((c.strike is not None and abs(c.strike - n) < 1e-6)
                                or (n <= 31 and n == int(n) and c.expiry and c.expiry.day == int(n))):
                            ok = False
                            break
                    if not ok or any(r not in c.trading_symbol for r in rest):
                        continue
                    wants_option = bool(opt or numbers or kind == "option")
                    kind_rank = 0 if c.is_option == wants_option else 1
                    scored.append(((u_score, len(und), kind_rank, c.expiry or date.max, c.strike or 0,
                                    c.option_type or "", c.trading_symbol), c))
        scored.sort(key=lambda t: t[0])
        return [c for _, c in scored[:limit]]

    def status(self, segment: str | None = None) -> dict:
        def one(seg: str) -> dict:
            s = self._segs[seg]
            return {"source": s.source, "count": len(s.contracts), "loaded_on": s.loaded_on,
                    "message": s.last_error if s.source != "kotak" else None}

        if segment is not None:
            return one(segment)
        return {seg: one(seg) for seg in SEGMENTS}


contract_master = ContractMaster()
