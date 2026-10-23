# C — F&O (round 1)

Auditor C (F&O: NRML contracts on nse_fo / bse_fo). Findings are confirmed with a failing test through the real
adapter (`kotak_sim`), scratch file `backend/tests/review_C_scratch_test.py` (deleted at the end; the test code is
copied here).

## Checked so far

- Read end to end: `trading.py` (entry plan for lots, `_size_lots`, `_lot_margins`, `_current_contracts`,
  `_outside_position`, `_max_order_units` / `_slices` / `_interleave`, `fire_plan` / `_await_final` /
  `_short_closes`, `reconcile_orders` / `_apply_order`, `run_exit(ALL)` NRML part, `_cancel_own_open_orders` /
  `_confirm_cancels`, `tradelab_book`, `broker_view`, `_shrink_to_broker`, `_expired_contract`,
  `unfinished_keys` / `unconfirmed_orders`, `build_sell_plan`, `_run_sell`, `_run_buy`, `launch` and locks),
  `contracts.py`, `sections.py`, `scheduler.py`, `broker/kotak.py`, `broker/manager.py`, `broker/base.py`,
  routers `sections`, `basket`, `stocks`, `automation`, `accounts` (square-off).
- Slicing: each slice is its own pending row with its own tag, sent once; an unclear slice is looked up, never
  resent; a failed slice leaves its units in the ledger and the run partial. Fallback slice sizes stay under
  the index freeze limits for today's lot sizes. No double order found here.
- Sell-first next to the holder's long, and buy next to the holder's short: `_outside_position` skips them;
  Exit sizes by min(TradeLab's records, Kotak's net), so the holder's same-side units are left.
- Carried positions (cfBuyQty / cfSellQty) on later days: net and day quantities read correctly; no write-down
  while today's positions lag TradeLab's own fill of today.
- Expiry: on expiry day the contract is still in the list (not "expired"); the day after, Kotak's empty figure
  writes the record off (`_expired_contract`), Exit then shows nothing for it. No double close found.
- Finding 1 (per-account square-off) CONFIRMED on HEAD `853c3a3`; the main session has since applied the
  suggested fix in the working tree, and the test passes with it.
- Finding 2 (Buy plan ignores orders the book never confirmed) CONFIRMED.
- Finding 3 (Exit / panic "done" beside a stop-loss that can open a new position) CONFIRMED.

## Findings (most severe first)

### 1. CONFIRMED — One account's "Square off" closes TradeLab's F&O and Commodity contracts in every account

**Severity: high.** Tapping "Square off" on one account (Accounts screen) closes, at market, every NRML contract
TradeLab holds in *all* family accounts — futures and written options the owner meant to keep — with the charges,
slippage and realised P/L that brings, and it can't be undone. In the other accounts TradeLab's own working orders
are not cancelled first (the cancel step is limited to the chosen account), so a part-filled opening order there
can keep filling right after its position was "closed".

**Scenario.** TradeLab holds 1 lot of NIFTY FUT in Acc A and in Acc B. The owner taps "Square off" on Acc A only.
TradeLab sells the lot in Acc A *and* in Acc B ("2 orders placed").

**Where.** `backend/app/routers/accounts.py:213` launches `run_exit(..., [account_id], section=ALL)`;
`run_exit` passes `account_ids=[a.id for a in accounts]` to `build_sell_plan` (`backend/app/services/trading.py:2177`),
but `build_sell_plan` uses `account_ids` only for the "no longer trades in this section" note (`trading.py:2613`)
and reads the ledger for every account: `ledger = tradelab_open(user_id, section, None, ...)` (`trading.py:2617`).

**Test** (fails on the current code: Acc B's net is 0, not 65):

```python
def test_one_accounts_square_off_closes_contracts_only_in_that_account(client, owner, clock, sim):
    """The Accounts screen's "Square off" on Acc A must not close TradeLab's contracts in Acc B."""
    clock.set(IN_SESSION)
    a = _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    _, run = _run(client, owner, "fno", "buy")
    assert run.status == "done", run.summary
    acc_a, acc_b = sim.account("AB101"), sim.account("CD202")
    assert acc_a.net(ts, "NRML") == lot and acc_b.net(ts, "NRML") == lot

    r = client.post(f"/api/accounts/{a}/square-off", headers=owner)
    assert r.status_code == 200, r.text
    run = _wait(r.json()["run_id"])
    assert acc_a.net(ts, "NRML") == 0, run.summary
    # Acc B was not asked to be squared off: its contract stays open.
    assert acc_b.net(ts, "NRML") == lot, run.summary
```

(Helpers `_add`, `_run`, `_wait`, `_add_contract` and the `sim` fixture are those of
`backend/tests/test_kotak_replies_end_to_end.py`.)

**Smallest fix.** In `build_sell_plan`, read the ledger only for the accounts asked for:
`ledger = tradelab_open(user_id, section, account_ids, symbols, products, closes_by_fill=preview)`.
Exit passes `account_ids=None` (every account, as now), the panic button passes every account, the per-account
square-off passes its one account. Keep `chosen` as it is for the note.

### 2. CONFIRMED — A sell-first (or buy) order can close the holder's own lot beside a TradeLab order Kotak's book never confirmed

**Severity: medium** (a family member's own contract is closed at market; needs an unreadable order book).
Review #3 made Exit and the panic button refuse to size a close from TradeLab's records while an opening order of
today is accepted but not yet shown in Kotak's order book (`unconfirmed_orders`). The Buy plan has no such check:
`build_entry_plan` lets an order go out on the other side of TradeLab's own position ("First closes the 65
TradeLab bought") using records that still count the unconfirmed order in full. If that order was in fact
rejected, the order closes the holder's units instead.

**Scenario.** The holder carries 1 lot of NIFTY FUT of their own. TradeLab buys 1 lot; Kotak accepts it, then its
RMS rejects it while the order book can't be read. The owner switches the item to "sell first" and taps Buy.
TradeLab's records say it holds 65, Kotak shows 65 (the holder's), so `_outside_position` finds nothing outside
TradeLab and the sell goes out: the holder's lot is closed. (The mirror case: the holder's own short, TradeLab's
invisible rejected sell, item switched to buy.)

