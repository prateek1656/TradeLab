# F — Cross-cutting (round 4)

## Checked so far

- Read `git diff f360223 HEAD` for `trading.py` (run_exit, `_stopped_at`, `_record_missed_exit`, `_cancel_own_open_orders(exclude_run)`,
  `_writedown_lock`), `routers/alerts.py`, `SectionKit.tsx`, `types.ts`, `db.py`. `npm run typecheck` passes.
- `_writedown_lock`: one lock per account, never nested with another lock (only `_locks_guard`, briefly), re-read of
  `tradelab_open` + `unfinished_keys` and the ADJUSTED insert are inside it; no deadlock path. Residual: a Buy/Exit row
  inserted in the microseconds between the re-read and the insert is not excluded (the lock only serialises write-downs);
  needs a race inside one function, not reported.
- `_stopped_at`: set only for `section == ALL and account_ids is None` (the panic button); the per-account "Square off"
  (`section=ALL, account_ids=[id]`) does not set it. Read in `_run_buy` (against `confirmed`) and `_hold_hedges` (against
  the wave's start). Sound.
- `_record_missed_exit` dedupe: `_missed_exit_lock` + the summary regex; sound in itself (but see F4-2: it is almost never reached).
- Positions `settling`/`unclear`: server side sound (display only); frontend problems below.

## Findings

### F4-1. One account's "Square off" (or a later partial Exit) removes the pop-up of a failed panic / Exit in other accounts — CONFIRMED

Severity: medium. The pop-up is the only persistent reminder that positions are still open. It is dismissed by something
that did not close them.

Scenario: panic button; account Mummy Ji's positions can't be read, so its MIS position stays open and the pop-up
"Mummy Ji" shows. The owner then taps "Square off" on another account's page (`POST /api/accounts/{id}/square-off`
= `launch("exit", [id], section=ALL)`, run row section `all`) or runs an F&O/Holding Exit for one symbol. That run is
"done", so `alerts.list_alerts` (`done_ids`, keyed only by section and day) treats the failed panic as superseded and
drops it. Mummy Ji's position is still open and nothing says so. The same applies to the `newest` collapse: a newer failed
run of narrower scope hides the older failed run's other accounts.

Where: `backend/app/routers/alerts.py` (`done_ids` / `newest`, about lines 33-48). The run row has no record of its scope.

Test (fails on the last assert: `[]`):

```python
def test_one_accounts_square_off_hides_the_panics_failure_in_another(client, owner, clock, sim):
    small, big = _setup(client, owner, sim)           # _family + SBIN basket + buy(client, owner)
    mummy = sim.account("CD202"); held = mummy.net("SBIN-EQ", "MIS")
    mummy.faults["positions"] = GATEWAY_PAGE
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"], 30)
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Mummy Ji"]]
    run2 = _wait(client.post(f"/api/accounts/{small}/square-off", headers=owner).json()["run_id"], 30)
    assert run2.status == "done" and mummy.net("SBIN-EQ", "MIS") == held
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Mummy Ji"]]
```

Smallest fix: let a "done" run supersede an earlier failed one only if it covered it: store the run's account ids (and for
`_run_sell` its symbols) on the `ExecutionRun`, and drop the failed run only if every account named in its failed/unknown
rows is in a later done run's scope (or simply: only a later done run with `account_ids is None` and, for sells,
`symbols is None`). The `newest` collapse should merge account names of the collapsed failed runs instead of keeping the
newest run's alone (or collapse only runs with the same scope).

### F4-2. Round-3 F3-2 "missed square-off" pop-up is never produced by a real restart — CONFIRMED

