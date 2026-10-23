# D — Commodity (MCX, NRML) — round 1 (in progress)

## Checked so far

- Read: HANDOFF, CLAUDE.md, `kotak.py` (place_order, positions parsing: multiplier, genNum/genDen,
  prcNum/prcDen), `contracts.py` (lLotSize, lMultiplier, dGenNum/dGenDen, expiry decoding), `sections.py`
  (commodity window 09:00–23:25, calendar "mcx" = weekdays), `scheduler.py` (15:40 close-of-day, 23:58 after-MCX
  true-up), `trading.py` end to end (entry plan / lot sizing / margins, slicing, fire_plan, reconcile, sell plan,
  `_shrink_to_broker` with `NIGHT_ENDS`, panic button, expiry notes).
- Kotak's docs (kotak-neo-python `docs/functions/portfolio/positions.md`): positions quantities for F&O are in
  units ("For FnO Scrips, divide … cfBuyQty, flBuyQty, cfSellQty, flSellQty by lotSz"); `place_order.md` says only
  "quantity of the order" and nothing about MCX lots vs units. Kotak's own API docs (Kotak-Neo/Kotak-Neo,
  `docs/market-data-apis/instruments.md`, copy saved on 9 Oct) map the scrip master's `lLotSize` to the order's
  `qt`: "Quantity sent in place order should be in multiple of the lot size" — the files listed include mcx_fo.
  The installed SDK (kotakneoapi 3.0.7, pinned) passes the quantity through unchanged and has no MCX rule.
  OpenAlgo's Kotak adapter also uses `lLotSize` as the lot and sends units. TradeLab's `order_units` (lots ×
  lLotSize) matches all of this; Kotak's lMultiplier × dGenNum/dGenDen (GOLDM 0.1, GOLD 100) only makes sense
  with quantities in those units too. Still only a live order settles it for MCX (see the end of this file).

## Findings

(none confirmed yet)
