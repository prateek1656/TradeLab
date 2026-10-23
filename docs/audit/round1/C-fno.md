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
- Findings 1–3 CONFIRMED and fixed by the main session (`c384daf`, `f31496a`); the fixes were read: the Buy plan
  now skips a contract with an unconfirmed TradeLab order before any sizing, and `_stray_orders` only reports
  (never cancels or sends). Finding 4 (bonus / split) CONFIRMED, next to fix. Finding 5 (Exit order of closes for a
  hedged short) CONFIRMED.
- Input bounds: `max_lots` ≤ 25 per order (`schemas.SectionIn`), fixed lots ≤ 1000 but capped by `max_lots`;
  slicing covers any gap to the freeze limits. Sanity test passed: a 50-lot NIFTY close goes out in two slices
  under 1800, one slice's reply lost (but placed) and one refused; closing again closes exactly the rest, no
  double order.

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

### 4. CONFIRMED — After a bonus or split on a stock F&O contract, TradeLab closes only part of its position and says "done", or forgets a sold option

**Severity: high when it happens** (TradeLab's own futures or *sold stock options* stay open, unwatched, and stock
F&O is physically settled at expiry), **rare** (a stock F&O position held over a bonus, split or rights ex-date).
NSE adjusts open stock F&O positions for corporate actions: the position's units and the lot size are multiplied
by the adjustment factor, and options move into new contracts with the adjusted strike (a new trading symbol).
TradeLab's records keep the units it traded, and nothing notices the change:

- *Futures* (same symbol): Exit closes min(TradeLab's old units, Kotak's units). With a 1:1 bonus on 2 lots of
  RELIANCE FUT (lot 500 → 1000, 1000 units → 2000), Exit sells 1000 (a valid 1-lot order at the new size), TradeLab's
  records reach 0 and the run is "done" — 1000 of TradeLab's units stay open. (With 1 lot the close of 500 is not
  a multiple of the new lot and Kotak refuses it, which is at least reported.)
- *Options* (new symbol): the old symbol is gone from Kotak's list and positions, so `_shrink_to_broker` writes
  TradeLab's record off ("Closed outside TradeLab: Kotak shows 0 of the -500 TradeLab opened") — on any look:
  the 15:40 true-up, the Exit screen, Exit. Exit then says "F&O holds nothing bought by TradeLab" (done), while
  the sold call sits in the adjusted contract with twice the units, to be settled by delivery at expiry.

The Holding section warns about splits (`_looks_like_split`); F&O has no equivalent.

**Where.** `backend/app/services/trading.py:2690` (close sized from old units), `trading.py:2520` with
`_expired_contract` (`trading.py:2363`: "not in today's list" counts as expired even before the expiry date) and
the `view.rows > 0` evidence, `trading.py:974` (only new orders use today's lot size).

**Tests** (both fail: `(1000, 'done', '1 order placed', …)` and `(-1000, 'done', 'F&O holds nothing bought by
TradeLab · 0 orders placed', ["Closed outside TradeLab: Kotak shows 0 of the -500 TradeLab opened, …"], [])`).
The exchange's adjustment is modelled in the test (units multiplied; options moved to the adjusted contract):

```python
from dataclasses import replace as _replace

from app.services import contracts
from kotak_sim import _Pos


def _trading_days_back(n: int) -> timedelta:
    today = datetime.now(IST).date()
    day = today
    for _ in range(n):
        day -= timedelta(days=1)
        while not trading.is_trading_day(day):
            day -= timedelta(days=1)
    return today - day


def _age_rows(by: timedelta) -> None:
    """Make every record look `by` older (record times use the real clock)."""
    with session_scope() as db:
        for row in db.find(OrderLog, {}):
            row.created_at = row.created_at - by


def _bonus_adjust(acct, old_ts: str, new_contract, factor: int) -> None:
    """NSE's bonus / split adjustment: the open position's units are multiplied (options also move into a new
    contract with the adjusted strike, so a new trading symbol)."""
    seg = contracts.contract_master._segs["nse_fo"]
    rows = [c for c in seg.contracts if c.trading_symbol != old_ts] + [new_contract]
    contracts.contract_master._set("nse_fo", rows, "kotak", contracts._today().isoformat())
    with acct.lock:
        p = acct.open_positions.pop(("nse_fo", old_ts, "NRML"))
        acct.open_positions[("nse_fo", new_contract.trading_symbol, "NRML")] = _Pos(
            "nse_fo", new_contract.trading_symbol, "NRML", cf_buy=max(p.net, 0) * factor, cf_sell=max(-p.net, 0) * factor)


def test_a_bonus_on_a_stock_future_never_leaves_half_of_tradelabs_position_open_as_done(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 2, "sizing_mode": "custom"}, headers=owner)
    item = _add_contract(client, owner, "fno", "reliance fut", lots=2)
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == 2 * lot, run.summary
    trading.true_up_all()
    acct.settle_overnight()
    _age_rows(_trading_days_back(1))
    clock.set(IN_SESSION + timedelta(days=1))
    old = contracts.contract_master.get("nse_fo", ts)
    _bonus_adjust(acct, ts, _replace(old, lot_size=2 * lot), 2)  # 1:1 bonus: lot 500 -> 1000, units doubled
    assert acct.net(ts, "NRML") == 4 * lot
    pv, run = _run(client, owner, "fno", "sell")
    # All of it is TradeLab's: either it is all closed, or the run says something is left (never a plain "done").
    assert acct.net(ts, "NRML") == 0 or run.status != "done", (acct.net(ts, "NRML"), run.status, run.summary,
                                                                pv["orders"])


def test_a_bonus_on_a_sold_stock_option_is_never_forgotten(client, owner, clock, sim):
    clock.set(IN_SESSION)
    seg = contracts.contract_master._segs["nse_fo"]
    fut = next(c for c in seg.contracts if c.symbol == "RELIANCE")
    opt = _replace(fut, trading_symbol=f"RELIANCE{fut.expiry:%y%b}3000CE".upper(), token="S900001",
                   instrument_type="OPTSTK", option_type="CE", strike=3000.0)
    contracts.contract_master._set("nse_fo", seg.contracts + [opt], "kotak", contracts._today().isoformat())
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "reliance 3000 ce", side="S")
    ts, lot = item["trading_symbol"], item["lot_size"]
    assert ts == opt.trading_symbol
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == -lot, run.summary
    trading.true_up_all()
    acct.settle_overnight()
    _age_rows(_trading_days_back(1))
    clock.set(IN_SESSION + timedelta(days=1))
    new = _replace(opt, trading_symbol=f"RELIANCE{fut.expiry:%y%b}1500CE".upper(), token="S900002", strike=1500.0,
                   lot_size=2 * lot)
    _bonus_adjust(acct, ts, new, 2)  # the sold call now sits in the adjusted 1500 CE, 2 x the units
    acct.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "SBIN-EQ", "B")  # any other position today
    trading.true_up_all()  # the 15:40 check
    pv, run = _run(client, owner, "fno", "sell")
    still = acct.net(new.trading_symbol, "NRML")
    adjusted = [r.message for r in _logs(status="adjusted")]
    # TradeLab's sold call is still open at Kotak: it must not be written off as closed while the run says "done".
    assert still == 0 or run.status != "done", (still, run.status, run.summary, adjusted, pv["orders"])
```

**Smallest fix.** (a) Keep the lot size a position was opened at (store `lot_size` on each lots-section order row, or
use the basket item's saved `lot_size`) and, in `build_sell_plan` for lots sections, when today's lot size differs,
send nothing for that key and add an error row: "This contract's lot size changed from X to Y (a bonus, split or
other corporate action) and Kotak shows Z units here: TradeLab didn't close it — check the Kotak Neo app." (b) In
`_shrink_to_broker`, write off a lots-section record whose contract is missing from today's list only once its
expiry date has passed (store the expiry on the row, or read it from the basket item / trading symbol); before
that, keep it and report "no longer in Kotak's contract list though it hasn't expired (a corporate action may have
moved it into another contract) — check the Kotak Neo app".

