# D - Commodity (MCX, NRML) - round 2 final report

Verdict: **no unconditional bug found in the Commodity code paths** (sizing, slicing, Buy, Exit, panic, C5 two-wave close,
F-2, calendar and the evening/night windows all behave correctly through the real adapter and `kotak_sim`). Four
things can only be settled by Kotak's real MCX replies; two of them would silently wipe TradeLab's records if Kotak
behaves differently from the docs, so each has a cheap guard (findings 1-3) and a live-test line (end of file).

## Checked so far (all pass unless a finding says otherwise)

- Read end to end: `sections.py`, `contracts.py` (expiry decode, lot size, multiplier), `kotak.py` (positions,
  orders, place_order, margin_required), `trading.py` (entry plan, `_size_lots`, slicing, `fire_plan`, `_fire_in_waves`,
  `_run_sell`, `run_exit` panic NRML part, `_shrink_to_broker`, `true_up_all`, `expiry_note`), `scheduler.py` jobs,
  `routers/basket.py`, `routers/sections.py`, the installed SDK (kotakneoapi 3.0.7: quantity passes through unchanged,
  MCX allows validity DAY only, MCX expiry is plain Unix time, nothing about lots).
- Hedged MCX pair (sold CRUDEOIL put + bought CRUDEOIL put, same underlying) through the adapter: Exit sends the
  buy-back first and the hedge second; with a 25-lot short sliced 10+10+5 and the second buy-back slice refused, the
  hedge is kept (`B`,`S` order asserted). Passes.
- Evening session: Buy 23:20, 23:58 true-up, `settle_overnight`, 00:30 and 09:01 true-ups, then Exit: ledger intact,
  both legs closed, run "done". Passes. The 00:00-09:00 no-write window cannot lose an MCX record (only write-downs
  are blocked; closes are sized from `min(ours, Kotak net)`).
- NSE holiday on a weekday: `trade_window_error` is None for Commodity, Buy and Exit run to "done" (MCX calendar is
  weekdays only; no Commodity path calls `is_trading_day`). Passes.
- Known open question from the F-2 fixer (NRML Exit / panic when the order book is unreadable): see "Answer" below.

## Findings (most severe first)

