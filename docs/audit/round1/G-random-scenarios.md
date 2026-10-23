# G — Randomized scenario test (round 1) — in progress

Harness: `backend/tests/wip_random_scenarios.py` (not collected by the normal run; run it by path). When it is
finished and its default seeds pass it is copied to `backend/tests/test_random_scenarios.py`.

Run: `cd backend && python -m pytest -q -p no:cacheprovider tests/wip_random_scenarios.py`
More seeds: `TL_FUZZ_SEEDS=2000 python -m pytest -q -p no:cacheprovider tests/wip_random_scenarios.py -k fuzz`

## Status

- 23 Oct ~10:10 UTC: resumed after the container restart. Code read end to end (trading.py, kotak.py,
  manager.py, kotak_sim.py, the end-to-end tests, scheduler, routers). Harness being written.

## Design (what the harness does)

- Clock: every TradeLab clock follows the test clock (`trading.now_ist`, and `datetime.now` in `app.models`
  (record times), `app.services.trading` (order-book day, previous trading day) and `app.broker.manager`
  (session age)), so scenarios can run over several trading days the way production does.
- Simulator: `FuzzAccount(SimAccount)` re-implements `place_order` with the same rules plus an ownership
  oracle, later fills of working orders, an overnight step with Kotak's T+1 gap, and hooks that act at a chosen
  moment inside a TradeLab run (a fill or a holder's trade while TradeLab is reading the books).
- Ownership oracle per (account, segment, symbol, product): TradeLab's units (orders tagged "TL…") and the
  holder's units. A holder's sale takes the holder's own units first, then TradeLab's (symmetric for buying back
  a short). A TradeLab opening order first nets against TradeLab's own opposite position; netting against the
  holder's is a violation. A TradeLab close beyond TradeLab's units is a violation (never the holder's units),
  except the Intraday square-off / panic button on MIS, which by design closes every MIS position in scope
  (README) — but never past flat (no flip).
- Invariants after every step: (1) one Kotak order per TradeLab tag, no Kotak order with a TL tag TradeLab's
  records don't know; (2) never the holder's units; (3) never a flip; (4) a "done" Exit / panic / square-off
  leaves no TradeLab units open in its scope, else partial/failed with an exit pop-up naming the account;
  (5) at quiet moments (faults cleared, reconcile + true-up), records == oracle TradeLab units, except the cases
  HANDOFF accepts.

## Findings

(none confirmed yet)

## Simulator gaps closed in the harness

(being written)

## Not covered

(being written)
