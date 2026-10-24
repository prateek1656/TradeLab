# D - Commodity (MCX, NRML) - round 4 final report

Verdict: two confirmed problems, both in the new "unknown order" handling and both reachable only after a lost order
reply that stays unresolved past midnight (finding 1: MEDIUM, a forgotten short; finding 2: LOW-MEDIUM, Exit/panic
blocked, fail-safe). Everything else in the new MCX code held up.

## Checked so far (real adapter + kotak_sim; scratch test deleted)
- `git diff f360223 HEAD -- backend/app` read in full; then `reconcile_orders`, `_note_tags`, `_tags_echoed`, `_look_alike`,
  `unresolved_closes`, `unresolved_entries`, `tradelab_book` (next-day unknown rule, `closes_proved`), `_shrink_to_broker`,
  `build_entry_plan` (`unconfirmed |= unresolved_entries`), `build_sell_plan` (unresolved / unknown_buys / `gone`),
  `carry_forward` in `run_exit`, the 15:40 / 23:58 jobs and `catch_up_reports`.
- Proof of absence: a row becomes "Not placed" only if the book (readable, > MISSING_AFTER) has no order with its tag AND
  (a TL/SQ tag was ever seen for that account+segment, OR no order of the same segment / side with a symbol that is a
  prefix of ours or ours of it). The tag proof is per account+segment+durable; the look-alike branch only matters for
  the first TradeLab order in a segment. An unreadable book (None) never proves anything. Only a wrong *spelling*
  in a book that does not echo tags AND an account with no earlier proof could make the absence proof wrong (e.g. book
  `CRUDEOIL26NOV6000PE`, ours `CRUDEOIL26NOV6000.00PE`: neither is a prefix of the other); that needs three
  unlikely things at once, so only a live check (below, step 0), not a finding.
- Evening session and midnight: the 23:58 job reconciles then true-ups (grace 90 s, ends before midnight), so a close
  or Buy sent in the evening is normally settled before the "next day" rules apply. `catch_up_reports` also runs
  `reconcile_today` + `true_up_all` on a restart between 15:40 and midnight, so round 3's finding 3 is now mostly
  covered; it remains only if the server is down across the whole 23:58 to 00:00 window.
  Between 00:00 and 09:00 nothing is written down (`NIGHT_ENDS`) and MCX accepts no orders, so a close sent at
  23:50 that is still unknown at midnight is first judged at 09:00 by Kotak's figure; that part is sound
  (a close that went through leaves Kotak at 0; one that did not is closed once, capped by Kotak's figure).
- `unresolved_closes` blocks Exit / panic / Sell for today's unknown close per key and is dropped at midnight (the book is
  gone; the next-day rule takes over). Checked with the hedged flow: the unknown buy-back counts 0 under
  `closes_proved`, so the hedge stays held.
- `unresolved_entries`: today's unknown Buys also block a new Buy of that key (UNCONFIRMED); earlier days' do not (a
  new same-direction Buy is fine; an opposite-direction one is stopped by `_outside_position` because Kotak shows the
  possible position). See finding 2 for the one problem.

