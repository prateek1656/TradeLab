# C — F&O (round 2)

Auditor C, round 2. Findings are confirmed with failing tests through the real adapter (`kotak_sim`); the scratch
file `backend/tests/review_C_scratch_test.py` is deleted at the end, the test code is copied here. Helpers `_add`,
`_run`, `_wait`, `_logs`, `_alerts`, `_add_contract`, `_command`, `_placed`, `_refuse_buy_backs`, the `sim` fixture and
`IN_SESSION`/`GATEWAY_PAGE` are those of `backend/tests/test_kotak_replies_end_to_end.py`. `_pair` (used below):

```python
def _pair(client, owner, sim, lots, ucc="AB101", name="Acc A", cash=50_000_000.0, short_q="nifty 25000 pe",
          hedge_q="nifty 24500 pe", buys=1):
    _add(client, owner, sim, name, ucc, cash=cash)
    client.put("/api/sections/fno", json={"max_lots": 25, "sizing_mode": "custom"}, headers=owner)
    short = _add_contract(client, owner, "fno", short_q, side="S", lots=lots)
    hedge = _add_contract(client, owner, "fno", hedge_q, side="B", lots=lots)
    acct = sim.account(ucc)
    for _ in range(buys):
        _, run = _run(client, owner, "fno", "buy")
        assert run.status == "done", run.summary
    return acct, short["trading_symbol"], hedge["trading_symbol"], short["lot_size"]
```

## Checked so far (no finding)

- **Input bounds** (the gap left by round 1): `PUT /api/sections/fno` `max_lots` 0, -1, 26, 1.5, 1e12 are refused (422),
  1 / 25 / 10.0 / "10" accepted; `POST /api/basket` for F&O: lots 0, 0.5, -3, 1001, 1e9, 1e-300,
  2.0000000000000004, 999.9999999999999 refused, 1000 accepted; `amount`/`percent` refused for contracts; `qty` is
  turned into lots; `sizing_value` is `le=1e9, allow_inf_nan=False`; `reserve_pct` 0–90. `_size_lots` caps a fixed
  size by `min(custom, money fits, max_lots)`; margin missing/odd stops the item. Nothing out of bounds gets through.
- **C5 two-wave close, attacked** and holding: a 50-lot sliced buy-back (27 + 23 lots) with the second slice refused
  keeps the whole hedge (3250 units) and reports partial; a buy-back whose reply is lost but placed resolves from the
  book and the hedge is then sold; a buy-back with an unclear reply and no order keeps the hedge; two underlyings
  (NIFTY + BANKNIFTY pairs) with only the NIFTY buy-back refused: only the NIFTY hedge is held; the panic button with a
  short that has an unconfirmed order (`unclosed` row + confirmed part) buys back the confirmed part and holds the
  hedge ("Not sold…", "Still working…", "hasn't confirmed…"); the blocked hedge is a failed row with the account's
  name, so the run is partial and the pop-up (`/api/alerts`) names the account; `_run_sell` needs no `unclosed`
  because its plan keeps the skipped/error rows (they come back as failed entries from the first wave); the pools
  cannot deadlock (every `_fire` makes its own pool); a crashed account returns a failed entry. A hedge held back is
  not retried inside the run, only by tapping Exit again (works, tested by the commit's own test).
- **The F-2 open question (NRML part sends closes with the order book unreadable)**: judged safe from flipping a
  position. A close is sized from TradeLab's records ∩ Kotak's net; any contract with a TradeLab opening order of today
  the book hasn't confirmed is held back (`unconfirmed_orders`); a close TradeLab sent earlier counts in full in the
  records (no second close); cancelling own orders fails visibly when the book can't be read (a second Exit with the
  book down ended `failed`, "Could not read orders to cancel…", position unchanged, nothing sent twice); the only
  thing a stop-loss in the Kotak app can do is reported afterwards by `_stray_orders`. Tested: carried short, close
  rejected in the book while the book is down — run 1 partial, run 2 failed (not "done").
- Slicing (`_slices`, `_max_order_units`, `_interleave`) re-read: each slice its own pending row/tag, sent once;
  fallback cap `SLICE_LOTS` (20) stays under today's index freeze limits for today's lot sizes.

## Findings (most severe first)

### 1. CONFIRMED — Buy does not protect a hedged pair: the sold leg goes out beside its hedge, and a refused or half-filled hedge leaves a naked short with the run "done" or only "partial"

