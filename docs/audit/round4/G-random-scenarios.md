# G — Randomized scenarios (round 4)

Tested on HEAD as it moved during the run (start `eddbe8a`+round-4 fixes; replays at the end on `fa85882`).

## Result

- 3300 seeds (starts 10000-13299, all profiles, three processes): 2 distinct violations. Plus seed 350 (task 1).
  - **R4-G1 (low, fail-safe)**: seed 350. Real, a heuristic false positive; nothing wrong is sent. xfail test added.
  - Seed 11937 HARNESS_ERROR = a real 500 in the Sell preview (`KeyError` in `build_sell_plan.prepare`: an old unknown
    Buy whose key Kotak no longer lists stayed in `keys` but not in `ledger`). **Already fixed** by `d073f58` ("old unknown
    rows no longer crash") while the run was going; seed passes on `fa85882`.
  - Seed 12774 STOPLOSS_FLIP = accepted/designed (below).
- Mutants: no_writedown, oversell, panic_skips_contracts, forget_close, hedge_not_first, no_settling_guard caught in 40
  seeds; resend, exit_hedge_first, ignore_unconfirmed were NOT caught in 40 seeds (small sample; need more seeds or a
  directed test). New: `ignore_unknown_entries` (caught: broad profile + `TL_FUZZ_NOECHO=1`, seed 500138 DONE_BUT_OPEN, and
  both G2 tests), `ignore_unknown_closes` (random runs did NOT catch it in 300 seeds; the new directed test
  `test_R4_a_close_with_a_lost_reply_is_never_sent_twice_nor_called_closed` does: with the mutant the second Sell says
  "holds nothing" instead of reporting the close with no known outcome). Both mutants are in `apply_mutant`.

## R4-G1 — Kotak shows exactly opened/lot units: `_units_doubt` takes a real partial position for a lot count (seed 350)

CONFIRMED. Minimal (shrunk from 20 steps to 3): TradeLab buys 10 lots SILVERM (50 units, 5 per lot, bought today); the holder
or Kotak's risk system closes 8 lots (sells 40). Kotak shows net 10 units = 2 lots, a perfectly real position.
`trading.py::_units_doubt` (`shown % lot or shown * lot == abs(ours)`): 10 * 5 == 50, so it reads "Kotak counts lots instead of
units", keeps the records at 50 and writes nothing.

- Effect: records stay too high (50, truth 10) for ever; Sell / Exit / panic send nothing for the key ("Kotak shows 10 here but
  TradeLab opened 50 (5 units to a lot): that can't be a real position of this contract…", run failed). TradeLab's 2 lots stay
  open until the user closes them in the Kotak app, and then the next true-up sees 0 and writes the 50 off. **The holder's own
  contracts are not closed**: Exit/panic were run at the end (step log: sell preview ok then "failed", panic "1 failed,
  1 skipped", no order sent). So no wrong order, no silent loss: a stuck, loudly reported position with a misleading message.
- Needs `remaining_units * lot == opened_units`, i.e. opened lots == remaining_lots * lot (SILVERM lot 5: 10 lots opened, 2 left;
  CRUDEOILM lot 10: 10 opened, 1 left; GOLDM / NIFTY would need 100 / 75 times more lots). Rare, MCX small contracts, after a
  partial outside close. Severity low.
- Test: `test_R4_a_partly_closed_contract_whose_units_times_lot_equal_the_opened_units_is_still_closed` (xfail strict; with
  `--runxfail` it fails with RECORDS_TOO_HIGH 50 vs 10).
- Smallest fix: the lot-count reading needs evidence. Kotak's row also carries the day / carry buy and sell quantities; if
  `view.day_bought[key]` (or day_sold) equals TradeLab's units in this very run, Kotak is counting units, so skip the
  `shown * lot == abs(ours)` test (keep the `shown % lot` one). In the seed, day buy is 50 and day sell 40 for a net of 10.

## Seed 350 in the original scenario

Same cause: after the two-day carry and the split sell, the holder sold 45 on CD202 (her own 5 first, then 40 of
TradeLab's 50), leaving 10 units (2 lots) with 10 * 5 == 50. The oracle expects a write-down to 10; TradeLab keeps 50 on purpose.

## Accepted / designed

- Seed 12774 STOPLOSS_FLIP (hedge profile): the holder's stop-loss was sized for her whole net including TradeLab's 65-unit
  short (195 = 130 + 65). Exit bought back TradeLab's 65, and TradeLab said "Closed, but another order on this contract (a
  stop-loss, for example) is still working at Kotak and would open a new position if it fills. Cancel it in the Kotak Neo app."
  (run partial, pop-up). Her own order then flipped her. This is the C3 design (report, never cancel her orders).

## Harness changes (backend/tests/test_random_scenarios.py)

- mutants `ignore_unknown_entries`, `ignore_unknown_closes`;
- tests `test_R4_a_partly_closed_…` (xfail strict, R4-G1) and `test_R4_a_close_with_a_lost_reply_is_never_sent_twice_nor_called_closed`.
- Whole file on `fa85882`: 37 passed, 2 skipped, 1 xfailed.
