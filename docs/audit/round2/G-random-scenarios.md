# G — Randomized scenario test (round 2) — in progress

Harness: `backend/tests/wip_random_scenarios.py` (built from scratch in round 2; the round-1 file was never saved).
Run: `cd backend && .venv/bin/python -m pytest -q -p no:cacheprovider tests/wip_random_scenarios.py`
More seeds: `TL_FUZZ_SEEDS=2000 .venv/bin/python -m pytest -q -p no:cacheprovider tests/wip_random_scenarios.py -k fuzz`

## Status log
- Started: read audit_common/audit_G, round-1 design, HANDOFF, trading.py (entry, exit, sell plan, shrink), kotak_sim,
  end-to-end tests. Writing the harness.

## Findings
(none yet)

## Notes (working)
- Harness runs (~0.8 s/seed). Oracle decisions made while calibrating:
  * Intraday Let's Trade nets against the holder's own MIS short (by design; no check) -> not a violation.
  * Intraday square-off / panic: done must leave the account's MIS net at zero (holder's MIS included, by design).
  * Delivery units are fungible in the T+1 gap: a Sell of TradeLab's yesterday's units (still not in the demat) that
    sells min(records, demat) sells demat units the arriving ones replace (e.g. seed 7: records 243 vs 246 afterwards,
    the 3 units that stay at the end are the holder's own again) -> simulator/oracle artefact, not a loss.
  * Simulator gap fixed in harness: an order part-filled for its whole size showed "open" forever.
- Harness additions: positions lag measured in scenario time (`pos_lag`), order-book lag, holder stop-losses (MIS and
  NRML), injected holder trades inside a TradeLab run (a "race": reported as a note, not a violation), two TradeLab
  actions at once (`par`), a server restart after the next run (`restart`: the run is rewound to "running", its rows to
  "pending", then `recover_after_restart()`), autopilot off / item removed, large max_lots (order slicing), a clearing
  holiday on day 2 and sometimes no clearing-holiday list, true-ups at 15:40 and 23:58 before every overnight.
- Oracle calibrations that were simulator/oracle artefacts, not bugs: a part-filled order that filled whole stayed
  "open"; positions lag was measured in real seconds across a fake day; market-order remainders now end at the quiet
  moment; the HANDOFF open question (holdings quantity dropping when units are sold from the demat, then bought the
  same day) is taken out of invariant 5 ("tainted" keys).
- Note (low, no money): two "Let's Trade" taps at the same instant: the second request raises IntegrityError
  (unique trade_sessions index) and answers 500 instead of 409 (`arm`, trading.py ~1700: find_one then add, no
  duplicate-key handling). The unique index prevents a second trade, so only the message is wrong.
