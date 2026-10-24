# F — Cross-cutting (round 3)

## Checked so far

- Diff `7632274..HEAD` read for: `run_exit` (automatic guards, lost-run row, retry timer, per-account `account_close`,
  `carry_forward`, nested `_account_pool`), `_record_lost_exit`, `_finish_run`/`_live`, `_already_ran`, `reconcile_orders`
  with `_abandoned_to_unknown`, `_squared`/`_earlier_closes`, `_stopped_at`, alerts router (`entry` kind), `FundsAlerts.tsx`,
  scheduler (`sync_jobs` grace, `catch_up_square_off`, `AFTER_MCX_GRACE`), calendar clearing status, `db.py` session writes.
- Sound (tested through the real adapter): a 10-account panic (MIS + F&O + MCX) finishes in about a second, no database
  conflict, no stray errors, every NRML contract flat; one account's `square_off_account` raising is contained (the other accounts'
  entries and NRML closes still happen, the run is "partial", the pop-up names the account); a hedge refused / a login failure /
  funds in one account and another failure in a second account all give the right `entry` / `funds` pop-up; a lost automatic
  exit (database error) records a failed row and the retry timer then closes the position; alerts and dismiss are
  `user_id`-scoped; trade routes validate `opened` (Literal), fingerprint / confirm id lengths.