## Finding 1 (MEDIUM, CONFIRMED): a record that "No Data" keeps alive nets against the next opposite entry, and a real short is forgotten
`trading.py` `_shrink_to_broker` (the derivative guard `key.product != "CNC" and not (key in view.listed or view.rows > 0
or _expired_contract(key))`, ~line 3283) never writes down a contract when Kotak lists no row at all. That is the
case the D3-1 fixer raised, and it is common for MCX: a small account holds one contract, so after its close Kotak
answers "No Data" (positions empty) and the record stays at +2 lots. Exit then says "Nothing left to close" (done, no
error), which is acceptable. The harm comes next: `build_entry_plan` and `tradelab_book` treat the stale +2 as a real
TradeLab position. A new Buy on the other side (sell-first, i.e. a written option or a short future) is noted "First
closes the 2 TradeLab bought", and `tradelab_book` nets the entry against the stale record (+20 - 20 = 0). Kotak now
holds a real SHORT of 2 lots; TradeLab's ledger is empty. Exit answers "Commodity holds nothing bought by TradeLab", the
true-up has nothing to write: the short is open with no record and no warning (MCX short, unlimited loss, overnight).
How the stale record arises without a true-up that could write it down: an unknown close of an earlier day that DID go
through (reply lost, not resolved before midnight: book not echoing tags, 23:58 job missed because the server was
down across it, or the book unreadable that evening), then nothing listed at Kotak. Also any position closed in the app
on a day when no true-up ran (server down).
Test (fails on current code; `_unknown_close_yesterday` etc. are the helpers in test_kotak_replies_end_to_end.py):
```python
def test_stale_ledger_after_no_data_then_sell_first_entry(client, owner, clock, sim, monkeypatch):
    acct, ts, user = _unknown_close_yesterday(client, owner, clock, sim, monkeypatch, True)  # close went through
    assert acct.net(ts, "NRML") == 0
    _, run = _run(client, owner, "commodity", "sell")            # "done 0 orders placed": records still say +20
    for it in client.get("/api/basket", params={"section": "commodity"}, headers=owner).json():
        client.delete(f"/api/basket/{it['id']}", headers=owner)
    _add_contract(client, owner, "commodity", "crudeoilm fut", side="S", lots=1)   # sell-first (the run sold 20 units in my run: max_lots 2 sizing)
    _run(client, owner, "commodity", "buy")
    trading.true_up_all()
    _, run = _run(client, owner, "commodity", "sell")            # "holds nothing bought by TradeLab", 0 orders
    assert acct.net(ts, "NRML") == 0                              # fails: Kotak is -20 (a real short), records say 0
```
Smallest fix: never let a record Kotak does not list net against a fresh entry. In `build_entry_plan.prepare`, when the
ledger for a derivative key is non-zero and `key not in view.listed` (and the key is not unfinished/unconfirmed), either
write the stale record down first (Kotak answered; the last TradeLab order of the key is from an earlier day) or stop the
item with an error "TradeLab's records show N here but Kotak lists no position: check the Kotak Neo app". Better still:
in `_shrink_to_broker`, accept a successful positions reply with `view.rows == 0` as evidence for a key whose last
TradeLab order is older than today (a "No Data" reply is accepted as "empty" everywhere else), and keep the guard only
for today's keys.

