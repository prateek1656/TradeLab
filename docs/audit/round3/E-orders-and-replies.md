# E — Order lifecycle and Kotak replies (round 3) — final report

## Checked so far

- Round-1/2 E files and HANDOFF read; `git diff 7632274 HEAD -- backend/app` read for trading.py, kotak.py, scheduler.py.
- `_unsettled_to_unknown` / `_abandoned_to_unknown` vs `_live`, `_settle_logs`, `_finish_run`: the update is guarded by
  `status: pending`, a live run's rows are never touched, a run only leaves `_live` after settling. No race found.
- `reconcile_orders` from several threads (`_await_final(row_ids)`, the unfiltered call in each panic thread's
  `build_sell_plan`, `reconcile_today`): `_apply_order` writes absolute values, the session saves only changed fields,
  a write conflict replays the same `$set`. Nothing is additive, so no fill is counted twice. A 6-thread x 5-pass
  stress over 3 accounts with lost replies ended with every row success/final and no exception. (Only a stale-view
  overwrite of an UNKNOWN row, open view over a cancelled one, is possible; it needs a lost reply and a state change
  within milliseconds: not reported.)
- Opening waves (`_sales_to_wait_for`, `_fire_in_waves(closing=False)`, `_short_closes(closing=False)`): refused, part
  filled, skipped, unknown, still-working hedge all hold the sale; `_stopped_at` honoured; a crash in wave 1 sends no sale.
- Square-off exception path (`answered`, `_unsettled_to_unknown`, `sent`/`when`) and `_earlier_closes` row selection
  (exit runs, MIS, success/unknown, today, `order_id or tag` matches `found`, which is keyed by both): fine. The
  reopened-position-after-restart case is area A's.
- Session re-login (`manager._ensure` generation check): concurrent panic threads log in once; only stCode 1003/403 or the
  SDK's pre-send "2fa" text resend an order.

## Findings

### 1. CONFIRMED — a close that never reached Kotak stays UNKNOWN for good unless the day's book already shows a TradeLab tag in that segment; the next Exit/Sell then says "holds nothing" (done) with the position open, and TradeLab forgets it the next day

Severity: medium-high in money terms (a TradeLab position is left open and invisible; the owner got one "partial: 1 with
no reply from Kotak - check the Kotak app" pop-up, then TradeLab reports "done"). Likelihood: low per trade (needs a lost /
timed-out reply on a close that did not reach Kotak) but the precondition is the common case for the first order of a day
in a segment: carry-forward Exit of F&O / Commodity and Holding Sell on a later day, where the account has no other
TradeLab order in that segment today.

Scenario: a position bought yesterday; today Exit's only close times out and never reaches Kotak. The row is UNKNOWN.
Since the round-2 D change, `reconcile_orders` marks it "Not placed" only if `_tags_echoed` finds a `TL`/`SQ`-tagged
order of that segment in today's book; the lost close is the only TradeLab order today, so there is none and the row
stays UNKNOWN (reconcile_today also leaves it, and after midnight nothing looks at it). `tradelab_book` counts a
closing row in status UNKNOWN as closed (to avoid closing twice), so the ledger is 0: the second Exit is "F&O holds
nothing bought by TradeLab · done", the Positions screen no longer lists it, and the same row is counted as a close on
every later day. Before 7632274 the row became "Not placed" after 60 s and the second Exit closed the position.
The same crash-before-send route exists with `_unsettled_to_unknown` (a database blip between reserve and send turns the
close rows UNKNOWN with no order at Kotak) and `recover_after_restart`.

Where: `trading.py` `reconcile_orders` (the `_tags_echoed(orders, row.exchange_segment)` gate, about line 1562),
`tradelab_book` (closing rows count in status UNKNOWN, about line 2715), `build_sell_plan` (no check for old UNKNOWN closes).

Test (fails today; scratch copy at `/tmp/claude-0/-home-user-TradeLab/e000eb7b-c0d1-569a-a23f-04bde988e670/scratchpad/E_scratch_copy.py`,
functions `test_E1_*` for F&O and `test_E1b_*` for Holding; the scratch file in backend/tests is deleted):

```python
def test_E1_close_that_never_reached_kotak_without_tag_proof(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")
    ts = item["trading_symbol"]
    _next_morning(acct, clock)
    acct.faults["place_order"] = [{"message": "Endpoint request timed out"}]   # never reached Kotak
    _, run1 = _run(client, owner, "fno", "sell")                               # partial, 1 unknown close
    acct.faults.clear()
    trading.reconcile_orders(client.get("/api/auth/me", headers=owner).json()["id"])
    _, run2 = _run(client, owner, "fno", "sell")        # 'done', "F&O holds nothing bought by TradeLab"
    assert acct.net(ts, "NRML") == 0                    # FAILS: 65 (Holding: 492 ITBEES units still in the demat)
```

Smallest fix: two parts. (a) Give `reconcile_orders` a proof of absence that does not depend on tags: a readable book that
holds no order at all with the row's trading symbol, side and product (any tag, any time today) means the order was not
placed; keep the row UNKNOWN only when such an untagged look-alike exists. (b) Make `build_sell_plan` never answer "holds
nothing / done" while an UNKNOWN closing row of the key is older than MISSING_AFTER: skip the key with `error=True` ("a close
TradeLab sent has no answer; check the Kotak Neo app") so the run is partial and the position stays listed.

## Nothing else real found in area E

The new lifecycle code (crash rows to unknown, abandoned-row sweep, opening waves, per-account panic threads, `_earlier_closes`)
holds up; see the checked list above.

## Live checks that only Kotak can settle

- That `order_report` echoes `ig` as `GuiOrdId` for NRML and MCX orders (finding 1 is the cost when it cannot be proven).
- What an order that Kotak accepted and then rejects within milliseconds looks like in the book (statuses "put order req
  received" / "open pending" are treated as open, which is fail-safe).
