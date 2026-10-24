# D - Commodity (MCX, NRML) - round 3 final report

Verdict: **one real, confirmed bug** (finding 1, high; it comes from the new `_tags_echoed` and also hits F&O and
Holding). The rest of the new MCX hardening holds up: no false positive that blocks a normal Exit or panic was found.

## Checked so far (all through the real adapter and `kotak_sim`; scratch test deleted)
- `git show 00f240b` read in full, plus `_shrink_to_broker`, `_lot_of`, `broker_view.respelled`, `build_sell_plan`,
  `_square_off_account` (odd-lot guard, `_tags_echoed`), `reconcile_orders`, `scheduler.py` job list.
- `_units_doubt`: lot of 1 is skipped (`lot > 1`); a part-filled Buy (one lot of three, `partial_next`) then true-up then
  Exit: no write-off, Exit "done". A part-filled Exit (one lot of three) then a second Exit: "done", flat. Whole-lot
  holder trades and a whole lot less at Kotak still write down (existing test). The only false positive is a
  coincidence: TradeLab opened N lots and Kotak shows exactly N units (lot 10: 10 lots opened, 1 lot left; lot 100
  can't reach it as max_lots is 25). It fails safe (records kept, Exit names the contract and sends nothing) and needs
  the holder to have closed 9 of 10 lots; not reported. A positions snapshot that lags at exactly the lot count
  would give the same transient "check the app" and then clear: not a loss.
- Respelled check: only reached when the key has no row at all (`shown == 0`) and a row with the same token or the
  same symbol in other letters exists in the same segment and product; a flat closed row is listed under the exact key,
  so a normal Exit/panic is not blocked. Tokens are never empty-matched.
- Odd-lot refusal in `build_sell_plan`: ledger and Kotak's figure are whole lots in all normal flows (tested part-fill
  both ways). In `_square_off_account` only a derivative MIS position can reach it.
- `MCX_FAMILIES`: only over-matching can hold something back (a hedge waits one wave; a long future of a family is
  held if a short of the family could not be bought back, and it is reported). Not a loss.
- 23:58 job grace 90 s: ends 23:59:30, before the book/day boundary; fine. See finding 4 for what it does not do.
- `_tags_echoed` per segment: see finding 1 (the problem) and the owner effects below.

## Finding 1 (HIGH, CONFIRMED): a close whose reply was lost and which never reached Kotak is never "Not placed" on a later day, and the next Exit says "done" with the position still open
`trading.py:1561-1562` (`reconcile_orders`) now needs `_tags_echoed(orders, segment)`: a TradeLab-tagged (`TL`/`SQ`)
order of TODAY in the same segment in that account's book. A position carried forward (Commodity, F&O: opened on an
earlier day) is closed by the first TradeLab order of its segment that day, so the book has no TradeLab tag. If that
close's reply is lost (timeout, 5xx, gateway page) and the order never reached Kotak, the row stays `unknown` for good:
`tradelab_book` counts a close in status `unknown` as done (`trading.py:2713`), so the ledger is 0. The next Exit or
panic then answers "Nothing left to close", the run is "done" ("Commodity holds nothing bought by TradeLab"), the
positions card is empty, and 2 lots of CRUDEOILM stay open at Kotak with no record and no warning (the first run was
"partial"/unknown, but the second looks clean). Before 00f240b the row became "Not placed" after 60 s and the next
Exit re-sent the close. Same flow for NSE F&O (confirmed, `reliance fut`); the Holding Sell (first nse_cm order of
the day) runs through the same two lines (not run). On the same day as the entry the entry's own tag is the proof, so
it only bites on carried positions and on the first order of a segment in an account.
Test (both fail on current code; with `_tags_echoed` forced True the MCX one passes):
```python
def test_lost_never_placed_exit_close_is_forgotten(client, owner, clock, sim, monkeypatch):
    acct, ts, lot = _mcx_two_lots(client, owner, clock, sim)      # helpers from test_kotak_replies_end_to_end.py
    _next_morning(acct, clock)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    acct.faults["place_order"] = [{"message": "Endpoint request timed out"}]   # never reached Kotak
    _, run1 = _run(client, owner, "commodity", "sell")             # partial, 1 row unknown (correct so far)
    trading.reconcile_orders(client.get("/api/auth/me", headers=owner).json()["id"])
    pv, run2 = _run(client, owner, "commodity", "sell")            # "done", 0 orders placed
    assert acct.net(ts, "NRML") == 0                               # fails: 20 units still open
```
(F&O variant: same with `clock.set(IN_SESSION)`, `fno`, `reliance fut`, `trading.true_up_all()`, `settle_overnight`,
`_age_rows(_trading_days_back(1))`, `clock.set(IN_SESSION + timedelta(days=1))`.)
Smallest fix: make the proof durable. When any book read shows a `TL`/`SQ` tag for (account, segment), remember it
(a small collection or a field on the account: `{account_id, segment, seen_at}`), and let `_tags_echoed` return True
if the current book shows a tag OR that proof exists. The echo is a property of Kotak's API for the segment, not of
the day. Second guard, which also covers the very first order ever in a segment: an `unknown` close that is still
unresolved must not read as closed in `build_sell_plan`; make Exit/panic report "an earlier close for this is
unconfirmed (check the Kotak Neo app)" as an error instead of "Nothing left to close".

