"""The trading sections: each has its own list, accounts, settings and runs.

Every section works the same way: the money free in each account when it trades is split
equally between the section's items, and the same trade goes to every account at once.

* Intraday is the only section that works by the clock: it buys at its time after
  "Let's Trade" and squares off automatically.
* Holding (stocks and ETFs for delivery), F&O and Commodity trade only when you tap a button.
  Holding buys with Buy now and sells with Sell; F&O and Commodity can also sell a contract
  first and buy it back with Exit. Nothing in them ever happens by time: they use delivery
  (CNC) and carry-forward (NRML) orders, which Kotak doesn't square off at the end of the day.

Sections never touch each other's positions: Intraday squares off only intraday (MIS)
stock positions, and Sell / Exit only closes what that section itself opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime


@dataclass(frozen=True)
class SectionSpec:
    key: str
    label: str
    segments: tuple[str, ...]        # exchange segments its items trade on
    products: tuple[str, ...]        # allowed order products; the first is the default
    scheduled: bool                  # buys at its entry time after "Let's Trade" (else: on command)
    has_exit: bool                   # squared off automatically at its exit time
    can_sell: bool                   # "Sell" / "Exit" button: closes what this section opened
    sides: tuple[str, ...]           # B = buy; S = sell first (short) when it trades
    entry_time: str | None           # defaults
    exit_time: str | None
    opens: dtime                     # buys allowed from `opens` until `buy_until`
    buy_until: dtime
    sell_until: dtime                # last moment for Sell / square-off
    calendar: str                    # "nse": weekdays minus NSE holidays; "mcx": weekdays
    lots: bool                       # sized in lots (derivatives) instead of shares
    description: str


SECTIONS: dict[str, SectionSpec] = {
    "intraday": SectionSpec(
        key="intraday", label="Intraday", segments=("nse_cm", "bse_cm"), products=("MIS",), scheduled=True,
        has_exit=True, can_sell=False, sides=("B", "S"), entry_time="09:15:00", exit_time="14:30:00",
        # Kotak squares intraday stock positions off itself at 15:05 (since NSE's closing auction
        # started on 3 Aug 2026). TradeLab's square-off is at 15:00 at the latest, so the two never
        # close the same position at the same moment; new buys stop 5 minutes before the square-off.
        opens=dtime(9, 15), buy_until=dtime(14, 55), sell_until=dtime(15, 0), calendar="nse", lots=False,
        description="Stocks bought (or shorted) in the morning and squared off the same day.",
    ),
    # The three on-command sections: no entry time, no exit time, and only order products that
    # are never squared off by time (MIS would be closed by Kotak near the close).
    "holding": SectionSpec(
        key="holding", label="Holding", segments=("nse_cm",), products=("CNC",), scheduled=False,
        has_exit=False, can_sell=True, sides=("B",), entry_time=None, exit_time=None,
        # Stocks in F&O trade continuously until 15:15; after that NSE runs its closing auction.
        opens=dtime(9, 15), buy_until=dtime(15, 15), sell_until=dtime(15, 15), calendar="nse", lots=False,
        description="Stocks and ETFs bought for delivery when you tap Buy now, sold only when you tap Sell.",
    ),
    "fno": SectionSpec(
        key="fno", label="F&O", segments=("nse_fo", "bse_fo"), products=("NRML",), scheduled=False,
        # B = buy; S = sell first (short a future, write a call or put); Exit closes either.
        has_exit=False, can_sell=True, sides=("B", "S"), entry_time=None, exit_time=None,
        opens=dtime(9, 15), buy_until=dtime(15, 25), sell_until=dtime(15, 25), calendar="nse", lots=True,
        description="Futures and options in lots, bought or sold first. Opened when you tap Buy now, closed when you "
                    "tap Exit — never by time.",
    ),
    "commodity": SectionSpec(
        key="commodity", label="Commodity", segments=("mcx_fo",), products=("NRML",), scheduled=False,
        has_exit=False, can_sell=True, sides=("B", "S"), entry_time=None, exit_time=None,
        # MCX closes at 23:30 (23:55 from November to March); 23:25 is safe all year.
        opens=dtime(9, 0), buy_until=dtime(23, 25), sell_until=dtime(23, 25), calendar="mcx", lots=True,
        description="MCX gold, silver, crude oil and more in lots, bought or sold first when you tap Buy now, closed "
                    "when you tap Exit — never by time.",
    ),
}

ORDER = ("intraday", "holding", "fno", "commodity")
DEFAULT = "intraday"
SCHEDULED = tuple(k for k in ORDER if SECTIONS[k].scheduled)
SELLABLE = tuple(k for k in ORDER if SECTIONS[k].can_sell)


def spec(key: str) -> SectionSpec:
    try:
        return SECTIONS[key]
    except KeyError:
        raise ValueError(f"Unknown section {key!r}") from None


def label(key: str) -> str:
    s = SECTIONS.get(key)
    return s.label if s else key
