# E — Order lifecycle and Kotak replies (round 1) — in progress

## Checked so far

- SDK source (neo_api_client in /home/user/venv-tl): `place_order`, `cancel_order`, `order_report`, `positions`,
  `holdings`, `limits`, `margin_required`, `totp_login` / `totp_validate`, `rest.py` (timeouts and connect errors
  become `ApiException(status=0)`; `raise_on_error` is off, so HTTP 4xx/5xx bodies come back parsed as JSON, a
  non-JSON body as `{"Error": exc}`).
- Adapter `kotak.py`: `_error_message`, `_raise_for`, `_unclear_reply`, `_is_no_data`, `_report_rows`,
  `place_market_order`, `cancel_order`, `orders`, `positions`, `holdings`, `funds`, `margin_per_lot`, `login`.
- Broker manager `manager.py`: `call` (one re-login and retry on `SessionExpired`), `_ensure`, `_login`.
- `trading.py`: `_place`, `fire_plan`, `_reserve_logs`, `_settle_logs`, `_await_final`, `_short_closes`,
  `reconcile_orders`, `_apply_order`, `MISSING_AFTER`, `square_off_account`, `_cancel_own_open_orders`,
  `_confirm_cancels`, `tradelab_book`, `unfinished_keys`, `unconfirmed_orders`, `_fills_today`, `build_sell_plan`,
  `section_positions`, `_run_buy`, `_run_sell`, `recover_after_restart`, `launch`.
- `db.py` sessions: reads happen outside a transaction, a save writes only the changed fields with `$set` and, on
  a write conflict, replays the same `$set` blindly (last writer wins per field, no compare-and-set).
- Frontend: the section page's positions card polls `/api/sections/<section>/positions` every 30 s
  (`frontend/src/components/SectionKit.tsx:551`), and each poll runs `reconcile_orders(user_id)` over all of the
  user's unfinished rows of today.

## Findings

### 1. CONFIRMED — a rejection the positions poll records while a run is still sending is turned back into a fill; Sell then sells the holder's own units

Severity: high. Real money: TradeLab counts units Kotak never gave it (here 4,620 ITBEES), the run says "done",
and the next Sell sells the family member's own demat units in their place (here all 1,801 of Mummy Ji's own
ITBEES, about ₹64,700). For F&O / Commodity the same phantom closes the holder's own lots.

