# E — Order lifecycle and Kotak replies (round 1) — in progress

## Checked so far

- SDK source (neo_api_client in /home/user/venv-tl): `place_order`, `cancel_order`, `order_report`, `positions`,
  `holdings`, `limits`, `margin_required`, `rest.py` (timeouts and connect errors become `ApiException(status=0)`;
  `raise_on_error` is off, so HTTP 4xx/5xx bodies come back parsed as JSON, a non-JSON body as `{"Error": exc}`).
- Adapter `kotak.py`: `_error_message`, `_raise_for`, `_unclear_reply`, `_is_no_data`, `_report_rows`,
  `place_market_order`, `cancel_order`, `orders`, `positions`, `holdings`, `funds`, `margin_per_lot`.
- Broker manager `manager.py`: `call` (one re-login and retry on `SessionExpired`), `_ensure`, `_login`.
- `trading.py`: `_place`, `fire_plan`, `_reserve_logs`, `_settle_logs`, `_await_final`, `_short_closes`,
  `reconcile_orders`, `_apply_order`, `MISSING_AFTER`, `square_off_account`, `_cancel_own_open_orders`,
  `_confirm_cancels`, `tradelab_book`, `unfinished_keys`, `unconfirmed_orders`, `_fills_today`.
- Still to do: restart recovery (`recover_after_restart`, `launch`), `build_sell_plan` / `_run_sell` order
  handling, confirming the leads below with simulator tests.

## Findings

(none confirmed yet)

## Leads being confirmed

1. `MISSING_AFTER` counts from when the row was reserved (`created_at`, before sending), not from when the send
   finished: with many orders through the 32-thread pool and a slow Kotak, a reconcile (Trade page, Sell preview,
   `_await_final`) can mark an order "Not placed" (final) while it is still being sent or just timed out, and
   `_settle_logs` then overwrites the status but leaves `final=True`, so the order is never followed again.
