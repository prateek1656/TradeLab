# B — Holding (CNC) — round 2

Auditor B. Scratch test file `backend/tests/review_B_scratch_test.py` was deleted at the end; the test code of each
confirmed finding is below (helpers as in `test_kotak_replies_end_to_end.py`: `_add`, `_add_etf`, `_run`, `_logs`,
`_age_buys`, `_trading_days_back`, `_clearing_holiday`; `database` = `app.db.database`).

## Checked so far

- Read round 1's B file, `_settling_since`, `_previous_day_delivery_buys`, `_shrink_to_broker`,
  `_written_off_today`, `_note_holders_own` / `_holders_own_before_today`, `calendar.sync_clearing_holidays`,
  `save_clearing_holidays`, `clearing_holidays`, `parse_nse_holidays`, the scheduler jobs (08:35 + startup),
  `build_entry_plan` (CNC sizing), `build_sell_plan`, `section_positions`.
- `_settling_since` fuzzed against a brute-force model (20,000 random calendars across the 2026/27 year boundary,
  weekends, trading holidays, clearing holidays, with 15% closed-day density): it matches in every case except
  when 4+ consecutive trading days are all clearing holidays (the 4-trading-day walk limit; unrealistic, real
  clusters are 1-2 days). Year boundary, weekends, long trading-holiday runs, a clearing list partially known
  (any involved year unknown -> last two trading days) all behave correctly. The settlement day itself stays
  protected through 23:58 (settled == today), the next morning it is not: consistent with the old T+1 rule.
- Interaction with `_written_off_today`: `before_today = records + today's write-offs` makes repeated looks
  idempotent across the wider window; sales earlier days are handled by those days' own true-ups. Fine.
- 00:00-09:00 window: nothing is written, and the 08:35 sync lands inside it, so the window can only change
  between the morning's looks, never mid-write. Fine.
- Sync failure paths: any exception keeps the old list, returns False, never raises; an empty response
  (including a year with zero holidays) is rejected, so that year stays "unknown" and falls back to two days.
  Fail-safe.
- Existing Holding end-to-end tests still pass.

## Findings

### B3 — CONFIRMED — with two settling days (a clearing holiday), the holder's "own units" figure is overwritten with a too-low value, so her sale of her own units next morning writes off TradeLab's settling units

Severity: up to (units the holder sells of her own) TradeLab units forgotten for good in an account whose holder
owns the same ETF (Mummy Ji-type: hundreds of units); they stay in her demat, Sell reports done and leaves them.
Newly reachable through B2's wider window; it is the round-1 B1 loss again.

Scenario: Mummy Ji owns 30 ITBEES. Mon: TradeLab buys 19; the Mon 15:40 true-up records her own 30 (total 49
minus 19). Tue is a clearing holiday: TradeLab's 19 are in neither positions nor demat, Kotak shows 30 which is
>= TradeLab's 19, so `_shrink_to_broker` takes the `shown >= abs(ours)` branch and `_note_holders_own` records
her own as `30 - 19 = 11` (it counts her units as covering TradeLab's missing ones). Wed morning, TradeLab's
units still settling, she sells 15 of her own 30: `_holders_own_before_today` returns the stale 11, so
`outside = 15 - 11 = 4` and 4 of TradeLab's units are written off. They arrive that afternoon; Sell sells 15 and
she is left with 19 instead of 15. (With a single settling day the note taken on the settling day is refreshed by a
post-arrival look that same day, so the stale value is not used; the extra day is what exposes it.)

Where: `backend/app/services/trading.py:2760-2763` (`if shown >= abs(ours): ... _note_holders_own(key, shown -
abs(ours))`), read at 2781 (`_holders_own_before_today`).

Test (fails today: `written off [4]; she is left with 19 (her own: 15)`):

```python
def test_b3_own_units_misread_during_settling_then_holder_sells(client, owner, clock, sim):
    clock.set(IN_SESSION)
    mid = _add(client, owner, sim, "Mummy Ji", "CD202", cash=2000.0, demat={"ITBEES-EQ": [30, 30, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")            # Monday
    m = sim.account("CD202")
    bought = m.net("ITBEES-EQ", "CNC")
    assert 0 < bought <= 30
    trading.true_up_all()                            # Monday 15:40: sees her own 30
    d1, d2 = _trading_days_back(1), _trading_days_back(2)
    for d in database()["holder_units"].find({}):
        database()["holder_units"].update_one({"_id": d["_id"]}, {"$set": {"seen_at": d["seen_at"] - d2}})
    _age_buys(mid, d1)                               # now it is Tuesday (a clearing holiday)
    m.clear_day_positions()
    trading.true_up_all()                            # Tuesday 15:40: her 30 >= TradeLab's settling 19 -> own = 11
    for d in database()["holder_units"].find({}):
        database()["holder_units"].update_one({"_id": d["_id"]}, {"$set": {"seen_at": d["seen_at"] - d1}})
    _clearing_holiday(1)
    _age_buys(mid, d2 - d1)                          # Wednesday
    m.place_order("nse_cm", "CNC", "0", "MKT", "15", "DAY", "ITBEES-EQ", "S")   # she sells 15 of her own 30
    trading.true_up_all()                            # Wednesday 15:40, TradeLab's units still settling
    written = [a.quantity for a in _logs(status="adjusted")]
    m.demat["ITBEES-EQ"][0] += bought
    m.demat["ITBEES-EQ"][1] += bought
    m.clear_day_positions()
    _run(client, owner, "holding", "sell")
    left = m.demat["ITBEES-EQ"][0]
    assert (written, left) == ([], 15), f"written off {written}; she is left with {left} (her own: 15)"
```

Smallest fix: do not lower or refresh `holder_units` while any of TradeLab's units of that symbol may still be
settling (when `_previous_day_delivery_buys(...)` has the symbol, skip `_note_holders_own`, or store
`max(old, new)`). While they settle, `shown - ours` is not the holder's figure. Only a look after arrival (shown
>= ours with nothing settling) should update it.

