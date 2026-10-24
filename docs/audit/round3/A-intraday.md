# A — Intraday (MIS), round 3 — final report

## Checked so far

- Read `git diff 7632274 HEAD` for `trading.py` / `scheduler.py` / `alerts.py` / `kotak.py` and the live code of: `run_exit`
  (automatic guards: sell_until, auto_exit switch, `_already_ran`; `lost` / `unsaved` handling, `_record_lost_exit`,
  `_schedule_exit_retry`), `square_off_account` / `_square_off_account` (memory, `first`/`sent`/`when`, MISSING_AFTER
  drop with `_tags_echoed`, hedge hold, odd-lot refusal, crash path with `answered` tags), `_earlier_closes`,
  `reconcile_orders` / `_abandoned_to_unknown` / `_apply_order`, `_fire`, per-account panic flow (`account_close`,
  `carry_forward`, `_account_pool`), `recover_after_restart`, `catch_up_square_off`, `_start_exit` / `_with_db_retry`,
  `sync_jobs` grace, `arm` / `cancel_session` / `_claim_session` / `run_entry`, `build_entry_plan` and the sizing helpers,
  the panic and per-account square-off endpoints, the alert list and pop-up.
- Checked and sound: the automatic guards (late job, switch off, retry chain ends before sell_until); lost-run retry
  (failed row, no `_already_ran` hit, retry timer; `_finish_run` failure -> retry); a crashed account in a panic keeps the
  others' results and still starts its own carry-forward; the unconfirmed-close drop only with tag echo for that
  segment and 60 s; crash while placing marks the remaining tags as sent and the pending rows unknown; hedge hold
  (short bought back first, long sold only once the positions show no short, reported if it never does);
  `reconcile_orders` from several accounts at once (rows are per account, writes are field-level `$set`, `_abandoned_to_unknown`
  re-checks `_live` after reading and only updates rows still "pending"); entry claim/firing atomicity, panic vs
  sleeping entry, late-wake-up tolerance, account switched off before firing. Ran test_lets_trade, test_scheduler, test_trading: pass.
- Disclosed design, not reported: the 14:30 / manual square-offs close the holder's own MIS positions in accounts
  TradeLab bought in.

## Result

One real but narrow gap (A3-1, LOW, CONFIRMED). Nothing else real found in this area: no way found for an entry to fire
twice, late, on a holiday, after the panic button, or for a switched-off account, and no way for a square-off to
close more than TradeLab's first-seen position or to skip a position silently (every miss ends in a failed row,
a partial run and the pop-up).

### A3-1. CONFIRMED (needs a server restart during the day) — the square-off memory rebuilt after a restart lacks `first`, so it under-closes (or never closes) a position it had partly closed, or one that was closed and opened again

- Severity: LOW, rare (a restart between 09:15 and 15:00 plus a partial close or a reopened position). Money: the rest of
  a MIS position stays open (e.g. 100 of 250 SBIN) until Kotak's own 15:05 square-off (₹50 + GST per order, ~30 min of
  market risk on that part). It is reported (failed row, partial run, pop-up), but the 1-minute retries can never
  fix it, and the message can be wrong ("Kotak's order book shows this position closed, but its positions don't show
  that yet").
- Scenario A (partial close): the 14:30 run sends a 250 sell; Kotak fills 100 and rejects the rest; the server is
  restarted right then (or crashes). `_earlier_closes` rebuilds `sent` (the 100-filled close) but not `first`.
  The catch-up run sees the position already reduced to 150, so `first[k]` = 150, and subtracts the earlier close's
  fill again: `left = min(150, 150 - 100)` = 50. It closes 50, 100 remain. The next retry has `first` = 150 in memory
  and filled = 150, so `left` = 0 for good.
- Scenario B (reopened): the position was closed earlier in the day (e.g. "Square off now" at 11:00), a new MIS
  position in the same stock opens (the holder, in an account TradeLab bought in), the server restarts, the
  14:30 run: `first[k]` = new net 40, filled of the old, finished close = 250, `left = min(40, 40 - 250)` < 0 -> nothing
  is closed; retried every minute with the same result. Without a restart the memory drops flat keys (`flat`) and this
  does not happen.
- Where: `backend/app/services/trading.py` `_earlier_closes` (~2061) returns only `sent` / `when`;
  `square_off_account` (~2084) starts with `first = {}`; `_square_off_account` `start = first.setdefault(k, p.net_qty)`
  and `left = min(abs(p.net_qty), abs(start) - filled)` (~2204-2217).
- Test (fails on HEAD; copy kept at the scratchpad `A_scratch_copy.py`, helpers from `test_kotak_replies_end_to_end.py`):

```python
def _setup(client, owner, sim, cash=25000.0):
    _add(client, owner, sim, "Acc A", "AB101", cash=cash)
    r = client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                         "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text
    buy(client, owner)
    return client.get("/api/auth/me", headers=owner).json()["id"], sim.account("AB101")

def test_reopened_position_after_restart_is_not_closed(client, owner, clock, sim):
    user, acct = _setup(client, owner, sim)
    clock.set(SQUARE_OFF.replace(hour=11))
    _wait(trading.run_exit(user, "manual", section="intraday"))
    assert acct.net("SBIN-EQ", "MIS") == 0
    acct.place_order("nse_cm", "MIS", "0", "MKT", "40", "DAY", "SBIN-EQ", "B")   # a new position
    trading._squared.clear()
    trading.recover_after_restart()                                              # restart
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert acct.net("SBIN-EQ", "MIS") == 0, run.summary     # FAILS: 40, "0 orders placed · 1 failed"

def test_partial_close_then_restart_under_closes(client, owner, clock, sim, monkeypatch):
    import time as _t
    from types import SimpleNamespace
    monkeypatch.setattr(trading, "VERIFY_DELAY", 7.77)
    def sleep(sec):
        if sec == 7.77:
            raise RuntimeError("process killed right after the close was sent")
        _t.sleep(min(sec, 0.05))
    user, acct = _setup(client, owner, sim)
    held = acct.net("SBIN-EQ", "MIS")
    clock.set(SQUARE_OFF)
    acct.partial_next = 100
    acct.reject_in_book = "RMS: outside circuit"        # the close fills 100, the rest is rejected
    monkeypatch.setattr(trading, "time", SimpleNamespace(sleep=sleep, monotonic=_t.monotonic, time=_t.time))
    _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert acct.net("SBIN-EQ", "MIS") == held - 100
    monkeypatch.setattr(trading, "time", _t)
    monkeypatch.setattr(trading, "VERIFY_DELAY", 0.05)
    trading._squared.clear()
    trading.recover_after_restart()
    run2 = _wait(trading.run_exit(user, "catchup", section="intraday"))
    assert acct.net("SBIN-EQ", "MIS") == 0, run2.summary    # FAILS: 100 left, "1 order placed · 1 failed"
```

- Smallest fix: keep no-over-close but stop double-subtracting. In `_earlier_closes`, also return each close's sent
  time (`sent_at or created_at`, for ids with an order number too). In the attempt loop, for a key with no `first`
  entry, set `first[k] = p.net_qty` and count in `filled` only closes that are still open or were sent less than
  ~30 s ago (positions have caught up with older ones), i.e. rebuilt closes older than that are treated as already
  inside `p.net_qty`. (Alternative: persist `first` per account and day in a small collection and reload it here.)