**Severity: medium-high.** The two-wave rule exists only for closing. Buy (`_run_buy` → `fire_plan`, no `closing`)
sends the sold-first leg and the hedge at the same moment. The hedge is the cheap far-out-of-the-money option, the
contract Kotak/NSE refuse market orders on most often (LPP range, price band, market-protection). If it is refused
the account ends with the naked short: more risk and margin than the owner chose, no hedge. For a Buy the run is
only "partial" and **no pop-up** appears (`/api/alerts` lists only exit runs and funds-short entries). Worse, if the
hedge part-fills and the rest is rejected/cancelled, the row says "Only 65 filled; the rest was rejected" but its
status is `success`, so the **run is "done"** and nobody is warned (an entry has no `_short_closes`).

**Scenario A.** Items: sell first 1 lot NIFTY 25000 PE, buy 1 lot NIFTY 24500 PE. The hedge's market order is refused
(RMS/LPP). Result `-65 / 0`, run `partial`, no alert. **Scenario B.** 2 lots each, hedge fills 1 lot then the rest is
rejected: short -130, hedge +65, run **"done"** "2 orders placed", no alert.

**Where.** `trading.py` `_run_buy` (`fire_plan(plan, user_id, run_id, section)`, ~l.3446) and `fire_plan`/`_fire`
(l.1165, l.1246): no ordering between a sold-first and a bought contract of one underlying; `_await_final` turns a
part-filled entry into a `success` row with a smaller quantity but nothing marks the run.

**Tests** (both fail):

```python
def test_buy_of_a_hedged_pair_never_leaves_the_short_naked_when_the_hedge_is_refused(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=50_000_000.0)
    client.put("/api/sections/fno", json={"max_lots": 25, "sizing_mode": "custom"}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", side="S", lots=1)
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", side="B", lots=1)
    acct = sim.account("AB101")
    real = acct.place_order

    def refuse_hedge(**kw):
        if kw["trading_symbol"] == hedge["trading_symbol"]:
            return {"stat": "Not_Ok", "emsg": "RMS: order price is outside the LPP range", "stCode": 1009}
        return real(**kw)

    acct.place_order = refuse_hedge
    _, run = _run(client, owner, "fno", "buy")
    s, h = acct.net(short["trading_symbol"], "NRML"), acct.net(hedge["trading_symbol"], "NRML")
    assert not (s < 0 and h == 0), (s, h, run.status, run.summary)   # fails: (-65, 0, 'partial', '1 order placed · 1 failed')


def test_buy_hedge_part_filled_then_rejected_run_not_done(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=50_000_000.0)
    client.put("/api/sections/fno", json={"max_lots": 25, "sizing_mode": "custom"}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", side="S", lots=2)
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", side="B", lots=2)
    acct = sim.account("AB101")
    real = acct.place_order

    def hedge_part(**kw):
        if kw["trading_symbol"] == hedge["trading_symbol"]:
            acct.partial_next = 65
            acct.reject_in_book = "RMS: LPP"
        return real(**kw)

    acct.place_order = hedge_part
    _, run = _run(client, owner, "fno", "buy")
    assert run.status != "done", (run.status, run.summary)   # fails: 'done', '2 orders placed'; net -130 / +65
```

**Smallest fix.** Mirror C5 on the way in. In `_run_buy` for lots sections: per account, first fire every bought
contract (and sold-first ones with no bought contract of the same underlying), then fire a sold-first contract only if
every bought contract of its underlying (`_same_underlying`, unknown counts as same) in that account was placed and
fully filled (final, `row.quantity == planned`); otherwise skip it with a failed row "Not sold: its hedge wasn't bought
in full". Also in `_fire`, for a lots-section entry whose `row.quantity < planned` (or still working at the end) add
the same failed "Only N of M filled" entry that `_short_closes` adds for closes, so the run is partial, and let
`/api/alerts` include `entry` runs of lots sections that are partial/failed. (The sold leg's margin is sized without
hedge benefit, so selling after the hedge is never short of margin.)

### 2. CONFIRMED (timing) — The panic button starts the F&O/Commodity closes only after every account's Intraday square-off has finished, so trouble in one account delays every account's short buy-backs

**Severity: medium** (an unwatched sold option/future stays open during a crash for as long as the slowest intraday
account keeps retrying: 11 s measured with one slow close, up to ~25–30 s with `SQUAREOFF_ATTEMPTS = 5`; C5 adds
another wave of at least 2 s, up to 10 s, before the hedge). Hedged accounts pay twice.

