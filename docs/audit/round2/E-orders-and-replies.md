# E — Order lifecycle and Kotak replies (round 2) — final report

## Checked so far

- Round-1 E file read (E1 pending-row overwrite, E2 "Not placed" clock): both fixed (`_settle_logs` keeps a row
  that is no longer pending, `reconcile_orders` skips `pending`, "Not placed" measured from `sent_at`).
- Installed SDK neo_api_client 3.0.7 (httpx): `NeoAPI.place_order/cancel_order/order_report/positions`,
  `OrderAPI.order_placing`, `rest.RESTClientObject.request`, `ApiException`, `retry.py` / `circuit_breaker.py`
  (present but imported nowhere: the SDK never resends an order itself), `totp.py` (token swap), validation.
- Adapter `kotak.py` order paths (`place_market_order`, `_unclear_reply`, `_error_message`), `manager.call`
  (only a SessionExpired raised from stCode 1003/403 or the SDK's pre-send "2fa" text resends), `_place`.
- New code: `_fire_in_waves`, `_hedges_to_wait_for`, `_same_underlying`, `_fire`, `_reserve_logs`, `_settle_logs`
  (one `sent_at` per batch, taken after the whole batch returned), `_await_final`, `_short_closes`; tested a lost
  reply on a buy-back (hedge sold once the book shows it; no second buy-back after a lagging book): both fine.

## Findings (most severe first)

### 1. CONFIRMED — an exception between sending a batch and settling its rows leaves the rows "pending" for good; Exit then says "holds nothing" with the position open

Severity: high impact, low likelihood (needs an exception in `_fire` after the orders went out, e.g. a database
hiccup in `_settle_logs`; the Buy run is shown as "Buy crashed", but the Kotak position is then forgotten).
Since the round-1 E1 fix `reconcile_orders` no longer looks at `pending` rows ("the run settles them itself"),
so nothing in a running server ever settles a row its run abandoned: only `recover_after_restart` turns `pending`
into `unknown`. `tradelab_book` counts an opening order only when `status == "success"`, so Exit / the panic
button's NRML part see nothing to close: "F&O holds nothing bought by TradeLab", run `done`, 65 units open.

Where: `backend/app/services/trading.py` `_fire` (reserve at `_reserve_logs`, then `ex.map`, `_settle_logs`,
`_await_final` with no try/except around them), `reconcile_orders` status filter (`success`, `unknown` only),
`tradelab_book` (`opening` needs `success`).

Test (fails today):

```python
def test_P1_a_row_left_pending_after_a_failed_settle_is_never_followed_up(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    real, calls = trading._settle_logs, {"n": 0}
    def boom(ids, results):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return real(ids, results)
    monkeypatch.setattr(trading, "_settle_logs", boom)
    _run(client, owner, "fno", "buy")                      # "Buy crashed: database hiccup"; the order is at Kotak
    ts = item["trading_symbol"]
    assert acct.net(ts, "NRML") > 0 and _logs()[0].status == "pending"
    trading.reconcile_orders(1)                            # still 'pending'
    monkeypatch.setattr(trading, "_settle_logs", real)
    _, run2 = _run(client, owner, "fno", "sell")           # 'done', "F&O holds nothing bought by TradeLab"
    assert acct.net(ts, "NRML") == 0                       # FAILS: 65
```

Smallest fix: in `_fire` (and the square-off loop) wrap everything after `_reserve_logs` in `try/except Exception`
and, on the way out, turn any of `ids` still `pending` into `unknown` (`sent_at=now`, the "may have gone through"
message, as `recover_after_restart` does), then re-raise. Belt and braces: have `reconcile_orders` first turn
`pending` rows whose `run_id` is not in `_live` into `unknown`.

### 2. CONFIRMED — Buy of a hedged pair leaves a naked short when the hedge's buy is refused (mirror of C5)

Severity: medium. Opening a credit spread (sold NIFTY put + bought lower put, both in the Buy list) sends the
sale and the hedge's purchase at the same time. If the hedge buy is refused (price band, RMS) and the sale goes
through, the account holds a naked sold option: unlimited-style loss and far more margin than the spread. The run
shows "partial · 1 failed", but nothing stops the sale. Round 1 (C-fno.md, finding 5) dismissed the opening
mirror as "only margin, not safety"; with a refused hedge it is a safety problem too.

Where: `backend/app/services/trading.py` `_fire` sends all of `plan` at once (`fire_plan(plan, ..., closing=False)`
from `_run_buy` and `run_entry` never orders buys before sells).

Test (fails today):

```python
def test_O1_buy_does_not_leave_a_naked_short_when_the_hedge_buy_is_refused(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", side="S")
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", side="B")
    acct = sim.account("AB101"); real = acct.place_order
    def refuse(**kw):
        if kw["transaction_type"] == "B" and kw["trading_symbol"] == hedge["trading_symbol"]:
            return {"stat": "Not_Ok", "emsg": "RMS: order price is outside the LPP range", "stCode": 1009}
        return real(**kw)
    acct.place_order = refuse
    _, run = _run(client, owner, "fno", "buy")
    s, h = acct.net(short["trading_symbol"], "NRML"), acct.net(hedge["trading_symbol"], "NRML")
    assert not (s < 0 and h == 0), (s, h, run.status, run.summary)   # FAILS: (-65, 0, 'partial', ...)
```

Smallest fix: reuse `_fire_in_waves` for opening: per account, send the buys of derivative contracts first, and
send a sale (side "S", derivative) only if every bought contract of the same underlying in that account was
filled (`_same_underlying`); otherwise report "Not sold: its hedge could not be bought first" as a failed entry.
(Sales alone, with no same-underlying buy in the plan, go out in the first wave as now.) Bonus: the sale then
gets the spread's lower margin.

### 3. CONFIRMED — after a server restart a square-off can close a lagging position a second time (flip)

Severity: medium-low (needs a restart during the square-off window and a positions reply that still shows the
old figure). `_squared` (what each account's square-offs already closed today, F-1) lives in memory only. After a
restart the next square-off (automatic retry / catch-up, panic) does not know about the earlier closes, and sizes
a new close from positions that still show +10: net becomes -10.

Where: `backend/app/services/trading.py` `_squared` / `square_off_account` / `_square_off_account` (`sent`).

Test (fails today):

```python
def test_R1_square_off_after_a_restart_does_not_close_a_lagging_position_twice(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")
    stale = acct.positions()                        # Kotak's positions reply lags: it keeps showing +10
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    acct.positions = lambda **kw: stale
    trading._squared.clear()                        # the server restarted
    trading.recover_after_restart()
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net("SBIN-EQ", "MIS") == 0          # FAILS: -10
```

Smallest fix: when `_squared` has no entry for the account today, rebuild `sent` from today's OrderLog rows of the
account written by exit runs (`order_id`, `tag` = `TL<id>`, keyed by segment/symbol/product), so the book lookup
and "filled so far" accounting see the earlier closes.

### 4. CONFIRMED — a square-off close whose request never reached Kotak blocks every later square-off for the rest of the day

Severity: medium. An intraday (MIS) position stays open (until Kotak's own auto square-off), and the panic button,
the automatic retry every minute and "Square off now" all do nothing for it but repeat "Position still open after
square-off attempts", even after TradeLab itself has recorded the first close as "Not placed".

Scenario: the square-off sends a close and gets no clear answer (gateway page, timeout) for an order Kotak never
received. `_square_off_account` remembers the close in `sent` (`e.status != "failed"`, so `UNKNOWN` counts) and,
being a per-account per-day memory (`_squared`), keeps it for the whole day. Every later attempt looks the close up
in the order book by its tag; it is never there, so `closes = [None]` -> `working = True` -> "wait for it", and
nothing is sent. The order's OrderLog row is turned into `failed, final, "Not placed: Kotak's order book has no
such order"` after `MISSING_AFTER`, but `sent` is not told.

Where: `backend/app/services/trading.py` `_square_off_account`: the `sent.setdefault(...).append(e.order_id or tag)`
after `_place` (about line 2101) and the `closes = [found.get(i) ...]` / `any(o is None ...)` wait (about line 2074).

Test (fails today):

```python
def test_S1_a_close_that_never_reached_kotak_blocks_every_later_square_off(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")
    acct.faults["place_order"] = [GATEWAY_PAGE] * 10          # the close never reaches Kotak
    run1 = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net("SBIN-EQ", "MIS") == 10 and run1.status != "done"
    acct.faults.clear()
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(seconds=0))
    trading.reconcile_orders(1)                                # TradeLab now knows: "Not placed", final
    assert _logs()[0].status == "failed" and _logs()[0].final
    run2 = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net("SBIN-EQ", "MIS") == 0                     # FAILS: 10, nothing was sent
```

Smallest fix: keep when each close was sent in `sent` (id, time); when the order book was read this attempt and
the close is not in it and `MISSING_AFTER` has passed, drop it from `sent[k]` (it was never placed), so the
position is sized and closed again. (Equivalent: drop ids whose OrderLog row is `failed` and `final`.)

## Nothing found in (checked, no bug)

- SDK never resends: `retry.py` / `circuit_breaker.py` are unused; rate limiting is off; `raise_on_error` off, so
  HTTP error bodies come back as JSON and are classified by `_unclear_reply` / `_error_message`.
- Only stCode 1003 / 403 or the SDK's pre-send "2fa" text renew the login and resend an order; timeouts, 5xx
  codes, gateway pages and unreadable bodies are `OrderOutcomeUnknown` and looked up by tag.
- `_settle_logs` / `reconcile_orders` after the E1/E2 fixes; `_fire_in_waves` (no order in both waves, hedge held
  on a refused, part-filled, unknown or still-working buy-back, lost reply found in the book, no second buy-back).

## Live checks that only Kotak can settle

- A 1-lot NRML option market order: the SDK always sends market protection "mp": "0"; confirm Kotak accepts it
  (a refusal is fail-safe, but Exit of options would then never close).
- That the order book echoes `ig` as `GuiOrdId` for NRML / MCX orders too (the tag lookup depends on it).
