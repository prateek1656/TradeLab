# E — Order lifecycle and Kotak replies (round 2) — in progress

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

## Findings

### 1. CONFIRMED — a square-off close whose request never reached Kotak blocks every later square-off for the rest of the day

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