**Where.** `backend/app/services/trading.py:812-814` (ledger and `unfinished`, no `unconfirmed_orders`),
`trading.py:865` (`_outside_position` with the uncorrected ledger), `trading.py:893` (the "closes TradeLab's own
position first" case).

**Test** (fails: the holder's lot is closed, net 0):

```python
GATEWAY_PAGE = {"message": "Service Unavailable"}


def test_a_sell_first_never_closes_the_holders_lot_beside_an_order_the_book_never_confirmed(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "B")  # the holder's own lot
    acct.reject_in_book = "RMS: margin not available"
    acct.faults["order_report"] = GATEWAY_PAGE
    _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == lot  # only the holder's lot: TradeLab's buy was rejected
    r = client.patch(f"/api/basket/{item['id']}", json={"side": "S"}, headers=owner)
    assert r.status_code == 200, r.text
    pv, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == lot, (run.summary, pv["orders"])  # the holder's lot is never closed
    assert not [o for o in acct.orders if o["trnsTp"] == "S"]
```

**Smallest fix.** In `build_entry_plan` (lots only) read `unconfirmed = unconfirmed_orders(user_id, account_ids)`
next to `unfinished`, and skip with an error (the message Exit already uses: "Kotak's order book hasn't confirmed
TradeLab's last order for this yet, so nothing is sent for it…") any item whose key has an unconfirmed TradeLab
order when the item's order would go against TradeLab's own position (`ours > 0 if side == "S" else ours < 0`), or
simply whenever the key has an unconfirmed order.

### 3. CONFIRMED — Exit and the panic button report "done" while a stop-loss on the contract can still open a new position

**Severity: medium-high** (a fresh, unwatched futures position — unlimited risk — can open after TradeLab said
everything was closed). TradeLab has no stop-loss of its own, so protecting a sold option or a future with a
stop-loss order in the Kotak Neo app is the natural thing for the owner to do. Kotak keeps such an order working
after the position is closed. Exit (`_run_sell`) and the panic's NRML part cancel only TradeLab's own orders and
never look at other working orders on the contract, so they close the position next to the stop-loss and report
"done". When the stop-loss fires it opens a new position in the other direction, which TradeLab doesn't count and
Exit won't close. Intraday's square-off already guards against exactly this ("closing next to an order still
working would flip the position"), but the NRML paths don't.

**Scenario.** TradeLab sells 1 lot of NIFTY FUT first; the owner adds an SL-M buy for 65 in the Kotak Neo app.
Exit buys the lot back: run "done", the SL-M is still "trigger pending". If NIFTY rises to the trigger, the account
is suddenly long 65 NIFTY futures with nobody watching. Same with the panic button.

**Where.** `backend/app/services/trading.py:3193-3200` (`_run_sell`: cancel own orders, build, fire — no check of
other working orders), `trading.py:2167-2186` (panic NRML part), `_cancel_own_open_orders` (`trading.py:1996`)
by design only TradeLab's own orders.

**Test** (fails for both: `('trigger pending', 'done', ...)`):

```python
@pytest.mark.parametrize("how", ["exit", "panic"])
def test_exit_never_says_done_beside_a_stop_loss_that_would_open_a_new_position(client, owner, clock, sim, how):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut", side="S")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == -lot, run.summary
    sl = acct.add_open_order(ts, "nse_fo", "NRML", "B", lot)  # stop-loss placed in the Kotak Neo app
    if how == "exit":
        _, run = _run(client, owner, "fno", "sell")
    else:
        run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary
    still = acct.order(sl)["ordSt"]
    # Either the stop-loss is gone, or the run says it is still there (never a plain "done").
    assert still == "cancelled" or run.status != "done", (still, run.status, run.summary)
```

**Smallest fix.** Don't cancel the order (it may protect the holder's own units), but report it: after the closes,
for each closed key, sum the working orders in that account's book that aren't TradeLab's (order id / tag not in
TradeLab's rows), same segment, symbol and product, on the closing side; if that sum is more than what Kotak still
shows on the position's side, add a failed entry such as "Closed, but another order on this contract (a stop-loss,
for example) is still working at Kotak and would open a new position if it fills. Cancel it in the Kotak Neo app."
The run is then partial and the exit pop-up names the account. (Checking before the close and holding the close
back, as Intraday's square-off does, also works, but leaves the position open.)
