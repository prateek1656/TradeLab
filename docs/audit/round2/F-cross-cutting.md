# F — Cross-cutting (round 2)

## Checked so far

- Re-read after the F-2, C5 and B2 fixes: `launch`, `_exit_lock`, `_wait_for_entries`, `_stopped_at`, `_use_confirmation`,
  `run_exit` (incl. ALL), `_square_off_account` + `_squared` memory (F-1 fix) + the new "hold back until the book is read"
  rule, `_cancel_own_open_orders`, `_confirm_cancels`, `_stray_orders`, `fire_plan` / `_fire_in_waves` / `_hedges_to_wait_for`,
  `build_sell_plan`, `scheduler.py` (all jobs, misfire grace, catch-ups), `calendar.sync_clearing_holidays`, `broker/manager.py`,
  the automation / sections / alerts routers, and the pop-up component.
- Sound: two panic taps (second run sees `_squared`, sends nothing); auto job + catch-up at the same moment (both pass
  `_already_ran`, then serialize on `_exit_lock`, and `_squared` stops a second close); the clearing job is registered at
  08:35 (grace 6 h) and again at startup (checked by starting the real scheduler in a test); a failed clearing download only
  logs a warning and keeps the safe two-day fallback; confirmations are per user / section / action and single use; every
  trade route is `current_user`-scoped and account ids are filtered by `user_id`.
- Scratch tests were run and deleted (`backend/tests/review_F_scratch_test.py`).

## Answer to the open question: NRML part of the panic / F&O and Commodity Exit with an unreadable order book

**It cannot flip a position in normal use.** Confirmed with three tests through the real adapter. The NRML close is sized
from TradeLab's own records and Kotak's positions (never from the order book), and the order book is only used to
(a) cancel TradeLab's own working orders and (b) report other orders. What the unreadable book changes:

1. Holder's stop-loss on TradeLab's NRML contract, book unreadable vs readable: the same close of 65 is sent in both
   cases and the stop-loss is left in both (TradeLab never cancels the holder's orders, by design). Unreadable: the run is
   "partial" with "Closed, but Kotak's order book couldn't be read to check for another order still working…" and
   "Still working at Kotak (0 of 65 filled so far)". Readable: "…another order on this contract (a stop-loss…) is still
   working…". So the risk is identical and it is reported either way.
2. An earlier Exit whose close part-filled and still works (2 lots bought, close filled 1 lot), then the panic with the book
   unreadable: the panic sends nothing more (the earlier close is counted in full in the ledger), net stays 1 lot, the run
   is "failed" with "Could not read orders to cancel TradeLab's working orders". No second close, no flip.
3. TradeLab's own entry not confirmed by the book: already tested in the suite (held, reported).

Residual (reported, never silent): a TradeLab order that stays working because it could not be cancelled can fill after
the close and leave a remainder open, on the same side, not a flip. No change needed. The only thing I would add is
cosmetic: the first case's message could say outright that the close went out beside an order that could not be checked.

## Findings

### F2-1. Exit with "only what TradeLab bought" (or a single symbol) sells the hedge and leaves the sold leg naked — CONFIRMED

Severity: medium–high (an option spread loses its protection; margin needs jump; an unlimited-risk short is left open
overnight). C5 made the panic and a full Exit keep the hedge when a same-underlying short stays open, but the guard only
sees shorts that are in the plan or in `unclosed`. `_run_sell` never passes `unclosed`, and `build_sell_plan` removes the
shorts before the plan exists when `opened="B"` (the "Sell only what TradeLab bought" choice in the Sell sheet) or when
`symbols` names the hedge. The hedge is then sold at once with no warning in the preview (the sell-first warnings only
exist for Buy).

Where: `trading.py:3476` (`fire_plan(plan, …, closing=True)` without `unclosed`), `trading.py:2873-2890` (`ledger` filtered
by `opened` / `symbols` before planning), `trading.py:1196-1203` (`_hedges_to_wait_for` only looks at `plan` + `unclosed`).

Test (scratch; fails on the current code: net of the 25000 PE short stays -65, hedge 24500 PE goes to 0):

```python
def test_exit_only_what_was_bought_sells_the_hedge_and_leaves_the_short(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=250000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", "S")["trading_symbol"]
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", "B")["trading_symbol"]
    _run(client, owner, "fno", "buy")
    acct = sim.account("AB101")
    pv = client.get("/api/sections/fno/preview", params={"action": "sell", "opened": "B"}, headers=owner).json()
    body = {"fingerprint": pv["fingerprint"], "confirm_id": pv["confirm_id"], "symbols": None, "opened": "B"}
    _wait(client.post("/api/sections/fno/sell", json=body, headers=owner).json()["run_id"])
    assert acct.net(hedge, "NRML") != 0 or acct.net(short, "NRML") == 0   # fails: hedge 0, short -65
```

Smallest fix: in `_run_sell` (and `command_preview`) compute `unclosed` from the unfiltered ledger
(`tradelab_open(user_id, section, account_ids)` without `symbols` / `opened`): every short whose underlying matches a hedge
being sold and which is not itself in the plan becomes a skip+error PlannedOrder, and pass it to `fire_plan(..., unclosed=)`.
The hedge then gets "Not sold: the sold leg it hedges couldn't be bought back first…" and the preview says so.

### F2-2. The panic's intraday (MIS) square-off sends a hedge sale before the buy-back of its sold leg — PLAUSIBLE

Severity: low–medium. C5 fixed the order only for the NRML part. `_square_off_account` builds one `plan` from
`net.items()` in Kotak's positions order and sends it one close after another (`trading.py:2090-2112`), so an MIS spread
(the holder's own, in a TradeLab account; ALL scope covers every segment) can have the long sold before the short is bought
back. Kotak's RMS can reject the hedge sale for margin (self-heals on the next attempt, since refused closes aren't in
`sent`), but when it is accepted and the buy-back is then refused (price band, freeze) the short stays naked. Not run as a
test: kotak_sim has no margin model, only the order of the two `place_order` calls shows it.

Smallest fix: sort `todo` so buy-backs of derivative shorts go first, and hold derivative longs of the same underlying
back until those buy-backs are seen filled (the same rule as `_fire_in_waves`).

### F2-3. A single partial year in NSE's clearing list marks the whole year as known — CONFIRMED (mechanism); depends on NSE's feed

Severity: low–medium (only matters around New Year). `save_clearing_holidays` records every year that has at least one
date in the download as "fully listed". If NSE ever returns, say, 2026 plus only 26-Jan-2027, `clearing_holidays(2027)` is
`{26-Jan-2027}` instead of `None`, so `_settling_since` stops using the safe two-trading-day fallback in January 2027 and
trusts an incomplete list: a delivery buy before an unlisted clearing holiday would be written down as missing while it
is still settling. Test (fails: returns `{date(2027,1,26)}`):

```python
rows = [{"tradingDate": d, "description": "x"} for d in ("26-Jan-2026", "03-Mar-2026", "14-Oct-2026")]
rows.append({"tradingDate": "26-Jan-2027", "description": "Republic Day"})
assert calendar.sync_clearing_holidays(lambda: {"CM": rows})
assert calendar.clearing_holidays(2027) is None
```

Smallest fix: mark a year known only if it is the year of the download's majority / the current year, or has at least
~8 entries; otherwise keep it unknown.

### F2-4. Accounts past `max_parallel_orders` (32) wait for a whole earlier account's square-off — CONFIRMED (artificial cap)

Severity: low for a family (under 32 accounts), real for a larger list. `run_exit` runs the per-account square-offs on
`_pool(len(accounts))` (`trading.py:2342-2344`), capped at `max_parallel_orders` = 32. One `_square_off_account` is not an
order but a whole loop (up to 5 attempts of cancel wait, 2 s verify, book polls, and 30 s per hung Kotak call), so the 33rd
account starts only when a slot frees, possibly a minute or more after the panic was tapped. Test with the cap at 2 and
3 accounts: starts at 0.06 s, 0.06 s and 1.06 s. The same cap applies to `_fire_in_waves` (`len(hedged)+1` workers, each a
full two-wave close). Same slowness inside `_fire`: one account with many freeze-limit slices can fill workers while they
wait on that account's login lock (manager `entry.lock`), delaying other accounts' orders.

Smallest fix: size these per-account pools as `len(accounts)` (threads mostly sleep or wait on I/O), and keep the cap only
for the per-order pool in `_fire`.

### F2-5. Observations (no money impact, no change required)

- Pop-ups exist only for exit runs and funds; a failed Let's Trade buy (login failure in some accounts, orders with no
  clear reply) shows only in Activity. The square-off still covers accounts with unknown entries.
- A failing automatic square-off retried every minute until 15:00 creates one failed run, hence one pop-up, per minute;
  the page shows three at a time.
- `run_exit` consumes `ex.map(...)` results in order (`trading.py:2344`): if one account's `square_off_account` raised (a
  database error in `_reserve_logs` / `_settle_logs`), the entries of the accounts after it are dropped from the run's
  summary, which shows only "Square-off crashed". Their orders were already logged; the run is never "done".
