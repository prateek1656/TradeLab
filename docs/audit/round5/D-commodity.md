# D - Commodity (MCX, NRML) - round 5 final report

Verdict: nothing real in normal use. One LOW confirmed weakness that exists only if Kotak counts MCX positions in
lots (the very doubt the guard is for), and two things only a live 1-lot test can settle. No change needed for the
zero-row write-down, `_traded_today`, `_flat_at_kotak`, or the old-unknown-row handling as the code stands.

## Checked so far (real adapter + kotak_sim; scratch test deleted)
- `git diff eddbe8a HEAD -- backend/app frontend/src` read in full (trading.py, scheduler.py, models.py), then
  `_shrink_to_broker`, `_traded_today`, `_units_doubt`, `broker_view`, `build_sell_plan`, `_flat_at_kotak`,
  `unresolved_old_closes`, `_finish_unknown`, `build_entry_plan` (STALE_RECORD), `true_up_all`, the 15:40 / 23:58 jobs.
- Zero-row write-down across the evening and midnight: `_traded_today` takes created_at >= IST midnight, the same
  boundary as the order book. A Buy/Exit of the evening (say 20:00 on D) is "today" on D (so the 23:58 true-up keeps a
  lagging fill, tested with `hide_positions` at 23:58, then settle_overnight, 09:30 true-up: no write-down, Exit
  "done"). From 00:00 it is "yesterday", but nothing is written 00:00-09:00 (`NIGHT_ENDS`), and from 09:00 a position
  carried from the evening is expected to be listed. So the right day is used; the only dependency is Kotak's
  start of day (see live check A).
- The 23:58 and 15:40 jobs are Mon-Fri; a Friday evening trade is covered by Friday 23:58. MCX is not in SCHEDULED,
  so the new `catch_up_square_off` / `_record_missed_exit` branch never touches Commodity.
- Old unknown closes: finished only when Kotak lists nothing for the key AND keep == 0 (no fill of today that could
  lag), under the write-down lock, one at a time; the ledger is recomputed afterwards, so the records reach zero by
  themselves. While Kotak lists the key, Exit/panic report and send nothing (by design, fail safe). Old unknown Buys:
  confirmed units still sell, sized by min(records, Kotak), the unknown order is reported beside them.
- `_flat_at_kotak`: skips unfinished keys, keys with a TradeLab order today that Kotak does not list, respelled keys,
  changed contracts and any non-zero figure. A sold leg netted to zero by the holder's own opposite lot reads flat; the
  account really is flat in that contract, selling the hedge alone is then correct.
- STALE_RECORD in `build_entry_plan`: only fires for an opposite-side entry against a record Kotak does not list
  after the shrink (so only a lagging fill of today, or a night-time/unreadable case); same-side Buys are not touched.