Scenario: a Buy (or Let's Trade entry, or Exit) sends orders to several accounts at once; one account's order is
accepted and rejected a moment later (RMS margin), another account's order is still on its way. The section page
is open, so its 30-second positions poll runs `reconcile_orders`, finds the rejected order in Kotak's book by its
tag and records it as failed and final. When the slow order returns, `fire_plan` calls `_settle_logs`, which
overwrites status, order id, message, quantity and price of every row with what `_place` saw ("Order placed")
and leaves `final=True`, so the order is never looked at again: a rejected order is now a final full fill. The
same overwrite also puts back the asked quantity over a part fill the poll recorded (and erases its fill price),
and for a close it makes TradeLab think the whole close went through (the rest is forgotten and `_short_closes`
reports "done").

Where: `backend/app/services/trading.py:1042-1053` (`_settle_logs` overwrites unconditionally, never looks at
`final`), `trading.py:1287-1289` (`reconcile_orders` also takes `pending` rows, i.e. rows of a run still sending),
`trading.py:1158-1161` (rows stay `pending` until the whole batch has returned), `trading.py:2731`
(`section_positions` → `reconcile_orders(user_id)` every 30 s).

Test (fails today; `_hold` makes one simulated account's `place_order` wait until released):

```python
def test_a_rejection_seen_by_the_positions_poll_while_the_buy_is_still_sending_is_kept(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Kanha", "AB101", cash=200.0)
    _add(client, owner, sim, "Mummy Ji", "CD202", cash=168389.84,
         demat={"ITBEES-EQ": [1801, 1801, 30.0]})  # her own units, bought outside TradeLab
    kotak_sim.PRICES["ITBEES-EQ"] = kotak_sim.TOKEN_PRICES["19084"] = 35.90
    _add_etf(client, owner, "ITBEES")
    kanha, mummy = sim.account("AB101"), sim.account("CD202")
    mummy.reject_in_book = "RMS:Margin Exceeds,Required:165000.00,Available:160000.00"
    entered, release, orig = _hold(kanha)
    _, run_id = _start(client, owner, "holding", "buy")
    _until(lambda: entered.is_set() and any(o["ordSt"] == "rejected" for o in mummy.orders))
    assert client.get("/api/sections/holding/positions", headers=owner).status_code == 200  # the 30 s poll
    (row,) = _logs(account_name="Mummy Ji", action="buy")
    assert row.status == "failed" and row.final  # the poll saw the rejection
    release.set()
    run = _wait(run_id)
    kanha.place_order = orig
    (row,) = _logs(account_name="Mummy Ji", action="buy")
    assert row.status == "failed"   # FAILS: ('success', final=True, 4620, 'Order placed · …', '2 orders placed')
    pv, sell_run = _run(client, owner, "holding", "sell")
    assert mummy.demat["ITBEES-EQ"][0] == 1801   # FAILS: 0 — Sell sold all of her own 1,801 units
```

Smallest fix: let exactly one writer settle a row at a time. (a) `reconcile_orders` leaves `pending` rows alone
(drop `"pending"` from its status filter at `trading.py:1288`; a restart already turns left-over `pending` rows
into `unknown`, `trading.py:3258`). (b) `_settle_logs` updates a row only while it is still `pending` (an atomic
`update_one({"id": id, "status": "pending"}, …)`), and if it is no longer pending keeps what the book said
(filling in the order id only if missing). Reconcile's own writes should likewise be conditional on the status
and `final` it read, so two overlapping reconciles (poll + `_await_final`) can't write an older book's verdict
over a newer one.

### 2. CONFIRMED — "Not placed" is decided 60 s after the row was reserved, not 60 s after the order was sent: a lost reply late in a long batch is taken as refused, and the position is forgotten

Severity: high impact, lower likelihood. A TradeLab position Kotak holds is forgotten (never closed by Exit, the
panic button or the evening checks), and Exit reports "done" with the lot still open (here one NIFTY lot, 65
units). For a close the same misreading makes TradeLab close a second time later (it believes the first close
never happened), which can take the holder's own units or flip the position.

Scenario: `MISSING_AFTER` says "orders not seen in Kotak's order book this long after being sent were never
placed", but `reconcile_orders` measures it from `created_at`, which `_reserve_logs` sets before the batch starts.
A batch takes longer than 60 s when Kotak is slow (more than 64 orders through the 32-thread pool with 30 s
timeouts, or a re-login waiting for a fresh TOTP code in the middle). An order sent late in that batch whose
reply is lost (timeout) and which Kotak's book shows a few seconds later is marked "Not placed: Kotak's order book
has no such order" and `final` by `_await_final`'s first look 2 s after the batch (or by the 30 s positions poll),
and is never looked at again.

Where: `backend/app/services/trading.py:1229-1230` (the rule), `trading.py:1322` (measured from `created_at`),
`trading.py:1029-1036` (`created_at` is the reservation time, before sending), `trading.py:1201-1211`
(`_await_final` looks 2, 5 and 10 s after the whole batch).

Test (scaled down: `MISSING_AFTER` 60 s → 1 s, orders sent one after another so the batch takes 1.2 s; Acc B's
order goes out last, its reply is lost, and Kotak's book shows it one read later):

```python
def test_after_a_lost_reply_late_in_a_long_batch_exit_still_closes_the_lot(client, owner, clock, sim, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(seconds=1))
    monkeypatch.setattr(trading, "FINAL_CHECKS", (0.05, 0.05, 0.05))
    monkeypatch.setattr(trading, "_pool", lambda n: ThreadPoolExecutor(max_workers=1))
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    a, b = sim.account("AB101"), sim.account("CD202")
    orig_a = a.place_order
    def slow_a(*args, **kw):
        time.sleep(1.2)  # Kotak slow: the batch takes a while
        return orig_a(*args, **kw)
    a.place_order = slow_a
    b.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]
    _lagging_book(b, reads=1)  # Kotak's book shows B's order one read later
    _run(client, owner, "fno", "buy")   # B's row: 'failed', "Not placed: Kotak's order book has no such order"
    a.place_order = orig_a
    ts, lot = item["trading_symbol"], item["lot_size"]
    pv, run = _run(client, owner, "fno", "sell")
    assert a.net(ts, "NRML") == 0
    assert b.net(ts, "NRML") == 0 or run.status != "done"   # FAILS: 65 units open, run 'done', '1 order placed'
```

(`_lagging_book` wraps the simulated account's `place_order` / `order_report` so a newly placed order is left out
of the next book read.)

Smallest fix: measure the 60 s from when the send finished: `_settle_logs` (and the square-off's per-order
settle) stamp a `sent_at`, and `reconcile_orders` uses `now - (row.sent_at or row.created_at)`; together with
finding 1's fix (reconcile leaves `pending` rows alone) a row is never judged while its order is still on its way.

## Leads being confirmed

- (none open right now; see "checked so far")
