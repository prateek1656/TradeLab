# E — Order lifecycle and Kotak replies (round 3) — work in progress

## Checked so far

- Read round-1/2 E files and HANDOFF; read `git diff 7632274 HEAD -- backend/app` for trading.py / kotak.py.
- `_unsettled_to_unknown` / `_abandoned_to_unknown` vs `_live`, `_settle_logs`, `_finish_run` (a live run's pending rows are
  never touched; the update is `status: pending`-guarded, so a row settled in between is skipped): no race found.
- `reconcile_orders` from several threads (`_await_final(row_ids)`, the unfiltered call inside `build_sell_plan` of each panic
  thread, `reconcile_today`): `_apply_order` writes absolute values (filled, quantity, status), the session saves only
  changed fields, a write conflict replays the same `$set`; nothing is applied additively, so no double count. Only a
  stale-view/fresh-view mix on an UNKNOWN row (open view overwriting a cancelled one) is possible; it needs a lost reply plus
  a state change inside the same few milliseconds: not reported.
- Opening waves (`_sales_to_wait_for`, `_fire_in_waves(closing=False)`, `_short_closes(closing=False)`): hedge refused,
  part filled, skipped, unknown, still working all hold the sale; `_stopped_at` honoured.
- `_earlier_closes`: rows counted are exit-run rows of the account, MIS, success/unknown, today; `ident = order_id or tag`
  matches `found` (keyed by both). A reopened position after a restart is area A's (their scratch test covers it).

## Findings

### 1. CONFIRMED — a close that never reached Kotak stays UNKNOWN for good when the day's book has no TradeLab-tagged order in that segment; every later Exit says "holds nothing" (done) with the position open

See the final report below (filled in at the end).