## What `_tags_echoed` does to the owner when Kotak really shows no tag, or none is proved yet
- Entry (Buy) whose reply was lost and which never reached Kotak: row `unknown` until midnight ("may have gone
  through, check the app"), run "partial". The ledger does not count it (opening needs `success`), so nothing is
  forgotten; the contract's records are not written down that day (`unfinished_keys`) and a repeat Buy is allowed
  (tested: second Buy placed, Exit closed both). After midnight the row is never looked at again. Safe, a nuisance.
- The same entry that WAS placed, in a book that never echoes tags: no match, row `unknown`, position untracked
  (the existing known design "report, don't guess"); Exit later says it holds nothing while the position is open.
  Only the live check (below, 2) removes the uncertainty.
- Intraday square-off (`trading.py:2164`): the entry's tag is always in the book that day, so the proof is there.

## Finding 2 (LOW, PLAUSIBLE): the Commodity positions card hides a respelled or unit-mismatched position
`section_positions` (`trading.py` ~3390) matches rows by exact `trading_symbol`, `qty = min(|records|, Kotak's rows)`
and skips `qty <= 0`. When Kotak lists the contract under another spelling the card shows nothing, while Exit now
says "spelled differently ... close it in the Kotak Neo app". With a lots figure the card shows the small figure as
if it were units. Display only; Exit is correct. Fix: show the `respelled`/`unsure` reason on the card too.

## Finding 3 (LOW, PLAUSIBLE): nothing catches up the 23:58 job after a restart
`scheduler.py:316-324`: jobs live in memory, so a server restart or deploy between 23:58 and midnight (or while down)
skips the job entirely; the 90 s grace only covers a stalled scheduler. The evening's still-open rows are then
never reconciled (`reconcile_orders` reads only the new day), and the first true-up is the next day's 15:40
(a record, not a position, is at stake; Exit and every true-up size closes from Kotak's figure). Fix: at start-up, if
it is after 23:58 and before 00:00 on a weekday, run `_after_mcx_close` in a thread. Keep `AFTER_MCX_GRACE`.

## Finding 4 (LOW): `MCX_FAMILIES` keys must equal Kotak's `pSymbolName` exactly
Unmatched names (e.g. `NICKELM`, any different spelling of GOLDPETAL / SILVERMIC / NATGASMINI) silently give the
pre-C5 behaviour (a mini hedge is sold in the same wave as the buy-back). Settled only by printing the names
(live checklist, step 0).

## Live 1-lot checklist (only Kotak's real MCX behaviour can settle these)
One account (others switched off), a cheap near-month cash-settled contract (CRUDEOILM, lot 10; not GOLD whose lot 1
hides unit/lot mix-ups), in the evening session (about 18:00-22:00), not within 5 trading days of expiry, and not the
first evening of 23:55 closing (1 Nov change) unless step 10.

0. No TradeLab order yet, read-only. Buy 1 lot by hand in the Kotak Neo app, then print the raw rows of the adapter's
   `positions` and `order_report` and the MCX scrip-master rows (one-off script; the SDK reply is dumped before parsing):
   - position: `cfBuyQty`/`flBuyQty` = 10 or 1 (units or lots?), `exSeg` spelling (`mcx_fo` vs `MCX_FO`), `trdSym` equal to
     the master's `pTrdSymbol` (letters and case), `tok` equal to `pSymbol`, `prod` = NRML. A figure of 1 = lots: stop,
     TradeLab's new guard will refuse to close it, but fix the units first.
   - scrip master: `lLotSize` for each underlying you trade, `pSymbolName` of the mini/micro contracts against the keys
     of `MCX_FAMILIES` (CRUDEOILM, GOLDM, GOLDPETAL, GOLDGUINEA, SILVERM, SILVERMIC, NATGASMINI, COPPERM, ZINCMINI,
     LEADMINI, ALUMINI); `lMultiplier`, `dGenNum/dGenDen`, `dTickSize`.
   - Close that lot in the app.
1. Before pressing Yes on the first TradeLab Buy: preview shows quantity 10 (1 lot) and the margin; compare with the
   margin the Kotak app shows for 1 lot. A 10x gap means Kotak reads the order quantity as lots: do not send.
2. Buy 1 lot with TradeLab. Raw order row: `qty`/`fldQty` = 10 (not 1), `exSeg`, `trdSym`, `prod` = NRML,
   **`GuiOrdId` = `TL<id>`** (the tag echo that `_tags_echoed` and "Not placed" depend on), status complete (MKT order
   accepted on MCX futures).
3. Positions card: 10 units / 1 lot, average = fill price, P/L sign and size equal the Kotak app (multiplier).
4. 23:58 (log line `after-mcx` executed, between 23:58:00 and 23:59:30) and 09:05 next day (`cfBuyQty`): the card still
   shows 1 lot and Activity has no "Closed outside TradeLab" row and no "can't be a real position" / "spelled
   differently" message.
5. Next day, first order of the day in that segment: Exit. Exactly one sell of 10, `GuiOrdId` = `TL...` in the book, flat in
   the app, run "done". (This is the case finding 1 breaks if a reply is lost; the live test cannot force a lost reply,
   so read the tag proof in the code path via the book.)
6. Sell-first (1 lot short future, then Exit buys back): margin accepted, `flSellQty` = 10.
7. MCX option: a far-OTM CRUDEOIL put bought with a MKT order (if refused, options can't be traded via TradeLab and
   must be removed from the list); then the hedged pair (sell one put, buy a lower strike, 1 lot each) and a pair with a
   CRUDEOILM future against a CRUDEOIL option: Exit shows the buy-back earlier and complete before the hedge sale.
8. A weekday on which the NSE is closed but MCX trades in the evening: Buy and Exit allowed after 17:00; before 17:00
   the order must come back refused with no resting order in the book. MCX holidays that close the whole day, and
   Muhurat sessions on a weekend, are not in TradeLab's calendar (Buy/Exit are refused by Kotak, fail safe).
9. MCX quantity-freeze circular per underlying against `MCX_SLICE_LOTS * lLotSize` (10 lots per slice).
10. From 2 Nov 2026 (23:55 close): the 23:58 job still sees the evening's orders in the book and the contract in
    positions (3-minute margin); `buy_until`/`sell_until` stay 23:25.