Severity: low-medium (money: Kotak's own 15:20 square-off, with its charges, closes the position; the failure is silence).

Scenario: server down through 14:30 (deploy, crash), back at 15:01. `scheduler.start()` calls `catch_up_square_off()`, which
only acts when `exit_time <= now < sell_until`; APScheduler's job store is in memory, so the missed 14:30 job isn't replayed.
`_record_missed_exit` is only reached from inside `run_exit`, which nothing calls at 15:01. Result: no run, no pop-up,
open intraday position. The existing test `test_a_square_off_past_its_last_moment_leaves_a_popup` calls
`trading.run_exit(user, "scheduled")` by hand after `catch_up_square_off()`, so it never exercised the real path.

Where: `backend/app/services/scheduler.py` `catch_up_square_off` (about line 190); `trading.py` `_record_missed_exit`.

Test (fails: `len(runs) == 0`): `_intraday_bought`; `clock.set(15:01)`; `scheduler.catch_up_square_off()`; sleep 1.5 s;
no `ExecutionRun(kind="exit")`, position unchanged, `/api/alerts == []`.

Smallest fix: in `catch_up_square_off`, after the `auto_exit` check, when `now >= sp.sell_until` (same day) call
`trading._record_missed_exit(uid, section, "catchup", now.date())` (it already dedupes and checks that the section traded
and no exit ran). Also call it only if auto_exit is on, as there. (When automatic square-off was switched off, `run_exit`
itself would record a "TradeLab was not running" pop-up after `sell_until` before looking at `auto_exit` — move the
`auto_exit` check ahead of `_record_missed_exit` too.)

### F4-3. Positions card says "no open position" and shows no Exit button when Kotak can't be read — CONFIRMED (API), PLAUSIBLE (UI trace)

Severity: low-medium (a safety message that reads an unreadable account as empty; the Exit/Sell buttons exist only inside
the card).

Scenario: F&O/Commodity/Holding position open in the only account (or in all accounts) and Kotak's positions call fails
(expired login, gateway page). `section_positions` returns `rows: []`, `errors: [..]`. In `PositionsCard`
(`frontend/src/components/SectionKit.tsx`, about lines 584-650) the branch `groups.length === 0` shows "TradeLab has no
open position in this section…" and the "Couldn't read … check the login" banner is rendered only inside the `else`
branch (when at least one row exists). The only Exit/Sell buttons are in the same card (`Trade.tsx` lines 217, 402), so
the owner sees an empty section with no way to start an Exit from there. (The panic button still works.) The API test
(`rows == []`, `errors` set, `unclear == []`) passes today; the rendering follows from the code.

Smallest fix: render the `data.errors` banner outside the groups branch, and show the "no open position" sentence only
when `data.errors.length === 0` (else "Couldn't read <accounts>; positions there are unknown").

### F4-4. Pop-ups vanish after two calendar days, so a failed Friday Exit / panic is not shown on Monday — PLAUSIBLE (code trace)

Severity: low. `ALERT_DAYS = 2` (`routers/alerts.py`): `since = today - 1 day`. A failed or partial F&O / Commodity / Holding
Exit or panic on Friday (NRML contracts, delivery units stay open over the weekend) is not listed on Sunday or Monday
morning; Activity still has it and the Positions card still shows the open position, but the red pop-up is gone before the
owner is back. Smallest fix: count back to the previous trading day (or 4 days) instead of 2 calendar days, or keep an
exit pop-up until dismissed or a later done exit covers it (see F4-1).

### F4-5. A funds pop-up for one account is dropped when a Buy has an entry failure in another account — PLAUSIBLE (code trace)

Severity: low (information only; the funds-short account simply didn't trade). `list_alerts` returns one pop-up per entry
run: `if names: kind="entry" ... elif r.funds_short: kind="funds"`. With account A short of money and account B failing for
another reason (login, refusal), only "entry: B" shows; "Not enough funds: A" is lost. C4 of round 3 fixed the same-account
case only. Fix: emit both pop-ups for the run (two items with the same `run_id`, different `kind`).

## Not found

Nothing real found in: `_writedown_lock` (no deadlock, no lock nesting, re-read is inside the lock, comparisons use the
same `tradelab_open` flags as the callers), `_stopped_at` scope (panic button only; Buy against `confirmed`; hedge wave
against its own start), `_record_missed_exit` dedupe lock, `settling` / `unclear` server lists, `npm run typecheck`
(clean), `launch` double taps (exit never refused, serialised by `_exit_lock`), alerts and dismiss scoping by `user_id`.
