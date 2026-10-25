# B — Holding (CNC) — round 5

Auditor B. Scratch test `backend/tests/review_B_scratch_test.py` (deleted; key tests copied below).

## Checked so far
- Read `_note_holders_own / _trades / _bound`, `_holders_noted`, `_delivery_sizes`, `_shrink_to_broker`, `build_sell_plan`.
- Extended a copy of the randomized harness with: holder pledges and releases, holder delivery buys (with their own T+1 gap),
  an optional "late" arrival mode (her and TradeLab's units show in the holdings at 06:00 the morning after the
  settlement day, i.e. after the 23:58 true-up), missed true-ups (15% each), clearing holidays, part fills, 3-6 day
  sequences, a final strand check. About 550 seeds in both modes.
  - Arrival at 16:00 (before the 23:58 true-up): no violation from the model itself (one seed, 2185, did not replay after
    I changed the generator; not analysed).
  - "Late" arrival: about 3% of seeds sell her units or forget TradeLab's units; all traced to B5-2 below.
- Pledge/release after the note, the three-way bound with pledges, partial fills, her buys and sales on TradeLab's days,
  clearing holidays: bounds hold except B5-3 (strand, safe side).
- Everything else of the area (empty-demat replies, DDPI/TPIN, funds) unchanged since round 4; nothing new found.
- Note on severity: the account's units are one pool, so a Sell that takes her units while TradeLab's arrive later leaves the
  account whole after settlement. It still breaks the owner's rule ("her shares are never touched"); B5-2c is a real loss of
  TradeLab's own records.

## B5-1 — CONFIRMED — MEDIUM (rare trigger): an old unknown CNC sale that DID go through is repeated on her units two days later
Scenario: a Sell's reply is lost and the order book is unreadable until midnight (outage or server down), so the sale stays
`unknown`. Next days the records still count it as not closed. `_shrink_to_broker` (trading.py ~3392, branch
`not earlier.get(symbol)`) rewrites the holder note as `shown - ours`; `ours` still includes the sold units, so her note drops
by exactly that amount (100 -> 81). Day 1 uses the frozen `own_before` (nothing sent); day 2 uses the lowered note, and
`_delivery_sizes` (doubtful key) sells 19 of her 100 units.
Test: `test_B5_1_unknown_old_cnc_sale_that_went_through_is_not_repeated_on_her_units`
(patch_clock; Mummy 100 ITBEES; TradeLab buys 19; true-ups; units arrive; Friday Sell with `faults["place_order"]=[(PLACED_BUT, GATEWAY_PAGE)]`
and `faults["order_report"]=GATEWAY_PAGE`; true-ups; Mon/Tue/Wed Sell + true-ups; assert demat stays 100 -> fails `81 == 100` on day 20).
Smallest fix: for a key with an old unknown close, do not re-derive the note from `shown - ours`; apply only her own
trades (`_note_holders_trades`). Plain "freeze" (verified: test passes) is enough for this case but stops noting her later buys.

## B5-2 — CONFIRMED — MEDIUM (depends on when Kotak lists T+1 units; needs a live check): her units in the T+1 gap are missing from the holder note
Scenario: she buys N units on day X-1; TradeLab's first Buy of that ETF is on day X. At the day-X true-ups her N units are in
neither positions (cleared) nor the demat, so the note says `own = D`. They reach the holdings after the last true-up
(Kotak lists them next morning, or the 23:58 true-up is missed/failed). Day X+1, TradeLab's units still settle:
a) B5-2b: bound `total - own` counts her N as TradeLab's; Sell sells her 33 units (test fails `[33] == []`).
b) B5-2c: she sells all her units (incl. the N); `_note_holders_trades` subtracts a sale from a note that never held them, so
   `outside` is too big and `_shrink_to_broker` writes off N of TradeLab's settling units ("Closed outside TradeLab ... and N more may
   not show"); after they arrive TradeLab's records are N short and those units are forgotten (test fails `76 == 82`).
Passes when the units show before the 23:58 true-up runs (`pm` mode). Harness "late" seeds 13, 76, 103, 108, 1012, 1080,
1115, 1134, 1189, 1192, 1193 are this.
Tests: `test_B5_2b_...`, `test_B5_2c_...` (patch_clock; Mummy 100; she buys 33 (or 6) Wednesday; Thursday `clear_day_positions()`,
TradeLab Buy, true-ups at 15:40 and 23:58, Friday 06:00 `_arrive` her units, Friday Sell / her sale of everything).
Smallest fix: cap a Sell while units settle at the units certainly in the demat,
`older = ours - earlier[symbol] - bought_today`: `sizes = min(sizes, older)` (verified: B5-2b passes with this, also
when no note exists). For B5-2c do not write off units bought in the settling window (they cannot have been sold
before they arrived), or refresh the note from a morning (09:05) true-up.

## B5-3 — CONFIRMED — LOW/MEDIUM (safe side): a pledge made after the note strands TradeLab's arrived units for as long as it keeps buying daily
The note's `free` is never refreshed while an earlier buy is in the window. Wed buy, Thu she pledges 40, daily Buys
Thu-Tue: every Sell leaves 40 of TradeLab's (sellable) units with "can't be sold yet (not settled, or pledged)" for 5
trading days (Fri 16 to Thu 22 Oct), sold only on the 23rd. Reported honestly (partial), nothing wrong sold; but the panic
button cannot fully close during the streak. Test: `test_B5_3_daily_buyer_and_a_later_pledge_strand_arrived_units` (prints per-day sold/left).
Fix: since pledged units are hers, her free now = `own - (D - F)`; the `held - free` bound then equals `total - own`, so use
`min(held, total - own - bought)` and drop the stale-`free` term.

## Not reported
- Same pool effect when Kotak's holdings drop on the same day as her sale (HANDOFF open question) is exempt in the harness ("tainted").
- Old unknown BUY of a symbol she holds is never finished (key stays listed): permanent "partial" Sell reports; E5-1/F5-1 is being fixed.
- Unknown sale of today and the true-up: key is unfinished, skipped, fine.