### 5. CONFIRMED — Exit sends a hedge's sale together with the sold leg's buy-back, so a refused buy-back leaves a naked short

**Severity: medium** (the account ends up with a naked sold option or future — far more risk and margin than the
hedged position it had before the owner tapped Exit; Kotak's RMS may then close it at a bad price). The run is
reported as partial, so it isn't silent, but Exit made things worse. `fire_plan` sends every close of a run at
once (interleaved only by account), so the sale of a bought option that hedges a sold one goes out at the same
moment as the sold one's buy-back. If the buy-back is refused (Kotak's market-protection / LPP range in a fast
market, a price band, a freeze), the hedge is already sold. The reverse also bites: Kotak's RMS refuses a hedge's
sale that would leave the short leg short of margin when it is processed first, so Exit often needs a second tap.

**Scenario.** TradeLab sold 1 lot of NIFTY 25000 PE and bought 1 lot of NIFTY 24500 PE as its hedge. The owner taps
Exit; the 25000 PE buy-back is refused ("order price is outside the LPP range"), the 24500 PE is sold. The account
is now short a naked put: "1 order placed · 1 failed".

**Where.** `backend/app/services/trading.py:3286` (`_run_sell`) and `trading.py:2261` (panic) call `fire_plan` with
every close; `fire_plan` sends them all at once (`trading.py:1167`, `trading.py:1176`).

**Test** (fails: `(-65, 0, '1 order placed · 1 failed')`):

```python
def test_exit_never_sells_the_hedge_while_the_sold_leg_stays_open(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", side="S")
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", side="B")
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    s_ts, h_ts, lot = short["trading_symbol"], hedge["trading_symbol"], short["lot_size"]
    assert acct.net(s_ts, "NRML") == -lot and acct.net(h_ts, "NRML") == lot, run.summary
    real = acct.place_order

    def refuse_buy_backs(**kw):
        if kw["transaction_type"] == "B":
            acct.calls.append(("place_order", {"trading_symbol": kw["trading_symbol"], "side": "B", "refused": True}))
            return {"stat": "Not_Ok", "emsg": "RMS: order price is outside the LPP range", "stCode": 1009}
        return real(**kw)

    acct.place_order = refuse_buy_backs
    _, run = _run(client, owner, "fno", "sell")
    assert run.status != "done", run.summary
    # Never a naked short: while the sold put is still open, its hedge is still held.
    assert not (acct.net(s_ts, "NRML") < 0 and acct.net(h_ts, "NRML") == 0), (
        acct.net(s_ts, "NRML"), acct.net(h_ts, "NRML"), run.summary)
```

**Smallest fix.** Close in two waves in `_run_sell` and the panic's NRML part: first `fire_plan` the buy-backs of
what TradeLab sold first (side "B" rows); then the sales (side "S" rows), leaving out — with an error row such as
"Not sold yet: a sold contract on the same underlying in this account didn't close, and selling this one now
could leave it unhedged. Close again." — any sale in an account where a buy-back of the same underlying
(`contract.symbol`) failed, ended short or is unknown. (Opening could get the mirror rule — buys before sells —
but that only affects margin, not safety.)
