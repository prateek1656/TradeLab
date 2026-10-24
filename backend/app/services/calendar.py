"""Automatic market-holiday detection.

Two independent layers:
1. NSE's published trading-holiday calendar, downloaded every morning.
2. A live check just before entry: if the biggest stocks show no trade today,
   the market is closed (see `market_open_today`).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time as dtime

import httpx

from ..config import IST, get_settings
from ..db import session_scope
from ..models import ClearingHoliday, ClearingYear, Holiday

log = logging.getLogger(__name__)

NSE_HOME = "https://www.nseindia.com/"
NSE_HOLIDAYS = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_CLEARING_HOLIDAYS = "https://www.nseindia.com/api/holiday-master?type=clearing"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
}

# Large, always-traded NSE stocks (symbol, well-known NSE token) used to tell
# whether the market is open today.
BENCHMARKS = [
    ("RELIANCE-EQ", "2885"),
    ("HDFCBANK-EQ", "1333"),
    ("ICICIBANK-EQ", "4963"),
    ("INFY-EQ", "1594"),
    ("TCS-EQ", "11536"),
    ("SBIN-EQ", "3045"),
]

_lock = threading.Lock()
_status: dict = {"last_success": None, "last_error": None, "count": 0}


def _status_file():
    return get_settings().data_dir / "holiday_sync.json"


def sync_status() -> dict:
    with _lock:
        if _status["last_success"] is None and _status["last_error"] is None:
            try:
                _status.update(json.loads(_status_file().read_text()))
            except (OSError, ValueError):
                pass
        return dict(_status)


def _save_status(**kw) -> None:
    with _lock:
        _status.update(kw)
        try:
            _status_file().write_text(json.dumps(_status))
        except OSError:
            pass


_clearing_status: dict = {"last_success": None, "last_error": None}


def _clearing_status_file():
    return get_settings().data_dir / "clearing_sync.json"


def clearing_sync_status() -> dict:
    with _lock:
        if _clearing_status["last_success"] is None and _clearing_status["last_error"] is None:
            try:
                _clearing_status.update(json.loads(_clearing_status_file().read_text()))
            except (OSError, ValueError):
                pass
        return dict(_clearing_status)


def _save_clearing_status(**kw) -> None:
    with _lock:
        _clearing_status.update(kw)
        try:
            _clearing_status_file().write_text(json.dumps(_clearing_status))
        except OSError:
            pass


def parse_nse_holidays(data: dict) -> dict[date, str]:
    """Equity-segment trading holidays from NSE's holiday-master JSON."""
    rows = None
    for key in ("CM", "cm", "FO"):
        if isinstance(data.get(key), list) and data[key]:
            rows = data[key]
            break
    out: dict[date, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("tradingDate") or row.get("date") or "").strip()
        day = None
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                day = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if day is None:
            continue
        desc = str(row.get("description") or row.get("holiday") or "Market holiday").strip()[:120]
        out[day] = desc or "Market holiday"
    return out


def _fetch_nse(url: str = NSE_HOLIDAYS) -> dict:
    with httpx.Client(headers=_HEADERS, timeout=20, follow_redirects=True) as client:
        client.get(NSE_HOME)  # NSE hands out the cookies its API needs here
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def _fetch_clearing() -> dict:
    return _fetch_nse(NSE_CLEARING_HOLIDAYS)


_sync_lock = threading.Lock()


def sync_nse_holidays(fetch: Callable[[], dict] = _fetch_nse, today: date | None = None) -> dict:
    """Download NSE's holiday calendar into the holiday list. Never raises."""
    with _sync_lock:  # the startup sync, the 08:30 job and "Check now" can overlap
        try:
            return _sync(fetch, today)
        except Exception as exc:  # noqa: BLE001
            log.exception("Saving NSE holidays failed")
            _save_status(last_error=f"Couldn't save NSE's holiday list ({exc.__class__.__name__})")
            return sync_status()


