# G — Randomized scenario test (round 2)

Harness: `backend/tests/wip_random_scenarios.py`, copied to `backend/tests/test_random_scenarios.py` once its default
seeds passed (30 seeds, about 25 s). It was built from scratch in this round (the round-1 file was never saved) from
the round-1 design: the oracle, the invariants, the clock.

Run: `cd backend && .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_random_scenarios.py`
More seeds (run them in 3 processes at once; each pytest process has its own database):
`TL_FUZZ_SEEDS=2000 TL_FUZZ_START=0 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_random_scenarios.py -k fuzz -s`
Other switches: `TL_FUZZ_ONLY=<seed>` (one seed with its step log), `TL_FUZZ_OUT=file.jsonl` (every violating seed with
its spec and step log), `TL_FUZZ_REPLAY=file.jsonl TL_FUZZ_ONLY=<seed>` (replay a recorded spec),
`TL_FUZZ_SHRINK=<seed>:<KIND>` or `file.jsonl:<seed>:<KIND>` (minimise a failing scenario, `TL_FUZZ_SHRINK_OUT=file.json`),
`TL_FUZZ_PROFILE=hedge|holding|derivs|intraday|broad`, `TL_FUZZ_MUTANT=<name>` (see below), `TL_FUZZ_NOECHO=1`
(MCX / NSE books that do not show TradeLab's marker; finding G-2).

## Result

Tested against `5805f10` (the fixes merged up to the round-3 unconfirmed-close fixes). Final run of the finished
harness: RESULT_LINE

Real findings: **G-1** (low, no money: a 500 instead of a 409) and **G-2** (conditional on Kotak's MCX book not echoing
the marker: Sell / Exit / panic call themselves done with a lost-reply Buy's position open). **G-3** was found at
`2f84ec9` and is fixed at `5805f10`. Everything else the harness reported turned out to be a simulator gap, an oracle
rule that was too strict, a race the holder causes by trading inside the run, or one of HANDOFF's accepted cases
(table at the end).

## What the harness does

- A seed builds, as plain data (`gen_spec`), 1-3 family accounts (cash, a delivery holding of the holder's own, her own
  MIS and carry-forward positions), the four sections' lists (Intraday SBIN and sometimes INFY short; Holding ITBEES /
  GOLDBEES; F&O NIFTY / BANKNIFTY / SENSEX futures, calls and written puts, often a hedged pair; Commodity GOLDM /
  SILVERM / CRUDEOILM), one to four trading days (Wed 14 - Mon 19 Oct 2026, a clearing holiday on Thursday in 15 %, no
  clearing-holiday list in 15 %), and 10-60 steps. Five profiles: broad, holding, derivs, intraday, hedge.
- Steps go through the real API and the real Kotak adapter (`kotak_sim`, extended in the file): Let's Trade (in session and
  confirmed before the open, bought by the scheduler's `run_entry`), Buy now, Sell / Exit (with symbol and side filters),
  the Intraday square-off (button, scheduled, catch-up), the panic button, two TradeLab actions at once, a double tap of
  "Yes", the 15:40 and 23:58 true-ups, quiet moments (faults cleared, orders reconciled, true-up, records checked),
  autopilot off, an item removed, the server restarting after a run (its rows rewound to "pending") and **dying inside a
  run** (a `BaseException` raised in the simulated SDK at the n-th call: nothing after it is written or sent, then
  `recover_after_restart`).
- Holders' own trades (orders without a TradeLab tag, also in the middle of a TradeLab run), her stop-losses (MIS: the
  whole net; carry-forward: her own lots, sometimes the whole net), a holder or Kotak's risk system closing units TradeLab
  opened ("takeover"), later fills of working orders, stop-losses that trigger.
- Kotak faults and lags, each lasting for one TradeLab action: lost order replies (placed but the reply is a gateway page /
  timeout), refusals, 5xx, stCode 1003, a missing session, an unreadable order book / positions / holdings / limits / quotes /
  margin / login, positions that show a fill 0.2-1.5 s late, new orders missing from the book for 1-3 reads, a part fill
  (whole lots for contracts) with the rest working or rejected, a late rejection, slow cancels, the T+1 gap (delivery
  buys are in neither positions nor demat until the afternoon of their settlement day), day orders ending at 15:30 / 23:30,
  carry-forward positions carrying, expiry in cash overnight, freeze-quantity slicing (large `max_lots`).
