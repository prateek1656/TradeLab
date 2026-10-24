# A — Intraday (MIS), round 5 — report

## Checked so far

- Read round 1-4 A files, HANDOFF, `git diff eddbe8a HEAD` (alerts.py, scheduler.py, models.py, SectionKit.tsx,
  trading.py run_exit / `_set_scope` / `_record_missed_exit`).
- `_record_missed_exit` suppression rules traced: auto_exit off (checked first, both entry points), `_already_ran`,
  nothing traded, any earlier exit run of the section (any status), a panic / per-account run only if its scope covers
  every account that traded, idempotent via the "didn't run" summary. Holidays/weekends: nothing traded -> nothing.
  A4-1 (server down the whole window) is fixed by the `elif at >= sell_until` branch. No wrong pop-up found in normal use.
- `list_alerts` scope logic traced (coverage by account + symbol, `bad` accounts per newer run, merge of collapsed
  names, `dismiss` marking lower ids). Existing tests re-read; held up for MIS/panic/per-account cases.
- Panic flow: `_stopped_at` only for the all-accounts panic; `_cancel_armed_today` failure no longer stops the closes;
  per-account run does not stop other accounts' Buys; re-arm after a panic is possible (cancelled session is re-armed).
- Frontend PositionsCard change: unguarded `data.unclear/settling` are always present in the API type; fine.
- Randomized harness: 400 seeds `intraday` (start 95000), 500 seeds `race` (start 96000): no violation; `broad`
  partly run (timeout), no violation seen.

## Result

No wrong order, double trade or forgotten TradeLab position found. Two LOW alerting gaps in the new scope-aware
pop-up logic (no order is sent wrongly; the position stays visible in Positions / Activity).

### A5-1. CONFIRMED (LOW) — a done "Exit bought" / "Exit sold" (opened filter) hides the failed Exit's pop-up for the other side

- Money: a short F&O/MCX contract left open by a failed Exit stops being named in any pop-up after the owner taps the
  other-side Exit (e.g. "Exit bought") which finds nothing to do and is "done". A naked short stays open with no alert.
- Where: `trading._run_sell` calls `_set_scope(run_id, all accounts, symbols)` and ignores `opened`; `alerts.list_alerts`
  then treats the done run as covering every account and contract.
- Test: `backend/tests/review_A_scratch_test.py::test_exit_bought_hides_failed_short_popup` (scratch file deleted;
  buy a short nifty future and a long banknifty future, refuse buy-backs, Exit -> partial + pop-up "Acc A"; then
  `sell` with `opened="B"` -> "done, F&O holds nothing bought by TradeLab", pop-up gone, short still -65).
- Smallest fix: in `_run_sell` call `_set_scope(run_id, [], symbols)` (or skip `_set_scope`) when `opened` is set.

### A5-2. CONFIRMED (LOW) — account-less failures of a run (e.g. the panic could not cancel today's Let's Trade) are hidden by any later done run with a scope

- Money: if the panic's "Could not cancel today's Let's Trade confirmations" fails (database blip), the armed buy can
  still go out after the panic; the pop-up (accounts empty) disappears as soon as the owner squares off any one
  account from the Accounts screen (done, `section=ALL`, same key).
- Where: `alerts.list_alerts`: `if not rows and any(e.status == "done" for e in newer): continue` does not check the
  scope; account-less failed rows (`account_id` None: crash rows, cancel-confirmation failure) are never "covered".
- Test: `test_account_less_failure_of_panic_hidden_by_other_accounts_square_off` (monkeypatch `_cancel_armed_today` to
  raise; panic -> failed, pop-up with `accounts: []`; `POST /accounts/{b}/square-off` -> done; pop-up gone).
- Smallest fix: for a failed run whose failed rows are all account-less, hide it only behind a done run that covered all
  accounts the failed run covered (`set(r.scope_accounts) <= set(e.scope_accounts)`), and treat a run with no scope as
  uncovered.