### 1. PLAUSIBLE-conditional, CONFIRMED in simulation: if Kotak's MCX positions are in lots, records are wiped and Exit says "done" with 90% of the position open
`trading.py:2745` (`_shrink_to_broker`: `shown = max(side*broker, 0)`, `keep = min(abs(ours), cap)`) and `kotak.py:389`
(`buy_qty = cfBuyQty + flBuyQty`, no lot conversion). TradeLab sends and records **units** (`order_units` = lots x
`lLotSize`, e.g. CRUDEOILM 2 lots = 20). Kotak's SDK docs say F&O positions are in units ("divide by lotSz" to get
lots) and its instrument doc ties `lLotSize` to `qt`, but nothing in the docs or the SDK is MCX-specific. If an MCX
position row were in lots (2), the first morning true-up writes the records down to 2 ("Closed outside TradeLab:
Kotak shows 2 of the 20") and Exit sells 2 units; run "done", 18 units still open. Same-day it is hidden by the
`lagging` allowance, so the first Buy looks fine and the damage appears the next day. GOLD (lLotSize 1) can never
show the mismatch; GOLDM (100), CRUDEOILM (10), CRUDEOIL (100) would.
Test (fails on current code under the hypothesis; the wrapper divides Kotak's quantities by the lot size):
```python
def _positions_in_lots(acct, lot):
    real = acct.positions
    def pos(**kw):
        r = real(**kw)
        for row in (r.get("data") or []):
            for k in ("cfBuyQty", "cfSellQty", "flBuyQty", "flSellQty"):
                row[k] = str(int(row[k]) // lot)
        return r
    acct.positions = pos

def test_mcx_positions_in_lots(client, owner, clock, sim):
    clock.set(EVENING); _add(client, owner, sim, "Acc A", "AB101", cash=50_000_000.0)
    client.put("/api/sections/commodity", json={"max_lots": 2}, headers=owner)
    fut = _add_contract(client, owner, "commodity", "crudeoilm fut"); acct = sim.account("AB101")
    _run(client, owner, "commodity", "buy"); ts, lot = fut["trading_symbol"], fut["lot_size"]
    _positions_in_lots(acct, lot); acct.settle_overnight(); _yesterday()   # rows aged one day
    clock.set(datetime(2026, 10, 15, 9, 30, tzinfo=IST)); trading.true_up_all()
    _, run = _run(client, owner, "commodity", "sell")
    assert acct.net(ts, "NRML") == 0        # fails: 18 left, run.status == "done"
```
Smallest fix: in `_shrink_to_broker`, for a derivative key never write the records down to a figure that cannot be
a real position of that contract: with `lot` = the contract's / `OrderLog.lot_size`, treat `0 < shown < lot`,
`shown % lot != 0`, or `shown * lot == abs(ours)` as "unit mismatch": keep the records, add a failed/"cannot be sure"
entry (Exit then reports instead of closing), exactly like `_contract_changed`. Also have `build_sell_plan` skip with an
error a derivative close whose quantity is not a multiple of the lot size (an exchange would refuse it anyway).

### 2. PLAUSIBLE-conditional, CONFIRMED in simulation: an MCX order whose reply is lost is marked "Not placed" if the order book does not echo `GuiOrdId`
`trading.py:1458` (`reconcile_orders`: `match` by `order_id`, else `o.tag == row.tag`; an UNKNOWN row with no match
older than `MISSING_AFTER` becomes `failed`, "Not placed"). The SDK documents the tag echo, but it has never been seen
for `mcx_fo`. If MCX rows come back without `GuiOrdId`, a Buy whose reply was lost (timeout, 5xx) is recorded as
failed while the position is open at Kotak (sim: Kotak holds 10, ledger `{}`, row "Not placed"); for an Exit the
opposite: the close counts as failed, the position as still open, and the next Exit sends a second close (flip).
Smallest fix: only conclude "Not placed" when the book shows at least one TradeLab-tagged row today (proof that tags are
echoed for this account/segment), otherwise keep the row UNKNOWN and report "cannot be sure". Live test below checks the echo.

### 3. PLAUSIBLE-conditional: position/order keys are matched case-sensitively on `exSeg` and exactly on `trdSym`
`kotak.py:411,449` (`exchange_segment=str(r.get("exSeg", "nse_cm"))`, no `.lower()`), `trading.py:2525` (`broker_view`
keys `(p.exchange_segment, p.trading_symbol, prod.upper())` against the ledger's `("mcx_fo", pTrdSymbol, "NRML")`).
If Kotak's MCX positions carry another spelling of the segment or symbol, `view.held` is 0 for every Commodity key; the
first same-day Buy is protected by `lagging`, but the next day the records are written off whenever the account has any
other position row (`view.rows > 0`) and Exit sends nothing. Holdings already lower-case the segment, positions do not.
Smallest fix: `.lower()` the segment in `positions()` and `orders()`; and in `_shrink_to_broker`, do not write off a
contract that was bought today and is absent from every position row ("not listed") on the first look of the next day
without first checking the symbol appears at all. Live test below covers it.

### 4. PLAUSIBLE: the 10-lot MCX slice may exceed MCX's quantity freeze for some contracts
`trading.py:1088,1100` (`MCX_SLICE_LOTS = 10`, `lots * lot`) with `max_lots` up to 25 (`schemas.py:121`). MCX freeze
limits are per commodity and not in the scrip master; for a contract whose freeze is below 10 lots (large-lot metals,
or 1-kg GOLD lots) each full slice is refused whole. This is fail-safe (the run is partial and says why) but an Exit
slice refused means the position stays open and is reported. Fix: a per-underlying `FREEZE_UNITS` entry for each MCX
underlying the owner trades (read from MCX's current freeze circular), like the NSE ones. Cannot be settled offline.

### 5. PLAUSIBLE, low: C5 treats only the same `pSymbolName` as one underlying; MCX mini/micro families are not
`trading.py:1187` (`_same_underlying`: `ca.symbol == cb.symbol`). CRUDEOIL vs CRUDEOILM, GOLD / GOLDM / GOLDPETAL /
GOLDGUINEA, SILVER / SILVERM / SILVERMIC, NATURALGAS / NATGASMINI are different symbols, so a bought CRUDEOILM future
hedging a sold CRUDEOIL option is sold in the same wave as the buy-back (the pre-C5 behaviour, margin may spike and
Kotak may refuse one of them). It is unknown whether Kotak/MCX give a margin offset across such families. Smallest fix:
a small family map (strip a trailing M / MIC / MINI / PETAL / GUINEA for MCX) inside `_same_underlying`; an
over-match only delays a hedge by one wave.

### 6. PLAUSIBLE, low: message gap when a sold MCX option expires in the money
`trading.py:2764` writes an expired option off as "Closed outside TradeLab: Kotak shows 0 of the N TradeLab opened".
The in-the-money case turns into a futures position TradeLab will never track (documented in `MCX_OPTION_WARNING`
at Buy time and in the positions card, but not at the moment of the write-off). Fix: for an expired `mcx_fo` option
append "if it ended in the money it became a futures position - check the Kotak Neo app".

### 7. PLAUSIBLE, low: small gaps in the evening/night handling
- `scheduler.py:306`: the 23:58 job has `misfire_grace_time=1800`; run after midnight, `reconcile_today` and
  `_order_book_day_start()` look only at the new calendar day (`trading.py:1379`), so the evening's unfinished orders are
  not settled (Kotak's book still lists them until its start of day). Only matters if the server was down at 23:58 with an
  evening order still unsettled. Fix: before `NIGHT_ENDS`, settle rows from the previous day too.
- Buy preview gives no warning for a *bought* MCX delivery future inside its tender period (`_sell_first_warnings` only
  covers sold-first); Kotak refuses or closes it at 5 PM. The positions card does warn once held.
- Dashboard banner "<holiday> - no trading today" is NSE-only (`Dashboard.tsx:311`) while MCX's evening session is usually
  open on NSE holidays; the Commodity page itself is not blocked (correct), the banner can mislead.

## Answer to the F-2 fixer's open question (NRML Exit / panic with an unreadable order book)
It cannot make TradeLab flip a position by itself. A NRML close is sized `min(TradeLab's units, Kotak's net)`, and
closes already sent count as done in the ledger even while working, so no second close is possible. With the book
unreadable: (a) all of TradeLab's orders final: `_cancel_own_open_orders` has nothing to look up, the close goes out, and
`_stray_orders` then reports "Closed, but Kotak's order book couldn't be read ..." as a failed entry (run not "done");
(b) a partly filled entry still working: the close covers what has filled, the cancel failure is reported, and the rest
filling later is picked up by the next `reconcile` (test `test_exit_with_unreadable_book_and_a_working_buy`: run
"partial", 10 closed, 20-lot order still working and reported). A flip needs the holder's own working order (a stop-loss
set in the Kotak app), and that is reported, never sent beside silently. No change needed.

## What a live 1-lot test must check (only Kotak's real MCX behaviour settles these)
Use ONE account (others off), CRUDEOILM (lot 10, cash-settled) near-month future, in the evening session (about 18:00-22:00),
not GOLD (lot 1 hides unit/lot mix-ups), and not within 5 trading days of expiry.
1. Before any order: Buy preview shows quantity 10, 1 lot, and "per lot" margin; compare with the margin the Kotak
   app shows for 1 lot (finding 1: `margin_required` takes units; a 10x gap means quantity is read in lots).
2. Buy 1 lot. Raw rows (adapter log or a one-off script on `order_report` / `positions`) must show: order `qty` and
   `fldQty` = 10 (not 1); `exSeg` exactly `mcx_fo`; `trdSym` exactly equals the contract's `pTrdSymbol`; `prod` = `NRML`;
   **`GuiOrdId` = `TL<id>`** (finding 2); position `flBuyQty` = 10 (finding 1); the order is "complete" (a MKT order on
   MCX futures is accepted and fills).
3. Commodity "Open positions" card: 10 units / 1 lot, average = fill price, P/L sign and size match the Kotak app
   (checks `multiplier`, `genNum/genDen`, `prcNum/prcDen`: CRUDEOILM's value per price point; an app/TradeLab gap means the
   combined multiplier is wrong, display only).
4. At 23:58 (or call `true_up_all`) and again at 09:05 next day (position now `cfBuyQty` = 10): the card still shows 1 lot
   and Activity has no "Closed outside TradeLab" row.
5. Exit: exactly one sell of 10; position flat in the app; run "done".
6. Repeat with a sell-first (1 lot short CRUDEOILM future, then Exit buys back): margin accepted, `flSellQty` = 10.
7. MCX option: 1 lot of a cheap far-OTM CRUDEOIL put bought with a MKT order (some segments refuse market orders on
   options; if refused, options cannot be traded via TradeLab and must be removed from the list). Then the hedged pair
   (sell one CRUDEOIL put, buy a lower-strike put, 1 lot each): Exit must show the buy-back with an earlier time stamp
   and "complete" before the hedge sell is sent (Kotak order book times), and neither refused for margin.
8. A weekday on which the NSE is closed but MCX trades in the evening (check MCX's circular): Commodity must allow Buy
   and Exit after 17:00; before 17:00 the order must come back refused, with no resting order left in Kotak's book.
9. MCX's quantity-freeze circular for each underlying the owner uses against `MCX_SLICE_LOTS * lLotSize` (finding 4).
10. From 1 Nov 2026 the session ends 23:55: confirm the 23:58 job still sees the evening's orders in the book and the
    contract in the positions (3-minute margin).
