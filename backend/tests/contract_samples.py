"""A predictable stand-in for Kotak's futures and options lists (tests only; the app only ever
offers Kotak's own list). Expiries move with the calendar."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from app.services.contracts import _MONTHS, SEGMENTS, Contract, _num, _today



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
    """A small, realistic-looking list (expiries move with the calendar)."""
    today = today or _today()
    seq = iter(range(100001, 999999))
    out: dict[str, list[Contract]] = {seg: [] for seg in SEGMENTS}

    def add(seg: str, sym: str, inst: str, expiry: date, lot: int, opt: str | None = None,
            strike: float | None = None, mult: float = 1.0) -> None:
        if expiry < today:
            return
        tag = f"{expiry:%y}{_MONTHS[expiry.month - 1]}"
        ts = f"{sym}{tag}{expiry:%d}{_num(strike)}{opt}" if opt else f"{sym}{tag}FUT"
        out[seg].append(Contract(seg, ts, f"S{next(seq)}", sym, inst, opt, strike, expiry, lot, 0.05, mult))

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
    # MCX options on futures expire a few business days before their future.
    for sym, lot, day_of_month, strikes in (("CRUDEOIL", 100, 19, range(5000, 6501, 100)),
                                             ("NATURALGAS", 1250, 26, range(250, 351, 10))):
        for y, m in _months(today, 2):
            d = _business_day_on_or_before(date(y, m, day_of_month)) - timedelta(days=2)
            for k in strikes:
                for opt in ("CE", "PE"):
                    add("mcx_fo", sym, "OPTFUT", _business_day_on_or_before(d), lot, opt, float(k))
    for seg in SEGMENTS:
        out[seg].sort(key=lambda c: (c.symbol, c.expiry, c.option_type or "", c.strike or 0))
    return out