def _sync(fetch: Callable[[], dict], today: date | None) -> dict:
    try:
        holidays = parse_nse_holidays(fetch())
        if not holidays:
            raise ValueError("NSE returned no equity holidays")
    except Exception as exc:  # noqa: BLE001
        msg = f"Couldn't download NSE's holiday list ({exc.__class__.__name__}: {exc})"[:300]
        log.warning(msg)
        _save_status(last_error=msg)
        return sync_status()
    today = today or datetime.now(IST).date()
    years = {d.year for d in holidays}
    with session_scope() as db:
        existing = {h.day: h for h in db.find(Holiday)}
        for day, desc in holidays.items():
            h = existing.get(day)
            if h is None:
                db.add(Holiday(day=day, description=desc, source="nse"))
            elif h.source == "nse":
                h.description = desc
        # NSE sometimes moves a holiday: drop our future NSE entries it no longer lists.
        for day, h in existing.items():
            if h.source == "nse" and day >= today and day.year in years and day not in holidays:
                db.delete(h)
    _save_status(last_success=datetime.now(IST).isoformat(), last_error=None, count=len(holidays))
    return sync_status()


def save_clearing_holidays(days: set[date], years: set[int]) -> None:
    """Store the clearing holidays of these (fully listed) years, replacing what was stored for them."""
    with session_scope() as db:
        for h in db.find(ClearingHoliday):
            if h.day.year in years and h.day not in days:
                db.delete(h)
        have = {h.day for h in db.find(ClearingHoliday)}
        for day in days - have:
            db.add(ClearingHoliday(day=day))
        known = {y.year for y in db.find(ClearingYear)}
        for year in years - known:
            db.add(ClearingYear(year=year))


def sync_clearing_holidays(fetch: Callable[[], dict] = _fetch_clearing) -> bool:
    """Download NSE's clearing-holiday list (days banks and the clearing corporation are closed, which
    delay the settlement of delivery trades). Never raises; True if it was saved. A failure keeps the
    old list, and years never downloaded stay unknown (see `clearing_holidays`)."""
    with _sync_lock:
        try:
            holidays = parse_nse_holidays(fetch())
            if not holidays:
                raise ValueError("NSE returned no clearing holidays")
            save_clearing_holidays({d for d in holidays if d.weekday() < 5}, {d.year for d in holidays})
            _save_clearing_status(last_success=datetime.now(IST).isoformat(), last_error=None)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't download NSE's clearing-holiday list (%s: %s)", exc.__class__.__name__, exc)
            _save_clearing_status(last_error=f"Couldn't download NSE's clearing-holiday list "
                                             f"({exc.__class__.__name__}: {exc})"[:300])
            return False


def clearing_holidays(year: int) -> set[date] | None:
    """The clearing holidays of a year, or None if that year's list was never downloaded."""
    with session_scope() as db:
        if db.get(ClearingYear, year) is None:
            return None
        return {h.day for h in db.find(ClearingHoliday) if h.day.year == year}


def market_open_today(clients: list, now: datetime, tokens: list[tuple[str, str]] | None = None,
                      min_quotes: int = 3) -> bool | None:
    """True/False if the quotes clearly show whether the market traded today, None if unknown.

    Only says "closed" when the quotes (by default several large NSE stocks) all show
    their last update on an earlier day, so an ordinary trading day is never mistaken
    for a holiday. `tokens` checks other instruments instead (e.g. MCX contracts).
    """
    from .scrips import scrip_master

    if tokens is None:
        tokens = [("nse_cm", scrip_master.token_for(sym) or tok) for sym, tok in BENCHMARKS]
    today_start = datetime.combine(now.astimezone(IST).date(), dtime(0, 0), tzinfo=IST).timestamp()
    for client in clients:
        if not hasattr(client, "quote_times"):
            continue
        try:
            times = client.quote_times(tokens)
        except Exception as exc:  # noqa: BLE001
            log.info("Market-open check failed on one client: %s", exc)
            continue
        if len(times) < min_quotes:
            continue
        return max(times.values()) >= today_start
    return None
