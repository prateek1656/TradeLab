# A — Intraday (MIS), round 2 — final report

## Checked so far

- Read end to end at HEAD a4adb9b: `trading.py` Let's Trade (`plan_date`, `session_state`, `arm`, `cancel_session`,
  `_claim_session`, `_begin_firing`, `run_entry`, `build_entry_plan`, sizing, `_place`, `_fire`, `_await_final`,
  `reconcile_orders`, `_apply_order`), the whole square-off (`Scope`, `square_off_account`, `_square_off_account`,
  `_squared`, `accounts_traded_today`, `_wait_for_entries`, `_cancel_own_open_orders`, `_confirm_cancels`, `run_exit`,
  `_schedule_exit_retry`, `launch`, `recover_after_restart`), `scheduler.py` (jobs, misfire grace, catch-ups), the
  routers `automation.py` / `accounts.py` square-off endpoints, `broker/kotak.py` (`positions`, `orders`,
  `place_market_order`, `cancel_order`), `base.Order.is_open`, `kotak_sim.py`.
- F-2 attacked with the simulator: book unreadable on attempt 0 only (closes in the same run, run "partial", a retry
  then ends "done"); book breaking after a close was sent (no second close); unreadable book beside a holder's
  stop-loss that then reads on a later attempt (SL cancelled, one close). No double close and no close beside an
  unseen order found. The held-back / `book_read` / `held_back` bookkeeping holds up.
- Checked and fine: entry claim is atomic (`_claim_session`), panic vs a sleeping/firing entry, cancel vs re-arm,
  entry late tolerance and market-closed recheck, account switched off before the buy (`_still_on_autopilot`),
  `accounts_traded_today` statuses, sell/buy-back sizing `min(net, |first| - filled)`, `_squared` day reset and
  flat-forgetting, rejected / part-filled closes, the DB-retry code in `scheduler._with_db_retry` (see A-R2-1).
- Disclosed design, not reported: the 14:30 and manual square-offs close the holder's own MIS positions in accounts
  TradeLab bought in (and "Square off now" in every account); positions left open when the book stays unreadable until
  15:00 are Kotak's 15:05 auto square-off to deal with (the run says so).

## Findings


Summary: F-2 itself is sound (no close beside an unseen order, no double close, retries behave). Three real gaps
around the square-off's scheduling and retries; no money-moving bug in entry sizing, claiming or placement found.

### A-R2-1. CONFIRMED — a database blip at 14:30 (before the run row exists) loses the whole automatic square-off: no run, no retry, no pop-up

- Severity: every TradeLab MIS position stays open until Kotak's own 15:05 square-off (₹50 + GST per order, 35
  minutes of market risk on the full positions), with nothing in the app saying the 14:30 run never happened.
  Needs a MongoDB hiccup (primary election, restart, network) of a few seconds at 14:30, or at any retry minute.
