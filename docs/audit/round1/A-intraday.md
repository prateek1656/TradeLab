# A — Intraday (MIS), round 1 — in progress (A1 fixed in f31496a)

## Checked so far

- Read end to end: `trading.py` (Let's Trade: `plan_date`, `session_state`, `arm`, `cancel_session`,
  `_claim_session`, `_begin_firing`, `run_entry`; sizing: `build_entry_plan`, `free_money`, `_size_equal`,
  `_size_custom`, `_prices`; placement: `fire_plan`, `_reserve_logs`, `_place`, `_settle_logs`, `_await_final`,
  `reconcile_orders`, `_apply_order`; square-off: `run_exit`, `square_off_account`, `_failed_cancels_today`,
  `accounts_traded_today`, `_schedule_exit_retry`, `_cancel_armed_today`, `launch`, `recover_after_restart`),
  `scheduler.py`, `sections.py`, `broker/kotak.py`, `broker/manager.py`, `broker/base.py`, routers
  `automation.py`, `sections.py`, `accounts.py`, `tests/kotak_sim.py`.
- Read the owner's user manual (Claude Doc) for what the square-off promises about the holder's own positions.
- Lead 1 (the 14:30 square-off closes the holder's own MIS positions in accounts TradeLab bought in) is disclosed
  design, not a code bug: the Let's Trade dialog says "Squared off at 14:30: every intraday (MIS) stock position in
  the accounts TradeLab bought in, and their pending intraday orders" (also README, Settings). Only the manual's FAQ
  ("Will TradeLab sell my family member's own shares?") is out of step: it names just the two manual buttons as the
  exception. See note N1.
- Lead 2 confirmed as A1 (fixed in f31496a; my test passes on it). Reviewed the fix: absent orders stay
  unconfirmed, no early "No open positions" while a cancel is unconfirmed — looks right.
- Lead 3 confirmed as A2 (still fails on HEAD 6a20745 + the main session's uncommitted changes).
- Next: square-off near Kotak's 15:05 (late automatic runs), retries vs the auto-exit switch, entry-time changes
  after the entry job started, "No Data" positions against TradeLab's own fills.

## Findings

### A1. CONFIRMED, FIXED in f31496a — the 14:30 square-off reports "done" right after Kotak *accepts* a cancel, while the order is still working (it can then fill and stay open, with no retry and no pop-up)

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

### A2. CONFIRMED (needs Kotak's positions to lag a fill by ~2.5 s or more) — a second square-off right after the first sells the whole position again (the account flips short)

- Severity: a full-size reverse MIS position (TradeLab sells the whole position a second time; for a short it buys
  twice). The first run's pop-up and the second run's "now on the other side" entry report it, and a retry a minute
  later (if before 15:00) closes the flip: the cost is an extra round trip on the full position over ~1 minute plus
  charges; after ~14:59 it is left to Kotak's 15:05 square-off.
- Scenario: every `square_off_account` call starts with empty `first` / `sent` (`trading.py:1877`, HEAD 6a20745), so
  the protection against Kotak's positions lagging a fill only covers closes sent by the same call. Run 1 (the 14:30
  run) closes 250 SBIN; its attempt 1 and final check (2 s later) still see +250 in the lagging positions, so it ends
  "partial: Position still open". Run 2 (the owner's "Square off everything now", "Square off now" on the account
  card, or a second automatic run that passed `_already_ran` at `:2186` before run 1's `_start_run`) waits on
  `_exit_lock` (`:2222`), starts right after, sees the same stale +250 with no memory of run 1's close
  (`:1947`, `:1958`) and sells 250 again. The same gap exists for a working order the run cancels (a holder's
  stop-loss, TradeLab's part-filled entry, or a previous run's close still working): fills it got just before the
  cancel landed are not subtracted if the positions don't show them yet.
- Where: `backend/app/services/trading.py:1877, 1947-1958` (per-call memory only), `:2186` / `:2222` (back-to-back
  runs).
- Test (fails on the current code: `[250, 250]`, run 1 "1 order placed · 1 failed", run 2 "1 order placed"):

```python
def _lag_positions(acct, seconds: float) -> None:
    """Kotak's positions show each fill only `seconds` after its order book does."""
    real_place, real_positions = acct.place_order, acct.positions
    state = {"until": 0.0, "stale": None}

    def place_order(*a, **kw):
        before = real_positions()
        res = real_place(*a, **kw)
        state["stale"], state["until"] = before, time.monotonic() + seconds
        return res

    def positions(**kw):
        return state["stale"] if time.monotonic() < state["until"] else real_positions(**kw)

    acct.place_order, acct.positions = place_order, positions


def test_a_second_square_off_right_after_the_first_does_not_close_again(client, owner, clock, sim, monkeypatch):
    monkeypatch.setattr(trading, "VERIFY_DELAY", 2.0)  # the real pauses
    monkeypatch.setattr(trading, "CANCEL_DELAY", 1.0)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _stock(client, owner)                               # SBIN-EQ, Intraday
    acct = sim.account("AB101")
    user = _user(client, owner)
    buy(client, owner)
    held = acct.net("SBIN-EQ", "MIS")
    _lag_positions(acct, 3.0)
    clock.set(SQUARE_OFF)
    first = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert acct.net("SBIN-EQ", "MIS") == 0  # the 14:30 run closed it
    second = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    sells = [o["qty"] for o in acct.orders if o["trnsTp"] == "S"]
    assert sells == [held], (sells, first.summary, second.summary)   # FAILS: [250, 250]
    assert acct.net("SBIN-EQ", "MIS") == 0
```

- Smallest fix: before closing a key, compare Kotak's positions with its own order book: if today's filled buys
  (or sells) in the book for that key exceed the position's `day_buy_qty` (or `day_sell_qty`, flBuyQty / flSellQty),
  the positions lag the book — treat the key as `working` (wait) and, if it is still so after the last attempt,
  report it ("Kotak's positions don't show the latest fills yet …") instead of closing. That covers every run and
  every cancelled order, not just this call's closes. (Alternative: seed `sent` with today's earlier square-off
  closes of that account from the order log and size from the book; more code.)