## Finding 2 (LOW-MEDIUM, CONFIRMED): an old unknown Buy blocks Exit AND the panic button on that key for good, including for a later, fully confirmed TradeLab position
`unresolved_entries` has no day limit for NRML rows (HANDOFF: "earlier days only while Kotak lists the key"), and rows of
earlier days are never reconciled again (`reconcile_orders` reads only today's rows). So a Buy whose reply was lost,
unresolved at midnight, is permanent. As soon as Kotak lists the key again (for instance after the owner's next, normal
TradeLab Buy of that contract, tracked and confirmed) Exit and the panic button refuse the key: "An earlier buy of this
(sent ...) got no reply from Kotak ... nothing is sent", run "failed", the confirmed lots stay open until the owner
closes them in the Kotak app, every time, until the contract expires. No money is lost by TradeLab sending something
wrong (fail-safe), but the panic button then cannot close TradeLab's own confirmed contracts.
Test (fails; Exit/panic leave 20 open):
```python
def test_old_unknown_buy_blocks_exit_of_later_real_position(client, owner, clock, sim, monkeypatch):
    clock.set(EVENING); monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=50_000_000.0)
    client.put("/api/sections/commodity", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "commodity", "crudeoilm fut", lots=2)
    acct = sim.account("AB101"); ts = item["trading_symbol"]
    database()["tag_proofs"].delete_many({}); _blank_tags(acct)            # a book that does not echo tags
    acct.orders.append({"nOrdNo": "778", "ordSt": "complete", "trdSym": ts, "exSeg": "mcx_fo", "prod": "NRML",
                        "trnsTp": "B", "qty": 1, "fldQty": 1, "avgPrc": "1.00", "prcTp": "MKT", "rejRsn": "--",
                        "GuiOrdId": "", "ordDtTm": "22-Oct-2026 10:00:00"})   # a look-alike, so no "Not placed"
    acct.faults["place_order"] = [{"message": "Endpoint request timed out"}]
    _run(client, owner, "commodity", "buy"); acct.faults.clear()
    trading.reconcile_orders(user)                                          # row stays unknown
    acct.settle_overnight(); _age_rows(timedelta(days=1)); clock.set(datetime(2026, 10, 15, 10, 0, tzinfo=IST))
    _run(client, owner, "commodity", "buy")                                 # a normal Buy: 20 units, tracked
    _, run = _run(client, owner, "commodity", "sell")                       # run "failed", 0 orders
    assert acct.net(ts, "NRML") == 0                                        # fails: 20
```
(The same happens with tags echoed if the 23:58 job did not run and the book was unreadable that evening.)
Smallest fix: treat the confirmed ledger part like the unconfirmed case in `build_sell_plan`: for a key with an old
unknown Buy AND a non-zero ledger, close min(ledger, Kotak's figure) (these are TradeLab's confirmed units; the unknown order
can only add to Kotak's figure) and report the unknown part as the error row, exactly as `panic and part > 0` does for
`unconfirmed`. Also let the owner dismiss an old unknown row ("I checked the app") so it does not stay forever.

## Nothing real found in
Lots / units (guards from round 3 unchanged), MCX calendar and windows, slicing, margin sizing, expiry, hedged-pair waves
with unresolved buy-backs, the tag-proof storage (`clear_all` clears it, per account + segment), `_look_alike` side/segment
matching (it can only hold a "Not placed" back, never create one, except for the spelling case above).

## Live 1-lot checklist (only Kotak's real MCX behaviour can settle these)
One account (others switched off), a cheap near-month cash-settled contract (CRUDEOILM, lot 10; not GOLD whose lot 1
hides unit/lot mix-ups), evening session about 18:00-22:00, not within 5 trading days of expiry.

0. Read-only first. Buy 1 lot by hand in the Kotak Neo app, then print the raw rows of the adapter's `positions` and
   `order_report` and the scrip master (one-off script):
   - position: `cfBuyQty`/`flBuyQty` = 10 or 1 (units or lots?). A 1 means lots: stop. `exSeg` spelling, `trdSym` equal to the
     master's `pTrdSymbol` (letters, case, digits), `tok` equal to `pSymbol`, `prod` = NRML.
   - order row: the `trdSym` in the book equals the symbol TradeLab sends (this decides whether the absence proof's
     look-alike check can be fooled); print `GuiOrdId` of this app-placed order and of any order the Kotak system itself
     creates (auto square-off, risk close): it must NOT start with "TL" or "SQ" (the tag proof would be set by it).
   - master: `lLotSize`, `pSymbolName` of the mini/micro contracts against the keys of `MCX_FAMILIES`, `lMultiplier`.
   - Close that lot in the app.
1. Before the first TradeLab Buy: preview shows quantity 10 (1 lot) and a margin close to the Kotak app's for 1 lot.
2. Buy 1 lot with TradeLab. Raw order row: `qty`/`fldQty` = 10, `exSeg`, `trdSym`, `prod` = NRML, **`GuiOrdId` = `TL<id>`**
   (tag echo: the durable tag proof and "Not placed" depend on it; after this look at the `tag_proofs` collection for
   `<account id>:mcx_fo`), status complete.
3. Positions card: 10 units / 1 lot, average = fill price, P/L sign and size match the app (multiplier).
4. After 23:58 (log `after-mcx` executed) and at 09:05 next day: the card still shows 1 lot, no "Closed outside TradeLab",
   no "can't be a real position" / "spelled differently" message.
5. Next day, first order of the day in the segment: Exit. Exactly one sell of 10 with `GuiOrdId` `TL...`, flat in the app, run "done".
6. "No Data" check (decides finding 1): with the account flat, open the Positions call in the app/adapter and confirm what Kotak
   returns for an account with NO open contract at all (stCode 5203 "No Data" vs an empty list vs a row with 0). And
   the day after a close, whether the closed contract is still listed with net 0 the same day only.
7. Sell-first (1 lot short future, then Exit buys back): margin accepted, `flSellQty` = 10. Then, with the account flat and
   the ledger deliberately stale (close the lot by hand, wait a day so Kotak lists nothing), press a sell-first Buy: nothing
   may be netted against the old record (finding 1 fix).
8. MCX option: a far-OTM CRUDEOIL put bought with a MKT order (if refused, options can't be traded via TradeLab); then a hedged
   pair (sold put + bought lower strike) and a pair with a CRUDEOILM future against a CRUDEOIL option: Exit buys back the
   sold leg first, completely, before the hedge is sold.
9. A weekday on which the NSE is closed but MCX trades in the evening: Buy and Exit allowed after 17:00; before 17:00 refused by
   Kotak with no resting order. Full-day MCX holidays and Muhurat sessions are not in TradeLab's calendar (Kotak refuses; fail safe).
10. MCX quantity-freeze circular per underlying against `MCX_SLICE_LOTS * lLotSize` (10 lots per slice).
11. From 2 Nov 2026 (23:55 close): the 23:58 job still sees the evening's orders in the book and the contract in positions;
    `buy_until`/`sell_until` stay 23:25.
12. Order book day boundary: at 00:05 and 07:00 print the order book: does it still hold the evening's orders (the code
    treats midnight IST as the boundary and writes nothing 00:00-09:00)?