- Fuzz: 1,800 seeds of the `derivs` profile (one batch with TL_FUZZ_NOECHO=1) found no violation. A `hedge` batch showed
  four RECORDS_TOO_HIGH on NSE F&O keys; none reproduces on replay (4 recorded specs, and by seed number, pass), the batch ran
  while other auditors loaded the machine (the harness's real-time lags), and they are F&O, not MCX.
- Unchanged areas re-read for regressions: MCX windows (09:00-23:25, `is_section_day` with the MCX calendar), slicing
  (`MCX_SLICE_LOTS` 10), odd-lot refusal, `_lot_of`, margin sizing. Nothing new.

## Finding 1 (LOW, CONFIRMED but only in a lots-counting world): the gross "units evidence" is satisfied by a holder's churn in lots mode
`trading.py` `_units_doubt` (~line 3020): `in_units = gross > shown and gross % lot == 0`. If Kotak counts lots, a row
with TradeLab's N lots plus a holder who bought and sold N lots of the same contract has gross 2N lots, net N lots,
so `gross > shown`, `gross % lot == 0`, and the coincidence test (`shown * lot == ours`) is waved through. The records
are then written down from N lots (N*lot units) to N units. Needs: lots mode (unverified, exactly the doubt), the
position at least `lot` lots (SILVERM lot 5 -> 10 lots, CRUDEOILM lot 10 -> 10 lots), and the holder trading the same
contract in the same account. Without churn (gross == net) the doubt still holds.
Test (fails on current code):
```python
def test_lots_mode_with_holder_churn_misreads_as_units(client, owner, clock, sim):
    acct, ts, lot = _mcx_two_lots(client, owner, clock, sim, query="silverm", lots=10)   # 50 units
    acct.place_order("mcx_fo", "NRML", "0", "MKT", "50", "DAY", ts, "B")
    acct.place_order("mcx_fo", "NRML", "0", "MKT", "50", "DAY", ts, "S")
    _positions_in_lots(acct, lot)
    trading.true_up_all()
    assert _logs(status="adjusted") == []        # fails: records 50 -> 10
```
Smallest fix: make the evidence something lots mode cannot fake: compare Kotak's average price for TradeLab's side
(`buy_avg` / `sell_avg` in `Position`) with TradeLab's recorded fill price of that contract; amount per unit-counted
quantity is `lot` times the price in lots mode. Or drop the gross shortcut and keep the doubt (fail safe, owner closes
in the app). Moot once live check 0 shows units.

## Finding-free notes (for the owner)
- An old unknown close on a key that Kotak still lists stays reported until Kotak stops listing it; there is no
  "I checked the app" dismissal. Fail safe, but a later confirmed TradeLab Buy of the same contract then cannot be
  closed by Exit or panic either (an error row names it). Accepted design; revisit only if it shows up live.
- The `gone`/`_finish_unknown` step in `build_sell_plan` and `_flat_at_kotak` have no 00:00-09:00 guard (the shrink has).
  MCX windows (09:00-23:25) stop any run at night, so not reachable by Commodity Exit; the panic button's night use
  would only drop a hold, not send a wrong order.

## Live 1-lot checklist (only Kotak's real MCX behaviour can settle these)
One account (others off), a cheap near-month cash-settled contract (CRUDEOILM lot 10; not GOLD whose lot 1 hides
unit/lot mix-ups), evening session about 18:00-22:00, not within 5 trading days of expiry.

0. Read-only first. Buy 1 lot by hand in the Kotak Neo app, then print the raw rows of the adapter's `positions` and
   `order_report` and the scrip master:
   - position: `cfBuyQty`/`flBuyQty` = 10 or 1 (units or lots?). A 1 means lots: stop (finding 1 and the whole
     lots-vs-units doubt become real). Also `buyAmt / flBuyQty` against the fill price (per unit or per lot?),
     `exSeg` spelling, `trdSym` equal to the master's `pTrdSymbol`, `tok` equal to `pSymbol`, `prod` = NRML.
   - order row: `trdSym` equals what TradeLab sends; `GuiOrdId` of an app-placed order and of any Kotak-created order
     (auto square-off, risk close) must NOT start with "TL" or "SQ".
   - master: `lLotSize`, `pSymbolName` against `MCX_FAMILIES`, `lMultiplier`. Close that lot in the app.
1. Before the first TradeLab Buy: preview shows quantity 10 (1 lot) and a margin close to the Kotak app's for 1 lot.
2. Buy 1 lot with TradeLab. Raw order row: `qty`/`fldQty` = 10, `exSeg`, `trdSym`, `prod` = NRML, **`GuiOrdId` = `TL<id>`**
   (look at the `tag_proofs` collection for `<account id>:mcx_fo`), status complete.
3. Positions card: 10 units / 1 lot, average = fill price, P/L sign and size match the app.
4. After 23:58 (log `after-mcx`) and at 09:05, 09:30 next day: the card still shows 1 lot, no "Closed outside
   TradeLab", no "can't be a real position" / "spelled differently" message.
5. NEW (zero-row write-down): at 00:05, 07:00, 08:55, 09:00, 09:02 and 09:10 print the raw positions reply of the account
   holding yesterday evening's lot. Is the carried NRML row there from the start (cfBuyQty = 10), or does the reply say
   "No Data" / list nothing until some hour? TradeLab treats "No Data" as "flat" for any contract not traded today
   from 09:00 on; if Kotak shows nothing until later, the evening's lot would be forgotten by a Buy, Exit or true-up
   run in that gap (raise `NIGHT_ENDS` accordingly).
6. NEW (what "No Data" means): with the account flat, confirm positions returns stCode 5203 "No Data" (not an empty
   list, not a row with 0) and that a contract closed today is listed with net 0 until midnight only.
7. Next day, first order of the day in the segment: Exit. Exactly one sell of 10 with `GuiOrdId` `TL...`, flat in the
   app, run "done".
8. NEW (gross evidence): after a normal Buy, positions row `cfBuyQty + flBuyQty` equals `flBuyQty` on the day of the
   buy, and after the carry the next day equals the net (cf is a net figure, not gross history). Expect no gross
   evidence on carried rows: a partly holder-closed position then stays "doubted" (fail safe) rather than written down.
9. Sell-first (1 lot short future, then Exit buys back): margin accepted, `flSellQty` = 10. Then with the account flat
   and the ledger deliberately stale (close the lot by hand, wait a day so Kotak lists nothing): a sell-first Buy
   must not net against the old record, and Exit must say nothing is left (not hold a hedge).
10. Old unknown row: cannot be provoked safely live. Read-only check instead: confirm a lost-reply order of yesterday is
    absent from the order book at 09:05 (the book is per day), which is what makes it "old".
11. MCX option: a far-OTM CRUDEOIL put bought with a MKT order (if refused, options can't be traded via TradeLab); then
    a hedged pair (sold put + bought lower strike): Exit buys back the sold leg first, then sells the hedge; and a
    filtered Sell of only the hedge after closing the short by hand in the app sells it (the flat-short rule).
12. A weekday on which the NSE is closed but MCX trades in the evening: Buy and Exit allowed after 17:00; before 17:00
    refused by Kotak with no resting order. Full-day MCX holidays and Muhurat sessions are not in TradeLab's calendar
    (Kotak refuses; fail safe).
13. MCX quantity-freeze circular per underlying against `MCX_SLICE_LOTS * lLotSize` (10 lots per slice).
14. From 2 Nov 2026 (23:55 close): the 23:58 job still sees the evening's orders and the contract; `buy_until` /
    `sell_until` stay 23:25.
15. Order book day boundary: at 00:05 and 07:00 print the order book: does it still hold the evening's orders?