- The clock: `trading.now_ist` and `datetime.now` in `app.models`, `trading`, `app.broker.manager` and `calendar` follow the
  test clock, so records, the order-book day and the previous trading day move with the scenario's days.
- Oracle: per (account, segment, symbol, product) the units of TradeLab (orders tagged `TL...`, split into entry and exit runs
  by the run that sent them) and of the holder; a holder's sale takes her units first, then TradeLab's; a TradeLab close takes
  TradeLab's first. The simulator's physical figures are checked against the oracle after every step (a harness self-check).
- Invariants after every step: (1) one Kotak order per TradeLab tag, no TradeLab-tagged order without a record, lots only;
  (2) a TradeLab order never takes the holder's own units and never opens against her opposite position in F&O / Commodity
  (the Intraday square-off and panic button close every MIS position in scope by design); no TradeLab cancel of her delivery /
  carry-forward orders; (3) no TradeLab order flips a position, no stop-loss she set flips one after TradeLab closed beside
  it; (4) a run reported done leaves no TradeLab units open in its scope and no working order of hers that would open a new
  position on the side TradeLab closed, otherwise it is partial / failed and the pop-up names the account; a Buy of F&O /
  Commodity that is done has every order filled in full; a Buy sells a contract first only after every bought contract of
  its underlying filled in full, an Exit never sells a hedge while the sold leg of the underlying stays open; (5) at quiet
  moments TradeLab's records equal the oracle's units (delivery units still inside their settlement day may be
  overstated by design).
- Exemptions that were needed (each is a documented rule, counted in the run statistics): trades she makes inside a run
  are a race (reported as a note); a key where her units and TradeLab's cancel each other in Kotak's net, or where
  HANDOFF's "holdings quantity drops when a delivery unit is sold" question applies, is not judged; records may stay above
  the oracle on a delivery key until its settlement day is over.

## Findings

### G-1 (low, no money) — two simultaneous Let's Trade taps: the second request crashes with a 500
- Seeds 1074 and 1273 (`par` of two `ib` steps); deterministic and minimal:
  `test_G1_a_second_lets_trade_tap_at_the_same_instant_is_refused_not_a_crash` (xfail strict).
- `trading.arm` (trading.py:1751, the insert at :1786): `find_one(TradeSession)` then `add(TradeSession)`. Two requests that both
  see "no session" both insert; the unique index (user, section, trade_date) rejects the second at commit and the
  `IntegrityError` is not caught by `routers/automation.py::lets_trade` (it maps only SessionError / RunInProgress to 409).
  The index prevents a second trade, so only the answer is wrong (HTTP 500 instead of 409 "already confirmed").
- Smallest fix: catch `IntegrityError` in `arm` (or the router) and raise `SessionError("... already confirmed")`.

### G-2 (conditional, medium) — an unknown Buy that can never be resolved is ignored by Sell / Exit / panic, which then say "done"
- Found with MCX orders whose book does not show TradeLab's marker (`TL_FUZZ_NOECHO=1`): seeds 500153, 500158, 500211,
  500429, 500487, 501279, 501440, 502068, 502093, 502144, 502285, 502562 (and 502584 with NSE F&O). Deterministic and
  minimal: `test_G2_an_unresolvable_unknown_buy_is_not_ignored_by_exit` (xfail strict). Overlaps the round-3 D / E reports
  ("the same entry that WAS placed, in a book that never echoes tags"); it is not on HANDOFF's accepted list.
- Scenario: a Buy whose reply is lost (timeout) or whose server dies before the reply is recorded did go through. In a
  book without the marker the row stays `unknown` for ever (`reconcile_orders` needs the marker, trading.py `_tags_echoed`),
  and `tradelab_book` counts an opening row only when it is `success` (trading.py:2776). The Buy run says "partial: 1 with
  no reply from Kotak - check the Kotak app", but the next Sell / Exit / panic builds its plan from the records, finds
  nothing and answers "Commodity holds nothing bought by TradeLab", status **done**, no pop-up, with 2 lots (200 units
  GOLDM) open and no longer in TradeLab's records. After a restart in the middle of a Buy it happens to every row of the
  run (seed 500153: 3 accounts x 10-20 lots; the panic button reported done with all of it open).
