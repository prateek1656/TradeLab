# Audit prompts (round 1 of the broad audit)

Each audit runs as one independent agent. To (re)start one in any session, give an agent the COMMON part below
with its AREA paragraph and its letter filled in. Findings land in `docs/audit/round1/<letter>-<name>.md`; a
round is done when every area has a final report there. The randomized scenario test has its own prompt at the
end.

## COMMON (all six areas)

You are an independent, adversarial auditor of trading code that moves real money: TradeLab trades many Kotak
Neo family accounts at once (FastAPI + MongoDB backend in /home/user/TradeLab/backend). The owner's instruction:
there must be NO bug on the trading side; effort is no object. Read docs/HANDOFF.md (what has been fixed, what
is known and accepted) and CLAUDE.md first. Core rules: a Kotak reply TradeLab can't be sure of stops the trade
and says why, never read as empty, refused or filled; a family member's own shares or contracts are never
touched; a TradeLab position is never forgotten or left open silently; no order is ever sent twice; a run never
reports "done" while something is left open.

Method:
- Read the code paths of your area end to end (backend/app/services/trading.py, sections.py, contracts.py,
  scheduler.py, backend/app/broker/kotak.py, manager.py, base.py, routers as needed). Think about real Kotak
  behaviour: reply shapes (see the adapter and backend/tests/kotak_sim.py), the order book and positions lagging
  each other, overnight clearing, T+1 settlement, partial fills, rejections, session expiry, many accounts,
  holders with their own positions, server restarts mid-run.
- Confirm every finding with a failing test through the real adapter using backend/tests/kotak_sim.py (helpers
  and style: backend/tests/test_kotak_replies_end_to_end.py). If the simulator lacks a behaviour you need, extend
  it inside your scratch test file (subclass or monkeypatch), not in kotak_sim.py.
- Scratch tests ONLY in backend/tests/review_<LETTER>_scratch_test.py; delete it before you finish. Write each
  finding to docs/audit/round1/<LETTER>-<name>.md as soon as it is confirmed (with a short "checked so far" list
  at the top), so nothing is lost if the session ends. Edit no other file; do not commit or push. Other auditors
  work at the same time; each pytest process uses its own database.
- Run: cd /home/user/TradeLab/backend && python -m pytest -q -p no:cacheprovider tests/review_<LETTER>_scratch_test.py
  (MongoDB replica set on 127.0.0.1:27017: start it with backend/scripts/dev_mongo.sh).
- Don't report what HANDOFF lists as known and accepted, style, naming, or paths that need several independent
  failures at once. Do report wrong money behaviour that can happen in normal use, even if rare.

Final report (also the final state of your docs/audit/round1 file; most severe first, up to ~10 findings): for
each, CONFIRMED (failing test) or PLAUSIBLE (precise code trace), severity in money terms, the scenario in two or
three sentences, file:line, the test code for confirmed ones, and the smallest fix. If you find nothing real in
your area, say so plainly.

## AREAS

