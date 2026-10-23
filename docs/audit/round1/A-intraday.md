# A — Intraday (MIS), round 1 — in progress

## Checked so far

- Read end to end: `trading.py` (Let's Trade: `plan_date`, `session_state`, `arm`, `cancel_session`,
  `_claim_session`, `_begin_firing`, `run_entry`; sizing: `build_entry_plan`, `free_money`, `_size_equal`,
  `_size_custom`, `_prices`; placement: `fire_plan`, `_reserve_logs`, `_place`, `_settle_logs`, `_await_final`,
  `reconcile_orders`, `_apply_order`; square-off: `run_exit`, `square_off_account`, `_failed_cancels_today`,
  `accounts_traded_today`, `_schedule_exit_retry`, `_cancel_armed_today`, `launch`, `recover_after_restart`),
  `scheduler.py`, `sections.py`, `broker/kotak.py`, `broker/manager.py`, `broker/base.py`, routers
  `automation.py`, `sections.py`, `accounts.py`, `tests/kotak_sim.py`.
- Read the owner's user manual (Claude Doc) for what the square-off promises about the holder's own positions.
- Leads being confirmed with kotak_sim tests (not findings yet):
  1. The automatic 14:30 square-off closes the holder's own MIS positions (any stock) in every account TradeLab
     traded in, and cancels the holder's own working MIS orders. The manual says TradeLab "never closes a family
     member's own positions in the normal case", and lists only "Square off everything now" and "Square off now"
     as exceptions that close the holder's own intraday trades.
  2. `square_off_account` returns "No open positions" (run "done") right after Kotak *accepted* a cancel of a
     working MIS order (e.g. TradeLab's own entry still pending) without seeing it stopped in the book; review #5
     added `_confirm_cancels` for Exit / panic but not for this path.
  3. Each square-off run starts with fresh `first` / `sent` memory: a run right after another (manual "Square off
     now" then the 14:30 run, or a retry) does not know the earlier run's closes; with Kotak's positions still
     lagging that fill it would close again.

## Findings

### A1. CONFIRMED — the 14:30 square-off reports "done" right after Kotak *accepts* a cancel, while the order is still working (it can then fill and stay open, with no retry and no pop-up)

- Severity: a whole Intraday position of TradeLab's (or a new one from the holder's pending order) left open after a
  "done" square-off, until Kotak's own 15:05 auto square-off closes it with Kotak's charges (₹50 + GST per order)
  and 30+ minutes of market risk on the full position. Happens in normal use whenever an order is still working at
  14:30 with nothing filled yet in that symbol (e.g. TradeLab's 09:15 market buy left pending as a protected limit
  order at an upper circuit, or the holder's own pending MIS order) and Kotak's book lags the cancel.
- Scenario: `square_off_account` cancels the working order (step 1); Kotak accepts. No position is open in that
  symbol yet, so step 2 finds nothing to close and, on attempt 0, returns "No open positions" (or `break`s when other
  positions were closed). Nobody looks at the book again: the run is "done", `_already_ran` makes every retry skip,
  and no exit pop-up shows. Review #5 added `_confirm_cancels` for Exit / the panic button's own orders, but the
  intraday square-off's cancel step never waits for Kotak's book to show a cancelled order stopped unless a position
  exists in the same key (`held_back`).
- Where: `backend/app/services/trading.py:1855-1869` (cancel step; `held_back` only matters for a key with a
  position), `:1896-1901` (early "No open positions" return / `break` with cancels unconfirmed).
- Test (fails on the current code at `assert run.status != "done"`; summary "0 orders placed · 1 skipped"):

```python
def test_square_off_is_not_done_while_a_cancelled_entry_may_still_fill(client, owner, clock, sim):
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _stock(client, owner)                       # SBIN-EQ, Intraday
    acct = sim.account("AB101")
    acct.partial_next = 0  # the 09:15 buy is accepted and keeps working with nothing filled
    user = _user(client, owner)
    buy(client, owner)
    (entry,) = [o for o in acct.orders if o["trnsTp"] == "B"]
    assert entry["ordSt"] == "open" and entry["fldQty"] == 0
    acct.cancel_lag = 3  # Kotak accepts the cancel; the book shows the order working for 3 more reads
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert entry["ordSt"] == "open"  # Kotak's book right after the run: still working
    entry.pop("_cancel_in", None)    # the cancel reaches the exchange too late: the order fills
    entry.update(ordSt="complete", fldQty=entry["qty"], avgPrc="100.00")
    acct.open_positions[("nse_cm", "SBIN-EQ", "MIS")].buy += entry["qty"]
    clock.set(SQUARE_OFF + timedelta(minutes=1))
    assert trading.run_exit(user, "catchup", section="intraday") is None  # retry: "already done"
    assert acct.net("SBIN-EQ", "MIS") == entry["qty"]  # TradeLab's buy is open after the square-off
    assert run.status != "done", run.summary                                   # FAILS: "done"
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Acc A"]]
```

- Smallest fix: in `square_off_account`, remember the orders whose cancel Kotak accepted (`cancelling[order_id]`);
  on every attempt whose book read succeeded, drop those the book shows stopped (absent or not `is_open`), and treat
  any left as `working` (no "No open positions" early return and no `break` while any remain). After the loop,
  report each one still not shown stopped as a failed "cancel" entry with its symbol and product (the same message
  as `_confirm_cancels`), so the run is partial, retried, raises the pop-up, and the next run holds that key back.
