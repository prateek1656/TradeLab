# C — F&O (round 5)

Auditor C, round 5. Scratch test `backend/tests/review_C_scratch_test.py` (deleted at the end).

## Checked so far

- Read `git diff eddbe8a HEAD` (trading.py, alerts.py, scheduler.py, models.py, SectionKit.tsx) and round1-4 C reports + HANDOFF.
- Zero-row "No Data" write-down (`_shrink_to_broker`, `_traded_today`): traced; only keys with no success / pending / unknown
  TradeLab order today are written from an empty reply; part-filled-then-rejected rows become "success" so they count as traded.
  No hole found (live check: Kotak's positions reply 09:00-09:15 for a carried NRML position, see Notes).
- Buy refusal (`STALE_RECORD`, `unlisted`): only fires for an opposite-side record the write-down could not remove (traded today,
  unfinished, contract changed); the hedge-first / sold-leg-waits waves keep a refused hedge from leaving a naked short.
- `_flat_at_kotak` / `_hold_hedges`: held only where unfinished / respelled / traded-today-and-unlisted / contract changed; pooled
  net 0 (holder's opposite lot) equals the existing write-down semantics.
- Old unknown NRML closes (`unresolved_old_closes`, `_finish_unknown`, `old_closes` hold) and old unknown entries (`gone`, `old`)
  across days, expiry: see finding 1 for the one defect.
- Scope-aware pop-ups (`_set_scope`, `list_alerts`): per account + contract coverage, collapsed names, dismiss of earlier attempts: no
  hole found (a crashed later run with no account rows counts as covering its scope, but it shows its own pop-up).
- Fuzz (`TL_FUZZ_PROFILE=derivs|hedge|broad`, `TL_FUZZ_NOECHO=1`): 300 + 400 + 400 + 400 seeds, see the end.

## Findings (most severe first)

### 1. CONFIRMED (low-medium) - finishing an old unknown close uses up the write-down's turn: the record stays stale (or turns into a phantom opposite record) until the next look, and a holder's new lot of the same contract is then sold

**Severity: low-medium.** After TradeLab's records were brought "down to Kotak" (flat/unlisted key), they can still be wrong
for one more look. If the holder opens the same contract in that window, Exit sells HER lot (the exact thing write-downs exist
to stop). Needs: an old unknown NRML close (lost reply, never reached Kotak, not provable), TradeLab opening more of the contract
afterwards, the position then closed outside TradeLab, and the holder taking the contract again before the next true-up.

**Scenario.** Day 1 TradeLab buys 1 lot; Exit's close gets no reply and never reached Kotak (row stays `unknown`). Day 2 TradeLab buys
a second lot (records +2). The owner closes both lots in the Kotak Neo app. Day 3 Kotak lists nothing. The true-up (or any Exit /
Buy look) runs `_shrink_to_broker`: Kotak shows 0 of 2, so it should write the whole 2 down. It first calls `_finish_unknown` on the
old close (now counted as "sent", -1), recomputes `now = -1 + 2 = 1`, sees `now != ours` and does `ledger[key] = now; continue`
without writing the remaining 1 down. The record stays +1 until the next look (15:40 / 23:58 true-up, next Exit). When the finished close was
larger than what is left (an `ADJUSTED` row already took part of the position down), the same code turns the record into a PHANTOM
opposite record (+3 entry, -2 adjusted, -3 finished close = -2). In the window a holder lot of the same contract (or a Buy that
nets against the phantom) is exposed: Exit's `shown >= ours` keeps the record and sells her lot.
Found by the randomized harness (seed 880261, profile hedge, `RECORDS_TOO_HIGH` at a quiet moment: -780 -> -390 -> 0 only on the
next true-up).

**Where.** `trading.py` `_shrink_to_broker`, the `with _writedown_lock` block: after the `_finish_unknown` loop,
`if now != ours or key in unfinished_keys(...): ledger[key] = now; continue`.

**Test (fails today: the record is +65 after one true-up; and the holder's lot is sold).**
```python
def test_second_look_needed_after_old_unknown_close_finished(client, owner, clock, sim, monkeypatch):
    acct, ts, lot, user = _setup_lost_close(client, owner, clock, sim, monkeypatch, went_through=False)
    _next_morning(acct, clock)
    _run(client, owner, "fno", "buy")                      # TradeLab's second lot: 2 lots in all
    assert acct.net(ts, "NRML") == 2 * lot
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(2 * lot), "DAY", ts, "S")   # all closed in the Kotak app
    _day_after(acct, clock)
    trading.true_up_all()
    assert trading.tradelab_open(user, "fno") == {}, trading.tradelab_open(user, "fno")   # +65 left

def test_holders_new_lot_is_sold_after_one_true_up_left_a_stale_record(client, owner, clock, sim, monkeypatch):
    ... same set-up, then _run(client, owner, "fno", "sell")   # one look
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "B")   # the holder's own new lot
    _, run = _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == lot          # fails: 0, "1 order placed"
```
(`_setup_lost_close`: Buy 1 lot, then a lost Exit reply that never reached Kotak, tags blanked, an untagged look-alike in the book,
`MISSING_AFTER = 0`, so the close stays `unknown`.)

**Smallest fix.** In that block, after finishing the old closes, recompute and keep going instead of `continue`: set
`ours = now` (and `side`, `gone = abs(now) - keep`, with `keep = min(abs(now), cap)`; if `now` is zero or has the opposite sign
only set `ledger[key] = 0` / write the ADJUSTED row for `abs(now)` on the right side), then write the rest down in the same pass.

**Phantom variant (also CONFIRMED, test below fails with `{... NRML: -130}`).** TradeLab bought 3 lots; Exit's close of 3 lots went through
but its reply was lost (row `unknown`); next day the holder has 1 lot of her own in that contract, so the true-up sees 1 of 3 and
writes 2 down (`ADJUSTED`, record +1: the "close went through" is not known yet). Later she closes her lot (Kotak lists nothing). The
true-up finishes the old close (counted as 3 sent): 3 - 2 - 3 = **-2 lots, a short TradeLab never opened**, and stops there; the
next look writes it back to 0. Until then a Buy / Exit sees "TradeLab is short 2" with Kotak flat; if the holder opens a short of
that contract meanwhile, Exit buys it back (shown >= ours).
```python
def test_phantom_opposite_record_after_finishing_a_close_that_went_through(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION); monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=5_000_000.0)
    client.put("/api/sections/fno", json={"max_lots": 3}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut", lots=3); ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101"); _run(client, owner, "fno", "buy")           # 3 lots
    database()["tag_proofs"].delete_many({})
    acct.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]; _blank_tags(acct)
    _run(client, owner, "fno", "sell")                 # went through, the reply was lost
    acct.faults.clear(); user = client.get("/api/auth/me", headers=owner).json()["id"]; trading.reconcile_orders(user)
    _overnight(acct, clock)                            # settle_overnight, rows aged 10.5 days, 15 Oct 09:30
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "B")   # the holder's own lot
    trading.true_up_all()                              # shown 1 lot of 3: ADJUSTED 2 -> record +1
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "S")   # she closes it
    _day_after(acct, clock)
    trading.true_up_all()
    assert trading.tradelab_open(user, "fno") == {}, trading.tradelab_open(user, "fno")   # -130 today
```
(`_overnight` = `acct.settle_overnight(); _age_rows(timedelta(days=10, hours=12)); clock.set(15 Oct 09:30)`.)

### 2. CONFIRMED (medium-low) - in custom-lots sizing the sold leg takes the money first, so its hedge is sized far below it and the Buy is "done" with the short mostly naked

**Severity: medium-low** (sold options mostly without their hedge, the exact exposure the hedge-first waves exist to prevent; the
confirm screen does show "1 lot (fixed 8) - capped by the money free" on the hedge row, but nothing says the pair is no longer a pair,
and the run ends "done"). Round 2 noted this only for equal sizing ("an expensive hedge could be smaller than the short"); it also
happens with fixed lots because the items are sized one after the other from the same money (`left` / `left_cash`), the sold leg
(listed first, margin Rs 13,325 a lot) leaving the hedge only what is left.

**Scenario.** Custom sizing, max lots 25, one account with Rs 95,000: sold NIFTY 25000 PE 8 lots, bought NIFTY 24500 PE 8 lots. The sold
leg is capped to 7 lots (Rs 93,000 of margin), the hedge gets what remains: 1 lot. `_sales_to_wait_for` only waits for the hedge to
fill in full of its (capped) quantity, so the 7 lots are sold with a 1-lot hedge: run "done 2 orders placed", net -455 / +65.
(Rs 100,000: 7 / 6; Rs 60,000: 4 / 6; Rs 80,000: the hedge gets nothing and the sold leg is correctly not sent.)

**Where.** `trading.py` `build_entry_plan`, the `custom` branch of the `elif sp.lots` block (`own = ... left ... left_cash`;
`left -= used`): sized in basket order, bought and sold legs alike.

**Test (fails: `assert 65 >= --455`).**
```python
def test_hedge_sized_below_the_sold_leg_by_the_money_left(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=95000.0)
    client.put("/api/sections/fno", json={"max_lots": 25, "sizing_mode": "custom"}, headers=owner)
    short = _add_contract(client, owner, "fno", "nifty 25000 pe", side="S", lots=8)
    hedge = _add_contract(client, owner, "fno", "nifty 24500 pe", side="B", lots=8)
    acct = sim.account("AB101")
    pv, run = _run(client, owner, "fno", "buy")          # done: sold 7 lots, hedge 1 lot
    assert acct.net(hedge["trading_symbol"], "NRML") >= -acct.net(short["trading_symbol"], "NRML")
```

**Smallest fix.** Size bought legs before sold-first legs in that loop (they are fired first anyway), then cap a sold leg's lots
at the lots of the bought legs of the same underlying in the same account when the plan has any and they were capped below their
fixed lots (note: "cut to the hedge's lots"); or at least mark the sold leg `skip`/`error` ("its hedge could only be sized to N
lots") so the run is not "done".