- **A — Intraday (MIS)** (`A-intraday.md`). Let's Trade: confirming a day (arm), the scheduled entry at the chosen
  time, catch-up after a restart or a late wake-up, holidays, cancelling; entry sizing per account (money split,
  keep-aside %, funds, prices); order placement (tags, slices, lost or unclear replies) and following orders to
  their final state; the automatic 14:30 square-off (`run_exit`, `square_off_account` and its attempts, positions
  held back beside the holder's own working orders, retries every minute until 15:00, `accounts_traded_today`),
  "Square off now" by hand, behaviour near Kotak's own auto square-off. Can a square-off close more than TradeLab
  bought, close the holder's own MIS position when it shouldn't (check what the manual and HANDOFF promise), or
  skip a TradeLab position silently? Can an entry fire twice, late, on a holiday, after the panic button, or for
  an account switched off?
- **B — Holding (CNC)** (`B-holding.md`). Buy now (stocks and ETFs, sizing, prices, funds); Sell (only what
  TradeLab bought; DDPI/TPIN refusals; `build_sell_plan` for CNC incl. error rows for units Kotak doesn't show or
  won't let be sold); `_shrink_to_broker` (today's fills vs Kotak's day quantities, the previous-day settling rule
  with `_written_off_today`, the 00:00–09:00 no-write window); `broker_view` (held vs total); the positions view;
  the 15:40 / 23:58 true-ups; partial fills; pledged units; empty-demat "no holdings found" replies; several days
  of buys of the same ETF; holders owning the same ETF; selling the next day (T1).
- **C — F&O (NRML on nse_fo / bse_fo)** (`C-fno.md`). Contract search and lot sizes, margin sizing (max lots,
  custom lots), freeze-quantity slicing (`FREEZE_UNITS`, `_slices`, `_max_order_units`, `_interleave`), buying and
  selling first, Exit (`_run_sell`), the panic button's NRML part, the Accounts screen's per-account square-off,
  carried positions (cfBuyQty / cfSellQty), expiry (weekly / monthly, expiry day, physically settled stock F&O),
  several contracts and accounts, holders with their own positions in the same contract, a lot size that changes.
- **D — Commodity (MCX NRML)** (`D-commodity.md`). MCX quantity units (lots vs units sent in orders and read from
  positions; multiplier, genNum/genDen, prcNum/prcDen, lotSz; the SDK source in the venv), the evening session to
  23:30 / 23:55 vs the 23:58 true-up and the 00:00–09:00 no-write window, MCX holidays and half sessions vs the NSE
  calendar (`is_trading_day`, `is_section_day`, trading windows), slicing (`MCX_SLICE_LOTS`), margin sizing, Exit
  and the panic button, expiry of commodity futures and options. Say exactly what a live 1-lot test must check
  where only Kotak's real behaviour can settle it.
- **E — Order lifecycle and Kotak replies** (`E-orders-and-replies.md`). The adapter (every SDK call and reply
  shape; read the installed SDK source, neo_api_client), the broker manager (sessions, re-login, retries — can any
  path resend an order that may have been placed?), `_place`, `_reserve_logs`, `_settle_logs`, `_await_final`,
  `reconcile_orders` / `_apply_order` (every order status Kotak can show; MISSING_AFTER; tag / GuiOrdId matching;
  the order book's day boundary), restarts mid-run.
- **F — Cross-cutting** (`F-cross-cutting.md`). Overlapping runs (`launch`, `_exit_lock`, `_wait_for_entries`,
  `_stopped_at`), double taps and repeated confirmations (`_use_confirmation`, fingerprints, confirm ids), the
  scheduler (logins, entries, square-offs, true-ups, misfire grace, restarts, catch-ups, holidays added late),
  account isolation (one account's failure or slowness affecting others; thread pools, shared state), the panic
  button end to end (`run_exit(section=ALL)`), alerts and pop-ups (a failure without a pop-up, or a wrong one),
  the API routes that start or change trades (auth, which user's accounts and runs, input validation).

## RANDOMIZED SCENARIO TEST (`G-random-scenarios.md`)

Build backend/tests/wip_random_scenarios.py (pytest doesn't collect that name; run it by path), and only when it
is finished and its default seeds pass, copy it to backend/tests/test_random_scenarios.py. Do not change app code
or any existing file; extend the simulator inside the new file.
- A seeded generator of small scenarios: 1–3 family accounts (some holding their own shares or contracts), the
  four sections, random steps over one or more trading days: TradeLab actions through the real API (Buy now /
  Let's Trade, Sell / Exit, the panic button, square-off, true-ups), holders' own trades (sim.place_order without
  a TL tag), and Kotak faults and lags (PLACED_BUT lost replies, gateway pages, 5xx, stCode 1003, an unreadable
  order book, hide_positions, cancel_lag, partial_next, reject_in_book, clear_day_positions, settle_overnight).
- An ownership oracle from the simulator's records (orders tagged "TL…" are TradeLab's; a holder's sale takes the
  holder's own units first, then TradeLab's).
- Invariants after every step: (1) no duplicate orders (one Kotak order per TradeLab tag; no unknown TradeLab
  order); (2) never the holder's own units (no TradeLab close exceeds TradeLab's units on that key); (3) never a
  flip; (4) a run reported "done" (Exit, panic, square-off) leaves no TradeLab units open in its scope, otherwise
  it is partial/failed with an exit pop-up naming the account; (5) at quiet moments TradeLab's records per key
  equal the oracle's TradeLab units (except HANDOFF's accepted cases).
- A small default number of seeds (normal suite under ~1 minute extra; monkeypatch FINAL_CHECKS and CANCEL_DELAY
  small) and many more with an environment variable (TL_FUZZ_SEEDS=2000). Run thousands of seeds; shrink each
  distinct violation to a minimal reproduction; classify it (real bug / simulator gap / accepted case). Keep notes
  and findings with seeds in docs/audit/round1/G-random-scenarios.md. Real bugs' minimal reproductions stay in the
  final file marked pytest.mark.xfail(strict=True, reason=...).
