# A — Intraday (MIS), round 4 — final report

## Checked so far

- Read `git diff f360223 HEAD -- backend/app` and the live code of `_earlier_closes`, `square_off_account`,
  `_square_off_account` (the `rebuilt` / derived `first` / `lost` paths, the memory save in `finally`), `run_exit`
  (`_stopped_at` only for the real panic, `exclude_run` cancel, crash rows written by `_write_logs`),
  `_record_missed_exit`, `_already_ran`, `arm` (IntegrityError -> 409), `_claim_session`, `run_entry`, the scheduler's
  `catch_up_square_off` / `_start_exit` / `sync_jobs` grace, and the alert list (`routers/alerts.py`).
- Re-ran round 3's A3-1 tests against HEAD (partial close then restart; reopened position then restart), with the
  logged closes aged by 3 minutes so the 60 s hold is over: both pass. The rebuilt `first` = net + what the earlier
  closes filled gives `left = |net|` in every case I traced (partial close, full close then reopen, short side,
  close still working -> waits, close not in the book -> waits, unreadable book -> waits).
- Randomized harness: 250 seeds `intraday` (start 70000), 500 seeds each of `intraday`, `race`, `broad`
  (start 81000): no violation.
- Checked and sound: the hold-back until MISSING_AFTER (a run inside the minute reports "Not closed yet…" and the
  1-minute retry closes it); an unresolved memory is not saved so the next run rebuilds; `lost` (log unreadable)
  holds every position and says so; `exclude_run` cancels none of the panic's own closes but still cancels an earlier
  run's working order and every Buy; per-account panic no longer stops other accounts' Buys (intended);
  the two ways `arm` can race (two creators -> IntegrityError -> 409; second tap after the first launched -> busy ->
  409). Not reported: a second tap landing in the few milliseconds between the first tap's launch and its run
  claiming the session can cancel the armed session (needs two taps within ~10 ms, past `_entry_busy`).
- Not reported (design / needs several failures): out-of-scope keys of a rebuilt memory are dropped from the
  in-process memory when a narrower-scope run follows (same as before the change).

## Result

Nothing that loses or double-trades money in normal use. Two LOW gaps in how the owner is told about a square-off
that did not (fully) happen. Both are alerting gaps; no order is sent wrongly.

### A4-1. CONFIRMED (LOW) — a server that was down for the whole 14:30–15:00 window leaves no pop-up: `_record_missed_exit` is unreachable in that case

- Money: MIS positions of accounts TradeLab bought in stay open until Kotak's own square-off (about Rs 50 + GST per
  order plus market risk), and the owner is never told that TradeLab's square-off did not run. (The commit's intent:
  "leaves a failed run behind so the owner gets the pop-up".)
- Scenario: crash / deploy / VM restart from 14:25 to 15:10. At start-up `catch_up_square_off` only acts while
  `exit_time <= now < sell_until`, so at 15:10 it does nothing; APScheduler's 14:30 job is not replayed (in-memory
  jobstore, and its grace of `sell_until - exit_time` has passed anyway); `_schedule_exit_retry` stops before
  `sell_until`. Nothing ever calls `run_exit`, so `_record_missed_exit` (only caller: `run_exit`, trading.py ~2562)
  is not reached. The existing test calls `run_exit` by hand at 15:01, which hides this.
- Where: `backend/app/services/scheduler.py` `catch_up_square_off` (~204: `< sp.sell_until`).
- Test (fails on HEAD; helpers from `test_kotak_replies_end_to_end.py`):

```python
def test_server_down_over_the_whole_square_off_window_leaves_no_popup(client, owner, clock, sim):
    user = _intraday_bought(client, owner, sim)
    acct = sim.account("AB101")
    held = acct.net("SBIN-EQ", "MIS")
    clock.set(SQUARE_OFF.replace(hour=15, minute=10))
    scheduler.catch_up_square_off()          # what start() does
    time.sleep(0.5)
    assert acct.net("SBIN-EQ", "MIS") == held
    assert _alerts(client, owner), "the owner is never told the 14:30 square-off did not run"   # FAILS: []
```

- Smallest fix: in `catch_up_square_off`, also call `run_exit(uid, "catchup", section=section)` when
  `now >= sp.sell_until` on a trading day (it only records the missed row; `run_exit` already refuses to trade then
  and `_record_missed_exit` is idempotent and checks `_already_ran` / `accounts_traded_today`). Also make
  `_record_missed_exit` treat a manual done exit run of the section as "ran" (today `_already_ran` counts only
  scheduled / catch-up runs, so a "Square off now" at 14:00 followed by such a start-up would show a false
  "didn't run" pop-up) and skip it when Automatic square-off is switched off (that check comes after it in `run_exit`).

### A4-2. CONFIRMED (LOW) — a "done" per-account "Square off" hides the panic's (or another account's) failure from the pop-up list

- Money: a TradeLab MIS position left open by a failed panic close (e.g. A's close lost at Kotak) is no longer named in
  any pop-up once the owner squares off a different account from the Accounts screen; the position is still in
  Positions and Activity, but the alert is the safety net.
- Scenario: panic -> account A fails (pop-up names A) and B is fine; before the pop-up is read the owner taps
  "Square off" on B (or on A when both A and B had failed: then B's failure disappears). That run is
  `kind=exit, section=ALL`, same key as the panic, `status=done` and later, so `list_alerts` drops the panic's failed
  run (`done_ids.get(key) > r.id`). The collapse assumes a later done run covers the same accounts.
- Where: `backend/app/routers/alerts.py` `list_alerts` (`done_ids` keyed by `(section, trading_date)` only).
- Test (fails on HEAD):

```python
def test_a_done_square_off_of_one_account_hides_the_panics_failure_in_another(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "VERIFY_DELAY", 0.05)
    a = _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    b = _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    sa, sb = sim.account("AB101"), sim.account("CD202")
    sa.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")
    sa.faults["place_order"] = [GATEWAY_PAGE] * 50
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert run.status != "done" and sa.net("SBIN-EQ", "MIS") == 10
    assert [x["accounts"] for x in _alerts(client, owner)] == [["Acc A"]]
    _wait(client.post(f"/api/accounts/{b}/square-off", headers=owner).json()["run_id"])   # done, 1 skipped
    assert _alerts(client, owner)          # FAILS: []
```

- Smallest fix: a done run supersedes an older failed one only if it covered every account the failed run named:
  in `list_alerts` take the accounts of the done run's `OrderLog` rows (or store `account_ids` on the run) and keep the
  failed run unless its failed account names are all among them (a per-account run has `account_id` rows only for
  its account).
