"""Index ETFs on NSE for the Holding section: a built-in list, merged with the live Kotak list once it is loaded."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass

from .etf_data import CORE, INDEX_ETFS, NON_INDEX_SYMBOLS
from .scrips import Instrument, scrip_master

log = logging.getLogger(__name__)

# Display order of the groups.
GROUPS: tuple[str, ...] = (
    "Broad market", "Nifty Next 50", "Midcap & Smallcap", "Bank & Financials", "Sensex", "Sector & Theme",
    "Factor & Strategy", "International",
)

# Gold, silver, liquid, debt... anything that is not an equity index fund. Checked on the symbol and on the name.
NON_INDEX = re.compile(
    r"GOLD|SILV|LIQ|1\s?D\s?RATE|OVERNIGHT|MONEY\s?MARKET|CASH|GILT|G-?\s?SEC(?!TOR)|\bSDL|SDL\d|BOND|TREASURY|"
    r"T-?BILL|DEBT|CRISIL|HYBRID|PHYSICAL|DOMESTIC\s?PRICE|ARBITRAGE|GOVERNMENT\s?SECURITIES|\bAAA|^COMMODITY\b",
    re.IGNORECASE,
)
# Bharat Bond ETFs: BBETF0432, EBBETF0433...
_BHARAT_BOND = re.compile(r"^E?BBETF\d{4}$")
# Equity index funds whose names trip the rule (the CPSE ETF used to be "Goldman Sachs CPSE")...
FORCE_INDEX = frozenset({"COMMOIETF", "MIDSELIETF", "CPSEETF", "ICICIB22", "ESG"})
# ...and debt or mixed funds whose names don't.
FORCE_NON_INDEX = frozenset({"AXISBPSETF", "LICNETFGSC", "HYBRIDETF"})

# Best-guess group for an ETF found only in the live Kotak list. First match wins; checked on name + symbol.
_GROUP_RULES = [
    ("International", r"NASDAQ|FANG|S&P500|HANGSENG"),
    ("Bank & Financials", r"BANK|FINANC|BFSI|CAPITAL|INSUR|PVTBAN|PSUBNK|PSUBK"),
    ("Sensex", r"SENSEX"),
    ("Nifty Next 50", r"NEXT50|JUNIOR"),
    ("Midcap & Smallcap", r"MID|SMALL|SML"),
    ("Factor & Strategy", r"MOMENTUM|QUALITY|VALUE|VOL|ALPHA|EQUAL|DIVIDEND|DIVOPP|MULTICAP|FLEXI|IPO|MNC|SHARIAH|"
                          r"ESG|GROWTH|NV20|LEADERS|LARGEMID|TOP1|TOP2|QLTY|MQ"),
    ("Sector & Theme", r"IT|TECH|PHARMA|HEALTH|HOSPITAL|AUTO|EV|FMCG|CONSUM|INFRA|MANUFACT|ENERGY|OIL|POWER|METAL|"
                       r"CHEMICAL|CEMENT|COMMODIT|REALTY|DEFENCE|RAIL|PSE|BHARAT22|DIGITAL|INTERNET|TOURISM|SERVICES|"
                       r"SECTOR"),
]
_GROUP_RES = [(name, re.compile(pat)) for name, pat in _GROUP_RULES]

_CORE_RANK = {s: i for i, s in enumerate(CORE)}
_GROUP_RANK = {g: i for i, g in enumerate(GROUPS)}
# A loaded Kotak list that matches fewer built-in ETFs than this is treated as broken and ignored.
_MIN_LIVE_MATCHES = 100


@dataclass(frozen=True)
class Etf:
    symbol: str
    trading_symbol: str
    name: str
    isin: str | None
    group: str
    core: bool = False
    token: str | None = None
    exchange_segment: str = "nse_cm"

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper().removesuffix("-EQ")


def looks_like_index_etf(symbol: str, text: str = "", isin: str | None = None) -> bool:
    """The rule alone: an equity index ETF, not gold/silver/liquid/debt. With an ISIN, it must be a fund's (INF...)."""
    sym = _clean_symbol(symbol)
    if isin is not None and not isin.strip().upper().startswith("INF"):
        return False
    if sym in FORCE_INDEX:
        return True
    if sym in FORCE_NON_INDEX or _BHARAT_BOND.match(sym):
        return False
    return not (NON_INDEX.search(sym) or (text and NON_INDEX.search(text)))


def guess_group(symbol: str, name: str = "") -> str:
    # Kotak descriptions start with the fund house ("MIRAEAMC - DEFENCE"); that part says nothing about the index.
    name = re.sub(r"^[^-]*AM[CL]\s*-\s*", "", name or "")
    key = f"{name} {symbol}".upper().replace(" ", "")
    for group, rx in _GROUP_RES:
        if rx.search(key):
            return group
    return "Broad market"


def _ordered(ranked: list[tuple[int, Etf]]) -> list[Etf]:
    # Core first in CORE order, then by group, then symbol.
    ranked.sort(key=lambda t: (t[0], _GROUP_RANK.get(t[1].group, len(GROUPS)), t[1].symbol))
    return [e for _, e in ranked]