- Scenario: `scheduler._start_exit` relies on `_with_db_retry` ("if the database doesn't answer, try again until
  15:00"). But `run_exit` catches every exception itself (`except Exception` at the bottom of its try). A
  `PyMongoError` from `_already_ran`, `accounts_traded_today`, the accounts query or `_start_run` is therefore
  swallowed: `run_id` is still None, nothing is recorded (`_finish_run` is skipped), `status` stays None so
  `_schedule_exit_retry` is not called, and `_with_db_retry` never sees an exception. `_already_ran` is false, but
  nothing calls `run_exit` again until a restart or a settings change. The same hole ends the Timer retry chain
  (a blip during a retry minute) and drops the retry when `_finish_run` itself fails to save.
- Where: `backend/app/services/trading.py` `run_exit` (the outer `except Exception` ~line 2382, `if automatic and
  status is not None and status != "done"` ~line 2392); `backend/app/services/scheduler.py:86-96`.
- Test (fails: "runs []", position still +250):

```python
def test_a_database_blip_at_14_30_does_not_lose_the_square_off(client, owner, clock, sim, monkeypatch):
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _stock(client, owner)                       # SBIN-EQ, Intraday
    acct = sim.account("AB101")
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    buy(client, owner)
    assert acct.net("SBIN-EQ", "MIS") > 0
    real, calls = trading.accounts_traded_today, {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AutoReconnect("primary stepped down")
        return real(*a, **kw)

    monkeypatch.setattr(trading, "accounts_traded_today", flaky)
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic))
    clock.set(SQUARE_OFF)
    scheduler._start_exit(user)                  # what the 14:30 cron job runs
    time.sleep(1)
    assert acct.net("SBIN-EQ", "MIS") == 0       # FAILS: still +250, no ExecutionRun row exists
```

- Smallest fix: in `run_exit`, remember a failure before the run started or while finishing it (`crashed = True`
  in the outer `except` when `run_id is None`, and around `_finish_run`), and for an automatic run call
  `_schedule_exit_retry(user_id, section)` when `status != "done"` or `crashed` (a plain `status is None` would
  also retry no-trade days, so use the flag). Alternatively re-raise `PyMongoError` for automatic runs that have not
  started a run row, so `_with_db_retry` does its job.

### A-R2-2. CONFIRMED — the one-minute retry of a failed automatic square-off ignores the "Automatic square-off" switch

- Severity: low-to-medium, rare. The owner sees the 14:30 run fail (pop-up), switches Automatic square-off OFF to
  deal with the position themselves, and TradeLab still cancels their working orders (a stop-loss they just set)
  and sells/buys the position at 14:31, 14:32 ... until 15:00. A hand close plus the retry can flip the account.
- Scenario: `_schedule_exit_retry` starts a `threading.Timer` that calls `run_exit(user, "catchup")` directly.
  Unlike the 14:30 cron job (`_start_exit`) and `catch_up_square_off`, which both check
  `get_section(...).auto_exit`, neither the Timer nor `run_exit` re-checks the switch. Changing settings calls
  `settings_changed` -> `catch_up_square_off` (which respects the switch) but the pending Timer is untouched.
- Where: `backend/app/services/trading.py` `_schedule_exit_retry` (~2399-2410), `run_exit` automatic branch.
- Test (fails: position closed although the switch is off):

```python
def test_retry_timer_ignores_auto_exit_switch(client, owner, clock, sim, monkeypatch):
    user, acct = _setup(client, owner, sim)               # entry bought SBIN (+250) in Acc A
    held = acct.net("SBIN-EQ", "MIS")
    monkeypatch.setattr(trading, "EXIT_RETRY", timedelta(seconds=0.3))
    acct.faults["order_report"] = GATEWAY_PAGE            # 14:30 run: book unreadable -> partial, retry scheduled
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert run.status != "done" and acct.net("SBIN-EQ", "MIS") == held
    assert client.put("/api/automation/settings", json={"auto_exit": False}, headers=owner).status_code == 200
    del acct.faults["order_report"]
    time.sleep(4)
    assert acct.net("SBIN-EQ", "MIS") == held             # FAILS: 0, the retry closed it
```

- Smallest fix: in `run_exit`, for `trigger == "catchup"` (and "scheduled") read `get_section(...).auto_exit` after
  the `_already_ran` check and return without a run if it is off (the same check `_start_exit` makes).

### A-R2-3. CONFIRMED (guard missing) — a scheduled square-off that fires after 15:00 still sends closes, possibly beside Kotak's own 15:05 square-off

- Severity: low, rare; if it collides it can flip a full position (both sell the same units). The design promise
  ("TradeLab's square-off is at 15:00 at the latest, so the two never close the same position at the same
  moment") is enforced by the settings validation, `catch_up_square_off` (`< sell_until`) and the retry
  (`when >= sell_until`), but not by the cron job itself: the exit job has `misfire_grace_time=3600` and
  `coalesce=True`, so a stalled process (VM pause, host sleep, blocked scheduler thread) that wakes at 15:04:40
  runs the 14:30 job, and `_start_exit` -> `run_exit` has no time check. Closes then race Kotak's 15:05 run.
- Where: `backend/app/services/scheduler.py:86-96` (`_start_exit`), `sync_jobs` grace 3600.
- Test (fails: closes are sent at 15:04:40):

```python
def test_late_scheduled_exit_after_15_00(client, owner, clock, sim):
    user, acct = _setup(client, owner, sim)
    held = acct.net("SBIN-EQ", "MIS")
    clock.set(SQUARE_OFF.replace(hour=15, minute=4, second=40))
    scheduler._start_exit(user)
    time.sleep(1)
    assert acct.net("SBIN-EQ", "MIS") == held     # FAILS: 0
```

- Smallest fix: in `_start_exit`'s `go` (and `run_exit` for automatic triggers) return unless
  `now_ist().time() < spec(section).sell_until`; and/or lower the exit job's `misfire_grace_time` to a few minutes.

## Notes (not findings)

- F-2 trade-off: a single malformed row in Kotak's order book (`orders()` raises on any row without `nOrdNo`,
  `fldQty` or a status) now keeps the square-off from closing anything in that account, every day, until 15:05.
  Intended fail-safe; worth knowing if the owner ever sees "order book couldn't be read" daily for one account.
- A close order TradeLab left working (illiquid stock) is never re-priced by the retries; after 15:00 it stays
  working beside Kotak's 15:05 run. Reported to the owner as "Still working at Kotak"; not changed.
- Kotak SDK calls have no explicit timeout in the adapter (area E): a hung call in one account holds `_exit_lock`
  and so queues the panic button behind it for that user.
- Positions lagging a fill of the holder's cancelled stop-loss (book says complete, positions still show the
  position) can still make the square-off close and flip; needs a coincident stop trigger and a lag (round 1 A2
  covers TradeLab's own closes only). Too many independent conditions to report.