**Where.** `run_exit` (`trading.py` ~l.2330–2380): `ex.map(square_off_account, accounts)` finishes for all accounts
(each retrying up to 5 × (`CANCEL_DELAY` 1 + 2 × `VERIFY_DELAY` 2) s), only then `_cancel_own_open_orders`,
`reconcile_orders`, then F&O, then Commodity one after the other (a Commodity short waits for F&O's two waves too).

**Test** (real delays patched back in; prints the time the NRML orders were sent; fails if more than 3 s):

```python
def test_panic_nrml_is_not_delayed_by_trouble_in_another_accounts_intraday(client, owner, clock, sim, monkeypatch):
    import time as _t
    from conftest import buy
    from test_kotak_replies_end_to_end import _family
    _family(client, owner, sim)                       # Kanha AB101, Mummy Ji CD202
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                     "exchange_segment": "nse_cm"}, headers=owner)
    buy(client, owner)                                # intraday positions
    _add(client, owner, sim, "Acc C", "EF303", cash=50_000_000.0)
    client.put("/api/sections/fno", json={"max_lots": 25, "sizing_mode": "custom"}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", side="S", lots=1)
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", side="B", lots=1)
    ids = [a["id"] for a in client.get("/api/accounts", headers=owner).json()]
    for it in (short, hedge):
        client.patch(f"/api/basket/{it['id']}", json={"account_ids": [ids[2]]}, headers=owner)
    clock.set(IN_SESSION)
    _run(client, owner, "fno", "buy")
    acc_c, mummy = sim.account("EF303"), sim.account("CD202")
    mummy.partial_next = 1                            # one account's intraday close part fills and keeps working
    for name, val in (("VERIFY_DELAY", 2.0), ("CANCEL_DELAY", 1.0), ("FINAL_CHECKS", (2.0, 3.0, 5.0))):
        monkeypatch.setattr(trading, name, val)
    t0, stamps, real = _t.monotonic(), [], acc_c.place_order
    acc_c.place_order = lambda **kw: (stamps.append((kw["transaction_type"], round(_t.monotonic() - t0, 1))), real(**kw))[1]
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"], timeout=120)
    assert stamps[0][1] < 3, stamps     # fails: [('B', 11.1), ('S', 13.1)]
```

**Smallest fix.** In `run_exit` start the NRML part for each account as soon as that account's own intraday
square-off is done (or just in a second thread beside the intraday pool, after `_cancel_own_open_orders`), and run
`fno` and `commodity` plans in parallel (they share no account/contract). The F&O/Commodity cancel step is
independent of the intraday one.

### 3. CONFIRMED — Exit on a hedge row alone (per-position Exit, or "Sell what TradeLab bought") sells the hedge and leaves the sold leg naked, with no warning

**Severity: low-medium** (owner action, but the confirmation sheet says nothing, and Kotak's RMS may then refuse or
square off the unhedged short at a bad price). C5 only orders closes that are in the same plan; with
`symbols=[hedge]` or `opened="B"` the sold leg isn't in the plan or in `unclosed`, so the hedge is sold at once.

**Where.** `build_sell_plan` (`symbols`/`opened` filters on the ledger, `trading.py` ~l.2877–2895) with `fire_plan`'s
`_hedges_to_wait_for` seeing no short.

**Test** (fails: short -65, hedge 0, nothing warned):

```python
def test_exit_of_the_hedge_row_alone_leaves_naked_short(client, owner, clock, sim):
    clock.set(IN_SESSION)
    acct, s_ts, h_ts, lot = _pair(client, owner, sim, 1)
    pv, r = _command(client, owner, "fno", "sell", symbols=[h_ts])
    run = _wait(r.json()["run_id"])
    assert not (acct.net(s_ts, "NRML") < 0 and acct.net(h_ts, "NRML") == 0), (run.status, run.summary)
```

**Smallest fix.** In `build_sell_plan` (lots sections), when a bought contract is selected for a sale and TradeLab
holds a sold-first contract of the same underlying in that account that isn't being closed in the same plan, either
hold the sale with a failed row ("Not sold: the sold X it hedges stays open — close both together") or, if the owner
should be able to do it, put the warning into the preview's `orders` note and require the owner's "Yes".

## Notes (not findings)

- Equal sizing (`sizing_mode: equal`) splits money equally per item, so a short and its hedge get unrelated lot
  counts (the hedge, cheap per lot, usually hits `max_lots`; an expensive hedge could be smaller than the short).
  Use custom lots for pairs; nothing in the UI says so. Not changed by the findings above.
- With more than 31 accounts hedged at once, `_fire_in_waves` runs accounts in batches of `max_parallel_orders` (32);
  later accounts' buy-backs wait for earlier accounts' two waves. Before C5 every close went at once. Unlikely at the
  family's size.
- After wave 2 the panic/Exit `_stray_orders(plan)` still checks a blocked (unsold) hedge, so a holder's own working
  sell order there could print a misleading "Closed, but another order…" line for a hedge that was never sold.