- Only matters if Kotak's MCX (or any) order book really does not echo the marker; nobody has seen it (HANDOFF lists only
  the first MCX order's quantity as a live check; the echo should be added to it). With the echo the same rows resolve by
  tag inside the run.
- Smallest fix: treat an unresolved `unknown` opening row (any day) as "unconfirmed" in `unconfirmed_orders` /
  `build_sell_plan`: Sell / Exit / panic then report "an order for this contract has no confirmed outcome, check the Kotak
  Neo app" as an error (run not done, pop-up names the account), and a new Buy beside it is refused, exactly as for a
  `success` row the book has not shown yet.

### G-3 (low-medium) — FIXED at 5805f10: an unconfirmed order of today switched off the "still settling" report
- Found at `2f84ec9` (seed 500201); `test_G3_a_sell_still_reports_units_settling_beside_an_unconfirmed_order_of_today` is now a
  plain regression test. A gateway page for a Buy that never reached Kotak left an `unknown` row (no TradeLab order of the
  day in that segment, so no echo proof); `unfinished_keys` then made `_shrink_to_broker` return before it built the
  "units still settling" row, and Sell said "Nothing left to close" (done, no pop-up) for units that were open. The durable tag
  proof of `e99649d` resolves the row, and the report is back.

## Investigated and not bugs (so nobody chases them again)

| What the oracle flagged | Why it is not a bug |
| --- | --- |
| Sell sells the holder's 3 ETF units while TradeLab's 246 are in the T+1 gap (seeds 7, 21, 145) | Delivery units are fungible; the arriving units replace them, final holdings are identical. Harness: a sale of up to TradeLab's total units is a TradeLab close |
| records above the oracle on the settlement day after her stale `holder_units` absorbed a second day's sale (seeds 120014, 120135, 202048) | TradeLab keeps units bought on a recent trading day until their settlement day is over; checked: the next trading day's true-up writes them off. Harness tolerates it |
| ~500 delivery units forgotten after she sold from the demat and TradeLab bought the same day (seeds 145, 1514) | HANDOFF's open question (does Kotak's holdings quantity drop at once?); those keys are not judged |
| a contract the holder closed stays in the records (seed 1804) | HANDOFF: the 15:40 / 23:58 true-ups did not run (the generator now always runs them; a true-up stopped by a Kotak fault is also that case) |
| OVERSOLD / TL_FLIP / DONE_BUT_OPEN with an injected holder trade (seeds 5, 214, 566, 579, 198 ...) | the holder trades inside the run: a race nobody can win; reported as notes |
| Intraday Let's Trade flips the holder's own MIS short (seed 5) | Intraday nets against MIS positions by design; the square-off closes the whole MIS net |
| part-filled order that filled whole stayed "open"; positions lag measured in real seconds across a fake day; market-order remainders never ended; parts of lots | simulator gaps, fixed in the harness (`close_markets`, whole-lot partial fills, lag in scenario time) |
| square-off scope: accounts with only skipped entry rows (seed 120938); `DONE_BESIDE_STRAY_ORDER` for a holder order on the side TradeLab did not close (seed 601017) | oracle too strict, fixed |

## Sensitivity: mutants (`TL_FUZZ_MUTANT=<name>`, monkeypatch only; no app file is touched)

| Mutant | Caught by |
| --- | --- |
| `no_writedown` (records never brought down to Kotak's figure) | RECORDS_TOO_HIGH, DONE_BUT_OPEN (about 1 seed in 8 after the holder "takeover" step was added) |
| `oversell` (every close one lot / share too big) | HOLDER_UNITS, OVERSOLD, TL_FLIP, DONE_BUT_OPEN, HEDGE_SOLD_NAKED |
| `resend` (a lost order reply is sent again) | DUPLICATE_ORDER |
| `panic_skips_contracts`, `forget_close` | DONE_BUT_OPEN |
| `hedge_not_first` (Buy sends the short with its hedge) | SHORT_BEFORE_HEDGE |
| `exit_hedge_first` (Exit sells the hedge with the buy-back) | HEDGE_SOLD_NAKED in a directed scenario; rarely in random seeds (needs a refused buy-back on a full Exit) |

## Not covered

Price moves and margin refusals (sizing is the same ₹100 world), a database blip, two users, Kotak answers nobody has
seen (beyond the marker echo switch), orders that are filled seconds later by a real exchange queue, live scheduler timers
(the harness calls the jobs), the order of events inside one second across accounts.