### B4 — CONFIRMED — the Positions screen shows nothing for units that are still settling (1 day, or 2-3 over a weekend / clearing holiday), no row and no note

Severity: display, but it is the screen the owner reads to know what TradeLab holds. The day after a Buy now,
TradeLab's records hold 19 ITBEES in the account, Sell says "still settling", but Positions is empty with value
and P/L 0. Over a weekend the owner sees an empty screen for almost three days after a Friday buy and may Buy
again, doubling the exposure. B2's wider window makes the gap longer.

Where: `backend/app/services/trading.py:3043-3045` (`qty = min(abs(held.qty), have); if qty <= 0: continue`).

Test (fails today: `TradeLab holds [19] but the Positions screen shows nothing`):

```python
def test_b4_positions_screen_hides_settling_units(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=2000.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    _age_buys(small, _trading_days_back(1))
    kanha.clear_day_positions()  # next day: settling, in neither positions nor demat
    body = client.get("/api/sections/holding/positions", headers=owner).json()
    assert _held_by_tradelab(client, owner, small) == [bought]
    assert body["rows"] or body["errors"] or body.get("settling"), body
```

Smallest fix: when `have < abs(held.qty)` for a delivery key, add the shortfall to the response as a
"settling" row (quantity, symbol, account, "bought on <date>, not at Kotak yet") or at least as an `errors` style
note, so the units are visible; keep them out of value / P&L if no price.

### B5 — PLAUSIBLE (hardening) — once a year is "known", a wrong or truncated clearing list silently narrows the protection below the old two-day fallback

Severity: if NSE's clearing endpoint returns a list that lacks a short-notice clearing-only holiday (NSE announces
these by circular days ahead; the API may lag), or a response that is really the trading list (a `type` the
server ignores), or a year with only a few stray dates (a list for 2026 containing "01-Jan-2027" marks 2027 as
"known" with one holiday), the year counts as complete and the window shrinks to the previous trading day only:
B2 returns for that holiday (units settling after the holiday are written off, Sell says "Nothing left to close").
Nothing in the app shows that the clearing sync never worked (its failures are only logged; there is no status
like `sync_status` for the trading list).

Where: `calendar.py:168-181` (`years = {d.year for d in holidays}`), `trading.py:2655-2658`.

Smallest fix: never go narrower than the fallback: `return min(known_since, days[min(1, len(days) - 1)])`, i.e.
always protect the last two trading days and use the clearing list only to protect more (a holiday run);
optionally mark a year known only when the response has >= 1 date in it from a CM list whose key is the expected
one, and surface last success / error of the clearing sync next to the trading one in the Settings status.

### Not reported (checked, fine)

- `_settling_since` boundary and year handling (see above); 4-day walk limit only matters with 4+ adjacent
  clearing-only trading days.
- `save_clearing_holidays` drops stored holidays of a listed year that the new list lacks, including past ones;
  harmless for a complete yearly list (NSE lists the whole year).
- The 08:35 job and the startup thread both take `_sync_lock`; a reader sees either the old or the new list or
  "unknown" (fallback), never a half-year that is wrongly "known".
- Buy now sizing for CNC (cash minus collateral, 1.5% headroom, protected price, equal split, custom qty /
  amount / percent capped by the cash left): no money error found.
