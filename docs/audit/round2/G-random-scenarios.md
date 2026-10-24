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

## Triage of violations found while calibrating (first ~1,500 seeds, old generators)
| Seed (harness version) | Violation | Class |
| --- | --- | --- |
| 7, 21, 145 | Sell sold the holder's 3 ITBEES in the T+1 gap | Fungible units: the arriving TradeLab units replace them, final holdings identical -> oracle artefact (kept as a note) |
| 5, 214, 566 ... | OVERSOLD / TL_FLIP / DONE_BUT_OPEN | the holder traded inside the run (injected hook) or Intraday's MIS netting by design -> race / accepted |
| 63, 1580 | RECORDS_TOO_HIGH / DONE_BUT_OPEN with a part-filled order | simulator gap: a working order never ended at the exchange close; now `close_markets()` ends day orders at 15:30 / 23:30 |
| 145, 1514 | records lose ~500 delivery units after the holder sold from the demat and TradeLab bought the same day | HANDOFF "open question" (does Kotak's holdings quantity drop at once?), taken out of the judged keys |
| 1804 | records keep a contract the holder closed (no true-up ran, then the day rolled, empty positions = no evidence) | accepted (HANDOFF: true-ups that did not run); generator now always runs 15:40 / 23:58 |
| 1074, 1273 | IntegrityError on two simultaneous Let's Trade taps | harness (note above): 500 instead of 409, no trade |

## Sensitivity check (mutants) — the oracle must notice TradeLab being broken on purpose
`TL_FUZZ_MUTANT=<name>` breaks TradeLab inside the test process (monkeypatch only; no app file is touched). 40 seeds each:
- `no_writedown` (records never brought down to Kotak's figure): caught (RECORDS_TOO_HIGH), but only in 1 of 40 seeds -> write-downs are exercised rarely.
- `oversell` (every close one lot / share too big): caught in most seeds (HOLDER_UNITS, OVERSOLD, TL_FLIP, DONE_BUT_OPEN, HEDGE_SOLD_NAKED).
- `resend` (a lost order reply is sent again): caught (DUPLICATE_ORDER).
- `panic_skips_contracts` / `forget_close`: caught (DONE_BUT_OPEN).