- Thread safety: `_squared` is written per account id as one tuple assignment, read under `_squared_guard`, and only
  `square_off_account` (under `_exit_lock`) touches it; `_stopped_at` / `_pacers` are single dict operations under the GIL;
  `_live` is always under `_live_guard`. The per-account pools share the per-account `_Pacer` and the manager's login lock.
  The concurrent `reconcile_orders(user_id, [a.id])` calls of the panic touch disjoint rows (filtered by account).
  `_abandoned_to_unknown` skips every run in `_live`, so a run still sending is never touched. (Residual, not reported: the
  UI's `section_positions` also calls `reconcile_orders(user_id)` for all accounts, so two reconciles can meet on the same
  row; the session replays the changed fields as computed, so a stale book read could lower `filled` of a final row;
  needs a poll landing in the milliseconds of a commit and a partial fill, and sizing is re-clamped to Kotak's figure.)
- Scratch test `backend/tests/review_F_scratch_test.py` was deleted.

## Findings

### F3-1. The panic button cancels the square-off's own part-filled close and leaves the rest of the position open — CONFIRMED

Severity: low–medium (reported, never silent, a second tap closes the rest; but the position stays open meanwhile, and for
an F&O MIS short it is an open short).

Scenario: panic button, an intraday (MIS) close that part fills and keeps working (a thin or circuit-limited stock; the
simulator's `partial_next`). Plain "Square off now" / the 14:30 job leaves that close working at Kotak, where it can still fill.
The panic's next step per account, `_cancel_own_open_orders(user_id, [a])`, cancels *every* TradeLab order of the day with
`final != True`, including the close the same run sent a moment earlier, so the unfilled rest is withdrawn. Result: 4997 of
5000 shares stay open, the summary says only "Position still open after square-off attempts", and the cancel row says
"Cancelled" with nothing linking the two. With 10 accounts and 4 of them thin, 4 positions were left (output above in the
10-account test).

Where: `trading.py` `carry_forward` -> `_cancel_own_open_orders(user_id, [a])` (about line 2549) running after
`square_off(a)`; the filter `flt` in `_cancel_own_open_orders` (about line 2368) does not exclude the current run's own rows.

Test (fails on current code: net 4997 and a "cancel success" row for the close):

```python
def test_panic_does_not_cancel_its_own_part_filled_close(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    acct = sim.account("AB101")
    acct.place_order(exchange_segment="nse_cm", product="MIS", price="0", order_type="MKT", quantity="5000",
                     validity="DAY", trading_symbol="SBIN-EQ", transaction_type="B")
    acct.partial_next = 3
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"], timeout=60)
    assert [o["ordSt"] for o in acct.orders][-1] != "cancelled"      # fails: 'cancelled'
    assert acct.net("SBIN-EQ", "MIS") == 0 or acct.orders[-1]["ordSt"] == "open"
```

Smallest fix: give `_cancel_own_open_orders` an `exclude_run` argument (`flt["run_id"] = {"$ne": run_id}`) and pass the
panic's `run_id` from `carry_forward`. (The NRML closes are sent after the cancel step, so nothing of this run needs cancelling.)

### F3-2. A square-off that did not run because the server was down until after the section's last moment leaves no trace — CONFIRMED

Severity: low–medium (money: the position is closed by Kotak's own auto square-off, with its charges, instead of by TradeLab;
the failure is that nobody is told).

Scenario: the server is down (deploy, crash, host reboot) through 14:30 and comes back at 15:01. `catch_up_square_off` and
the late job both end in `run_exit`'s new guard `now >= sell_until` -> `return run_id` with `run_id` None: no run row, no
pop-up, nothing in Activity. The owner's 5000 shares are still open and TradeLab never says that it didn't square off.
Test (passes = problem shown): after the entry, `clock.set(15:01)`, `scheduler.catch_up_square_off()` and
`trading.run_exit(uid, "scheduled")` -> zero `kind="exit"` runs, `acct.net("SBIN-EQ","MIS") == 5000`, `/api/alerts == []`.

Where: `trading.py` `run_exit`, the `sell_until` guard (about line 2490).

Smallest fix: in that branch, if `accounts_traded_today(user_id, today, section)` is non-empty and `_already_ran` is false, write
one failed exit row ("Not squared off: the server was not running before 15:00; Kotak squares it off itself") so the pop-up
appears (`_start_run` + `_finish_run` as `_record_lost_exit` does).

### F3-3. Pop-ups outlive the problem and repeat — CONFIRMED (cosmetic, but it can hide a real pop-up)

Severity: low. Each failed automatic attempt (including each lost-run row and each 60-second retry) is its own `ExecutionRun`
with its own pop-up; none is cleared when a later run of the same section and day finishes "done". Test: the first two
scheduled/catch-up attempts fail on a database error, the third closes the position: `/api/alerts` still shows two
"Positions may still be open" pop-ups, without account names, for a position that is flat. The page shows only three at a
time, newest first, so a long failing streak (one per minute until 15:00) pushes an older, real entry or funds pop-up out of
sight until each is dismissed.
Where: `alerts.py` `list_alerts`.
Smallest fix: in `list_alerts` (or when a run finishes "done"), skip / mark seen earlier failed or partial exit runs of the same
user, section and trading day when a later exit run of that section is "done"; show one pop-up per (kind, section, day).

### Observations (no change required)

- The `entry` pop-up is suppressed per account name, not per row: `set(names) - set(funds_short)`. An account that is
  funds-short for one pair and also has an *unknown* sold leg for another pair gets only "Not enough funds". Needs two pairs plus an
  unknown reply in one account; the run is still "partial" and Activity shows the row.
- The per-account "Square off" button (`section=ALL`, one account) sets `_stopped_at` for the whole user, so an on-command Buy
  that is still preparing for the other accounts is dropped with "Cancelled by Square off everything — nothing was sent"
  (run "done", no pop-up). No order is sent; nothing is lost but the trade.
- Crash rows built in `account_close` / `close` ("Carry-forward close crashed", "F&O sell crashed") are in the run's summary
  and status but are not written to the order log, so that pop-up lacks the account name.
- Scheduler: the exit grace (`max(60, sell_until - exit_time)`), `sell_until`/`auto_exit` guards, the retry cut-off and
  `catch_up_square_off`'s window agree with each other; `AFTER_MCX_GRACE` = 90 s means a restart across 23:58 has no catch-up
  for the evening reconcile (already accepted: the last seen fill is kept).

## Result

No double order, no flipped position, no unreported open position found in the new concurrency or scheduling code. F3-1 and F3-2
are small, reported-or-recoverable gaps; F3-3 is cosmetic.
