# D — Commodity (MCX, NRML) — round 1 (in progress)

## Checked so far

- Read: HANDOFF, CLAUDE.md, `kotak.py` (place_order, positions parsing: multiplier, genNum/genDen,
  prcNum/prcDen), `contracts.py` (lLotSize, lMultiplier, dGenNum/dGenDen, expiry decoding), `sections.py`
  (commodity window 09:00–23:25, calendar "mcx" = weekdays), `scheduler.py` (15:40 close-of-day, 23:58 after-MCX
  true-up), `trading.py` end to end (entry plan / lot sizing / margins, slicing, fire_plan, reconcile, sell plan,
  `_shrink_to_broker` with `NIGHT_ENDS`, panic button, expiry notes).
- Kotak's docs (kotak-neo-python `docs/functions/portfolio/positions.md`): positions quantities for F&O are in
  units ("For FnO Scrips, divide … cfBuyQty, flBuyQty, cfSellQty, flSellQty by lotSz"); `place_order.md` says only
  "quantity of the order" and nothing about MCX lots vs units. The installed SDK (kotakneoapi 3.0.7) passes the
  quantity through unchanged and has no MCX rule. OpenAlgo's Kotak adapter also uses `lLotSize` as the lot and
  sends units. So lots-vs-units for MCX is still only settled by a live 1-lot order (see the end of this file).

## Findings

(none confirmed yet)
