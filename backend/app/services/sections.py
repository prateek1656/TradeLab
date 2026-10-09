"""The trading sections: each has its own list, accounts, settings and runs.

Every section works the same way: the money free in each account when it trades is split
equally between the section's items, and the same trade goes to every account at once.

* Intraday buys at its time after "Let's Trade" and squares off automatically.
* Holding buys index ETFs for delivery after "Let's Trade" (at its time, or right away
  during market hours) and sells only when you tap Sell.
* F&O and Commodity buy when you tap Buy and sell when you tap Sell, straight away.

Sections never touch each other's positions: Intraday squares off only intraday (MIS)
stock positions, and Sell only sells what that section itself bought.
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
    can_sell: bool                   # "Sell" button: sells what this section bought
    sides: tuple[str, ...]           # B = buy; S = short at entry (intraday only)
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
        opens=dtime(9, 15), buy_until=dtime(15, 15), sell_until=dtime(15, 20), calendar="nse", lots=False,
        description="Stocks bought (or shorted) in the morning and squared off the same day.",
    ),
    "holding": SectionSpec(
        key="holding", label="Holding", segments=("nse_cm",), products=("CNC",), scheduled=True,
        has_exit=False, can_sell=True, sides=("B",), entry_time="09:20:00", exit_time=None,
        opens=dtime(9, 15), buy_until=dtime(15, 15), sell_until=dtime(15, 25), calendar="nse", lots=False,
        description="Index ETFs bought for delivery and kept until you tap Sell.",
    ),
    "fno": SectionSpec(
        key="fno", label="F&O", segments=("nse_fo", "bse_fo"), products=("NRML", "MIS"), scheduled=False,
        has_exit=False, can_sell=True, sides=("B",), entry_time=None, exit_time=None,
        opens=dtime(9, 15), buy_until=dtime(15, 15), sell_until=dtime(15, 25), calendar="nse", lots=True,
        description="Futures and options, bought in lots when you tap Buy and sold when you tap Sell.",
    ),
    "commodity": SectionSpec(
        key="commodity", label="Commodity", segments=("mcx_fo",), products=("NRML", "MIS"), scheduled=False,
        has_exit=False, can_sell=True, sides=("B",), entry_time=None, exit_time=None,
        opens=dtime(9, 0), buy_until=dtime(23, 0), sell_until=dtime(23, 25), calendar="mcx", lots=True,
        description="MCX gold, silver, crude oil and more, bought in lots when you tap Buy and sold when you tap Sell.",
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
