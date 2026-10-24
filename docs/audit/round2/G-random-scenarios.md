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

## Findings (confirmed, most severe first)

### G-1 (low, no money) — two simultaneous Let's Trade taps: the second request crashes with a 500
- Seeds 1074, 1273 (harness v3, `par` of `ib` + `ib`); deterministic: `test_G1_a_second_lets_trade_tap_at_the_same_instant_is_refused_not_a_crash`
  in `backend/tests/wip_random_scenarios.py` (xfail strict).
- `trading.arm` (trading.py ~1700): `find_one(TradeSession)` then `add(TradeSession)`; two requests that both see "no
  session" both insert; the unique index (user, section, trade_date) rejects the second at commit and the
  `IntegrityError` is not caught by `routers/automation.py::lets_trade` (it only maps SessionError / RunInProgress to 409).
  The index prevents a second trade, so only the answer is wrong (500 instead of 409 "already confirmed").
- Smallest fix: catch `IntegrityError` in `arm` (or in the router) and raise `SessionError("… already confirmed")`.

### G-2 (low, rare) — the holder's own delivery units noted before a settlement gap absorb two different days' sales
- Seed 120014 (harness v5, profile "holding"); minimal deterministic reproduction `test_G2_her_own_units_are_not_used_twice_to_explain_outside_sales`
  (xfail strict). Story: she owns 3 ITBEES; Wed 13:00 TradeLab buys 246 (true-ups run 15:40 and 23:58, noting `holder_units.own = 3`);
  Thursday is an NSE clearing holiday, so the 246 reach the demat only on Friday 16:00. Thursday 10:00 she sells her own 3 (TradeLab
  correctly writes nothing off: her units absorb it). Friday 18:00, with the 246 in the demat, 3 units are sold again (hers are gone, so
  they are TradeLab's). TradeLab's Friday true-up still credits her stale `own = 3` (`_holders_own_before_today`, trading.py ~2699; the
  doc is only refreshed when Kotak shows at least TradeLab's units, i.e. never during the gap), so the second sale is "explained"
  by her again and nothing is written off: records 246, Kotak 243. Needs a clearing holiday (>=2 trading days in the gap) plus two
  outside sales, so rare; the phantom units are at most her noted holding (here 3; it could be e.g. 1,801) and only cost money if she
  later owns the same ETF again and a Sell sizes min(records, Kotak) over it.
- Smallest fix: when a write-off decision credits her own units with an outside sale, consume the credit (store `own - used`
  in `holder_units`) so the same units cannot absorb a second day's sale.
