"""NSE equity instrument list (Kotak Neo scrip master) with fast in-memory search."""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx

from ..config import IST, get_settings

log = logging.getLogger(__name__)


@dataclass
class Instrument:
    symbol: str
    trading_symbol: str
    name: str
    token: str | None
    exchange_segment: str = "nse_cm"
    series: str = "EQ"
    isin: str | None = None


# Shown only until the full list can be downloaded with a connected Kotak account.
_BUILTIN = [
    ("RELIANCE", "Reliance Industries"), ("TCS", "Tata Consultancy Services"), ("HDFCBANK", "HDFC Bank"),
    ("ICICIBANK", "ICICI Bank"), ("INFY", "Infosys"), ("SBIN", "State Bank of India"),
    ("BHARTIARTL", "Bharti Airtel"), ("ITC", "ITC"), ("LT", "Larsen & Toubro"),
    ("KOTAKBANK", "Kotak Mahindra Bank"), ("AXISBANK", "Axis Bank"), ("HINDUNILVR", "Hindustan Unilever"),
    ("BAJFINANCE", "Bajaj Finance"), ("BAJAJFINSV", "Bajaj Finserv"), ("BAJAJ-AUTO", "Bajaj Auto"),
    ("MARUTI", "Maruti Suzuki India"), ("M&M", "Mahindra & Mahindra"), ("SUNPHARMA", "Sun Pharmaceutical"),
    ("TITAN", "Titan Company"), ("ULTRACEMCO", "UltraTech Cement"), ("ASIANPAINT", "Asian Paints"),
    ("NTPC", "NTPC"), ("POWERGRID", "Power Grid Corporation"), ("ONGC", "Oil & Natural Gas Corporation"),
    ("COALINDIA", "Coal India"), ("TATASTEEL", "Tata Steel"), ("JSWSTEEL", "JSW Steel"),
    ("HINDALCO", "Hindalco Industries"), ("HCLTECH", "HCL Technologies"), ("WIPRO", "Wipro"),
    ("TECHM", "Tech Mahindra"), ("ADANIENT", "Adani Enterprises"), ("ADANIPORTS", "Adani Ports & SEZ"),
    ("NESTLEIND", "Nestle India"), ("DRREDDY", "Dr. Reddy's Laboratories"), ("CIPLA", "Cipla"),
    ("GRASIM", "Grasim Industries"), ("EICHERMOT", "Eicher Motors"), ("BEL", "Bharat Electronics"),
    ("TRENT", "Trent"), ("APOLLOHOSP", "Apollo Hospitals"), ("HDFCLIFE", "HDFC Life Insurance"),
    ("SBILIFE", "SBI Life Insurance"), ("TATACONSUM", "Tata Consumer Products"),
    ("SHRIRAMFIN", "Shriram Finance"), ("JIOFIN", "Jio Financial Services"), ("ETERNAL", "Eternal"),
    ("IRCTC", "Indian Railway Catering & Tourism"), ("DLF", "DLF"), ("VEDL", "Vedanta"),
    ("TATAPOWER", "Tata Power"), ("PNB", "Punjab National Bank"), ("BANKBARODA", "Bank of Baroda"),
    ("YESBANK", "Yes Bank"), ("IDEA", "Vodafone Idea"), ("IRFC", "Indian Railway Finance Corporation"),
    ("HAL", "Hindustan Aeronautics"), ("BPCL", "Bharat Petroleum"), ("IOC", "Indian Oil Corporation"),
    ("GAIL", "GAIL (India)"), ("SAIL", "Steel Authority of India"), ("ZYDUSLIFE", "Zydus Lifesciences"),
]


