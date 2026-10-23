# F — Cross-cutting (round 1) — in progress

## Checked so far

- Read end to end: `trading.py` (launch, `_exit_lock`, `_wait_for_entries`, `_stopped_at`, `_use_confirmation`,
  arm / cancel / claim / firing, run_entry, run_exit incl. ALL, square_off_account, `_cancel_own_open_orders`,
  `_confirm_cancels`, build_entry_plan `ready_by`, build_sell_plan, fire_plan, recover_after_restart),
  `scheduler.py` (jobs, misfire grace, catch-ups, warm-up, retries), `broker/manager.py`, `broker/kotak.py`,
  `db.py` (session / claim semantics), routers (automation, sections, accounts, basket, alerts, holidays, auth).
- Looked at and found sound so far: two taps on Buy / Sell with the same confirm id; Buy and Sell of one
  on-command section never overlap; Sell / panic / square-off serialize on `_exit_lock`; Let's Trade claim
  ("armed" -> "running" -> "firing") vs cancel / panic; panic waits for a buy already firing; restart recovery
  (running runs -> failed "Interrupted", pending orders -> unknown, catch-up square-off re-runs).

## Leads being confirmed

1. Per-account "Square off" (`/api/accounts/{id}/square-off` -> `run_exit(account_ids=[id], section=ALL)`) closes
   TradeLab's F&O / Commodity NRML contracts in every account (`build_sell_plan` ignores `account_ids`), without
   first cancelling those accounts' working orders. Same lead as C (`C-fno.md`); F confirms the panic-path angle.
