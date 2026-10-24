# F — Cross-cutting (round 5)

## Checked so far

- `git diff eddbe8a HEAD` for `routers/alerts.py`, `models.py` (scope fields), `scheduler.py`, `SectionKit.tsx`, and the
  `_set_scope`, `_record_missed_exit`, `run_exit` parts of `trading.py`. `npm run typecheck` passes.
- Scope-aware pop-ups: a done run hides an account's failure only when it is newer, has `scope_accounts`, covers the
  account, the account has no failed/unknown row in it and (for a Sell) the row's symbol is in `scope_symbols`. Rows
  without a symbol, runs without a scope (old rows, `_record_lost_exit`, missed-exit rows) and still-running runs hide
  nothing. Collapse merges names across the same section and day. Sound. `dismiss` marks the same section/day's earlier
  runs seen; every earlier unhidden run was merged into the pop-up that was dismissed, so nothing unseen is lost
  (a still-running earlier run could be marked, but a later exit of the same section waits for `_exit_lock`, so it cannot
  finish a failed run before it; not reachable).
- Only Intraday has an automatic square-off (`has_exit`), so the missed square-off rule and the "rows empty + any done
  newer run" hide only involve Intraday runs, which always cover every account (`/square-off`, scheduled retries) or the
  accounts that traded. Panic runs (`section=all`) have their own key and cannot hide an Intraday failure (conservative).
- `_record_missed_exit` from `catch_up_square_off`: only when auto_exit is on, at/after `sell_until`, section traded
  today, no exit of that section (or a panic covering all traded accounts) today; deduped under `_missed_exit_lock`.
  Restart at 15:01, evening restarts, weekends and a normal day (exit done) behave correctly.
- Positions card: errors banner stays, Exit/Sell all button shows when an account is unreadable, empty-state text is
  not shown when unreadable/unclear/settling. Typecheck clean.
- New writes: `_set_scope` is one `update_many` on its own run row (no clobber by `_finish_run`, which re-reads);
  `holder_units` writes are last-writer-wins on absolute values computed from the same day figures (idempotent);
  `_finish_unknown` runs inside `_writedown_lock` in `_shrink_to_broker` but not in `prepare` (see F5-1).
- Fuzz: `race` profile, seeds 9000-9060 (+ replays). Seed 9221 RECORDS_TOO_LOW reproduces (F5-1). Seeds 9219 fails only
  under machine load (passes alone; timing).

## Findings

### F5-1. An old unknown Holding buy is finished (forgotten) while its units are still settling — CONFIRMED

Severity: medium (money left behind, not lost: units TradeLab bought for the account are never sold by Sell again, and
no message says so; the run says "holds nothing bought by TradeLab" or "done"). Overlaps area B (Holding).

Scenario: a Holding Buy of an ETF gets a lost reply and did go through (stays "unknown"). Next trading day the units
are still settling, so Kotak lists nothing for the key (no holdings row, and positions are of the day). The owner taps
Sell (or any Sell/panic builds the plan): `build_sell_plan.prepare` takes the key as `gone` because
`key not in broker_view(...).listed` and calls `_finish_unknown`, setting `final=True` on the unknown buy. The ledger
never counted it as confirmed, so the units that arrive a day later are unknown to TradeLab forever.
`_shrink_to_broker` keeps CNC out of this (`key.product != "CNC"`), `prepare` does not. Seed 9221 (race profile) shows it
in the randomized harness: "CD202 ITBEES-EQ CNC: TradeLab's records say 0, the oracle says 123".

Where: `backend/app/services/trading.py` `prepare()` in `build_sell_plan`, the `mystery` block (about line 3598-3603):
`gone = {k for k in old if k not in broker_view(acc_id, old).listed}` followed by `_finish_unknown(...)`.

Test (fails on the last assert, 492 < 492):

```python
def test_F_old_unknown_holding_buy_is_not_forgotten_while_it_settles(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=50000.0)
    _add_etf(client, owner, "ITBEES")
    acct = sim.account("AB101")
    database()["tag_proofs"].delete_many({})
    acct.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]
    _blank_tags(acct)
    _run(client, owner, "holding", "buy")            # went through at Kotak, reply lost
    acct.faults.clear()
    (row,) = _logs(action="buy"); assert row.status == "unknown"
    acct.settle_overnight(); held = acct.demat.pop("ITBEES-EQ")   # next morning: still settling, not listed
    _age_rows(_trading_days_back(1)); clock.set(datetime(2026, 10, 15, 9, 30, tzinfo=IST))
    _run(client, owner, "holding", "sell")
    acct.demat["ITBEES-EQ"] = held                    # a day later the units arrive
    _age_rows(timedelta(days=1)); clock.set(datetime(2026, 10, 16, 10, 0, tzinfo=IST))
    _run(client, owner, "holding", "sell"); _run(client, owner, "holding", "sell")
    assert acct.demat["ITBEES-EQ"][0] < held[0]       # fails: nothing was ever sold; the buy row is final
```

Smallest fix: in `prepare`, for a CNC key do not finish while the unknown row is inside the settling window
(`row.created_at >= _settling_since(today)` as `_previous_day_delivery_buys` uses), e.g.
`gone = {k for k in old if k not in listed and not (k.product == "CNC" and _within_settling(unknown_buys[k]))}`;
outside the window finish as now. (Keep the row reported as "no reply from Kotak" in the meantime.)

### F5-2. Entry and funds pop-ups of one run share a `run_id` — CONFIRMED (API), PLAUSIBLE (UI)

Severity: low. When a F&O/Commodity Buy fails for some accounts and others run out of money, `/api/alerts` returns two
items (kind "entry", kind "funds") with the same `run_id`. `FundsAlerts.tsx` uses `key={a.run_id}` inside
`AnimatePresence` (duplicate React keys: React may omit or duplicate one on a re-render), and `dismiss` removes both
and marks the run seen. The entry pop-up (a sold position may be missing its hedge) can disappear with the funds one.
Test: scratch `test_F_entry_and_funds_share_a_run_id` (asserts unique run_ids; fails).
Smallest fix: `key={`${a.run_id}-${a.kind}`}` in `FundsAlerts.tsx` (the dismiss may stay per run).

### Notes (not findings)

- A failed Intraday exit retried every 60 s until 15:00 creates a new run per retry; a dismissed pop-up comes back with
  the next failed retry (by design: still failing).
- A false "TradeLab was not running" pop-up is possible when the owner switches Automatic square-off back on after
  15:00 on a day it was off (message only; the positions are Kotak's to close).
- A newer run that crashed with only account-less rows (`LogEntry(None, ...)`) counts as covering every account in its
  scope, so older per-account names are replaced by its own pop-up with no account names. The pop-up still shows.
