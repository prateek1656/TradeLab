# B — Holding (CNC) — round 1 (in progress)

Auditor B. Scratch tests: `backend/tests/review_B_scratch_test.py` (deleted at the end; the test code of each
confirmed finding is copied here).

## Checked so far

- Read end to end: `docs/HANDOFF.md`, `CLAUDE.md`, `app/broker/base.py`, `app/broker/kotak.py`,
  `app/broker/manager.py`, `app/services/sections.py`, `app/services/scheduler.py`, `app/services/trading.py`
  (Buy now / `build_entry_plan` for CNC, `fire_plan`, `_await_final`, `reconcile_orders` / `_apply_order`,
  `tradelab_book`, `broker_view`, `_shrink_to_broker`, `_previous_day_delivery_buys`, `_written_off_today`,
  `true_up_all`, `build_sell_plan`, `_run_sell`, `section_positions`), `tests/kotak_sim.py`, the Holding tests
  in `tests/test_kotak_replies_end_to_end.py`.
- Existing Holding end-to-end tests pass (19).
- Leads being checked: see the findings below as they are confirmed.

## Findings

(none confirmed yet)