def _col(row: dict[str, str], *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def parse_scrip_csv(content: bytes, exchange_segment: str = "nse_cm") -> list[Instrument]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    # Kotak headers sometimes carry stray spaces or a trailing ';'.
    reader.fieldnames = [f.strip().rstrip(";") for f in (reader.fieldnames or [])]
    out: list[Instrument] = []
    seen: set[str] = set()
    for row in reader:
        trading_symbol = _col(row, "pTrdSymbol", "pTrdSym")
        token = _col(row, "pSymbol", "pToken")
        if not trading_symbol or not token:
            continue
        series = _col(row, "pGroup", "pSeries")
        if not series and "-" in trading_symbol:
            series = trading_symbol.rsplit("-", 1)[1]
        if series.upper() != "EQ":
            continue
        symbol = _col(row, "pSymbolName") or trading_symbol.rsplit("-", 1)[0]
        if trading_symbol in seen:
            continue
        seen.add(trading_symbol)
        out.append(
            Instrument(
                symbol=symbol.upper(),
                trading_symbol=trading_symbol,
                name=_col(row, "pDesc", "pDescription", "pCompanyName") or symbol,
                token=token.split(".")[0],
                exchange_segment=exchange_segment,
                series="EQ",
                isin=_col(row, "pISIN") or None,
            )
        )
    return out


class ScripMaster:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.instruments: list[Instrument] = [
            Instrument(symbol=s, trading_symbol=f"{s}-EQ", name=n, token=None) for s, n in _BUILTIN
        ]
        self.source = "builtin"
        self.loaded_on: str | None = None
        self.last_error: str | None = None
        self._last_attempt = 0.0
        self._by_trading_symbol: dict[str, Instrument] = {}
        self._reindex()

    def _reindex(self) -> None:
        self._by_trading_symbol = {i.trading_symbol: i for i in self.instruments}

    def _cache_path(self, day: str):
        d = get_settings().data_dir / "scrips"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"nse_cm-{day}.json"

    def _load_from_disk(self, today: str) -> bool:
        files = sorted((get_settings().data_dir / "scrips").glob("nse_cm-*.json"), reverse=True)
        for f in files:
            try:
                rows = json.loads(f.read_text())
                self.instruments = [Instrument(**r) for r in rows]
                self.source = "kotak"
                self.loaded_on = f.stem.removeprefix("nse_cm-")
                self._reindex()
                return self.loaded_on == today
            except Exception:  # noqa: BLE001 - corrupted cache, keep looking
                continue
        return False

    def ensure_loaded(self, force: bool = False) -> None:
        today = datetime.now(IST).date().isoformat()
        if not force and self.source == "kotak" and self.loaded_on == today:
            return
        with self._lock:
            if not force and self._load_from_disk(today):
                return
            # Don't hammer the broker when downloads keep failing.
            if not force and time.monotonic() - self._last_attempt < 600 and self._last_attempt:
                return
            self._last_attempt = time.monotonic()
            from ..broker.manager import broker_manager

            client = broker_manager.market_data_client()
            if client is None:
                self.last_error = "Add a Kotak Neo account to load the full NSE stock list."
                return
            try:
                url = client.scrip_master_url("nse_cm")
                resp = httpx.get(url, timeout=60, follow_redirects=True)
                resp.raise_for_status()
                instruments = parse_scrip_csv(resp.content)
                if len(instruments) < 100:
                    raise ValueError(f"scrip master looks incomplete ({len(instruments)} rows)")
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Could not download NSE stock list: {exc}"
                log.warning(self.last_error)
                return
            self.instruments = instruments
            self.source = "kotak"
            self.loaded_on = today
            self.last_error = None
            self._reindex()
            self._cache_path(today).write_text(json.dumps([asdict(i) for i in instruments]))
            for old in sorted(self._cache_path(today).parent.glob("nse_cm-*.json"))[:-3]:
                old.unlink(missing_ok=True)

    def token_for(self, trading_symbol: str) -> str | None:
        inst = self._by_trading_symbol.get(trading_symbol)
        return inst.token if inst else None

    def search(self, query: str, limit: int = 40) -> list[Instrument]:
        q = query.strip().upper()
        if not q:
            return self.instruments[:limit] if self.source == "builtin" else []
        scored: list[tuple[int, int, Instrument]] = []
        for inst in self.instruments:
            name = inst.name.upper()
            if inst.symbol == q:
                score = 0
            elif inst.symbol.startswith(q):
                score = 1
            elif name.startswith(q) or f" {q}" in f" {name}":
                score = 2
            elif q in inst.symbol:
                score = 3
            elif q in name:
                score = 4
            else:
                continue
            scored.append((score, len(inst.symbol), inst))
        scored.sort(key=lambda t: (t[0], t[1], t[2].symbol))
        return [t[2] for t in scored[:limit]]

    def status(self) -> dict:
        return {
            "source": self.source,
            "count": len(self.instruments),
            "loaded_on": self.loaded_on,
            "message": self.last_error if self.source != "kotak" else None,
        }


scrip_master = ScripMaster()