def _builtin(sm) -> list[Etf]:
    return _ordered([
        (_CORE_RANK.get(s, len(CORE)), Etf(symbol=s, trading_symbol=f"{s}-EQ", name=name, isin=isin, group=group,
                                           core=s in _CORE_RANK, token=sm.token_for(f"{s}-EQ")))
        for s, name, isin, group in INDEX_ETFS
    ])


def _merge(sm, instruments: list[Instrument]) -> list[Etf]:
    if sm.source != "kotak":
        return _builtin(sm)
    by_symbol = {row[0]: row for row in INDEX_ETFS}
    by_isin = {row[2]: row for row in INDEX_ETFS}
    candidates: list[tuple[str, str, Instrument]] = []
    matched: dict[str, Instrument] = {}  # built-in symbol -> live row
    for inst in instruments:
        if (inst.series or "EQ").upper() != "EQ" or inst.exchange_segment != "nse_cm":
            continue
        sym = inst.symbol.strip().upper()
        isin = (inst.isin or "").strip().upper()
        if isin and not isin.startswith("INF"):
            continue  # a company's shares, not a fund
        if sym in by_symbol:
            matched[sym] = inst
        else:
            candidates.append((sym, isin, inst))
    ranked: list[tuple[int, Etf]] = []
    for sym, isin, inst in candidates:
        row = by_isin.get(isin) if isin else None
        if row is not None:
            # Same fund under a new symbol (renamed); keep our readable name. A symbol match always wins.
            matched.setdefault(row[0], inst)
        elif isin and sym not in NON_INDEX_SYMBOLS and looks_like_index_etf(sym, inst.name, isin):
            ranked.append((len(CORE), Etf(symbol=sym, trading_symbol=inst.trading_symbol, name=inst.name or sym,
                                          isin=isin, group=guess_group(sym, inst.name), token=inst.token)))
    if len(matched) < _MIN_LIVE_MATCHES:
        # Kotak's file changed shape or came back half empty: better the built-in list than an empty picker.
        log.warning("Kotak list matched only %d index ETFs; showing the built-in list", len(matched))
        return _builtin(sm)
    for s, name, isin, group in INDEX_ETFS:
        inst = matched.get(s)
        if inst is None:
            continue  # delisted or renamed away: not tradable any more
        # A renamed core ETF keeps its place in the core order under its new symbol.
        ranked.append((_CORE_RANK.get(s, len(CORE)), Etf(
            symbol=inst.symbol.strip().upper(), trading_symbol=inst.trading_symbol, name=name,
            isin=inst.isin or isin, group=group, core=s in _CORE_RANK, token=inst.token)))
    return _ordered(ranked)


class EtfCatalogue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (cache key, sorted list, symbol -> Etf), swapped in one go so readers never see a half-built state.
        self._state: tuple[tuple | None, list[Etf], dict[str, Etf]] = (None, [], {})

    def _current(self) -> tuple[list[Etf], dict[str, Etf]]:
        sm = scrip_master
        instruments = sm.instruments
        # Rebuilt only when the scrip list changes (new day's download, builtin -> kotak).
        key = (sm.source, sm.loaded_on, len(instruments), id(instruments))
        state = self._state
        if state[0] != key:
            with self._lock:
                state = self._state
                if state[0] != key:
                    etfs = _merge(sm, instruments)
                    state = (key, etfs, {e.symbol: e for e in etfs})
                    self._state = state
        return state[1], state[2]

    def all(self) -> list[Etf]:
        return list(self._current()[0])

    def find(self, symbol: str) -> Etf | None:
        return self._current()[1].get(_clean_symbol(symbol))

    def search(self, query: str, limit: int = 60) -> list[Etf]:
        etfs = self._current()[0]
        q = _clean_symbol(" ".join((query or "").split()))
        if not q:
            return etfs[:limit]  # core first, then by group
        compact = q.replace(" ", "")
        scored: list[tuple[int, int, int, str, Etf]] = []
        for e in etfs:
            name = e.name.upper()
            group = e.group.upper()
            if e.symbol == q:
                score = 0
            elif e.symbol.startswith(q):
                score = 1
            elif name.startswith(q) or f" {q}" in f" {name}":
                score = 2
            elif q in e.symbol:
                score = 3
            elif q in name or compact in name.replace(" ", "") or f" {q}" in f" {group}":
                score = 4
            elif q in group:
                score = 5
            else:
                continue
            scored.append((score, _CORE_RANK.get(e.symbol, len(CORE)), len(e.symbol), e.symbol, e))
        scored.sort(key=lambda t: t[:4])
        return [t[4] for t in scored[:limit]]


etf_catalogue = EtfCatalogue()


def catalogue() -> list[Etf]:
    """Every index ETF the user can pick: core first (most traded), then by group and symbol."""
    return etf_catalogue.all()


def find(symbol: str) -> Etf | None:
    """The catalogue entry for a symbol ("NIFTYBEES", "niftybees" or "NIFTYBEES-EQ"), or None."""
    return etf_catalogue.find(symbol)


def is_index_etf(symbol: str, name: str | None = None) -> bool:
    """True only for ETFs in the catalogue. `name` is accepted for callers that have one; the catalogue decides."""
    return etf_catalogue.find(symbol) is not None


def search(query: str, limit: int = 60) -> list[Etf]:
    return etf_catalogue.search(query, limit)


def groups() -> list[str]:
    return list(GROUPS)
