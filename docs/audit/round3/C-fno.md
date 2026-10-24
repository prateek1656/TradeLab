# C — F&O (round 3)

Auditor C, round 3. Confirmed findings come with failing tests through the real adapter (`kotak_sim`); the scratch
file `backend/tests/review_C_scratch_test.py` is deleted at the end. Helpers (`_pair_items`, `_filtered_sell`, `_run`,
`sim`, `IN_SESSION`) are those of `backend/tests/test_kotak_replies_end_to_end.py`.

## Checked so far

- Buy in waves (`_sales_to_wait_for`, `_fire_in_waves(closing=False)`): hedge refused / part filled / still working /
  unknown reply all keep the sold leg unsold and give a failed row + `entry` pop-up; two accounts independent;
  other-underlying hedge does not wait; sliced hedge (RELIANCE futures, 25 lots = 20 + 5) with the 2nd slice refused keeps
  the short unsold; sliced short with the 2nd slice refused leaves a partial run + alert and a later Exit closes
  everything (net 0 / 0).
- Panic tapped mid-way: (a) while the hedge is still working (part filled): Buy ends with the short "Not sold", the
  panic cancels the rest of the hedge and sells the filled part, net 0 / 0; (b) while the sold leg is being sent: the
  panic waits for the Buy (`_wait_for_entries`), then buys back the short and sells the hedge, net 0 / 0.
- `_hold_hedges` when the short is in the same filter, in another underlying, or the filter is `opened="S"`: fine.
- `_units_doubt` / odd-lot refusal on NSE F&O: reached only when Kotak shows LESS than the records; `_contract_changed`
  runs first, so a lot-size change is held there; partial fills are whole lots on NSE, so `shown % lot` is no false
  positive for a holder's partial close (65 of 195 etc.). The `shown * lot == abs(ours)` clause only fires for
  `ours == lot**2 * k`: no realistic false positive.
- Alerts (`/api/alerts` kind `entry`): a funds-only partial run still shows as `funds`; hedge refused shows `entry`.
- Panic per-account NRML flow: each account's `build_sell_plan(account_ids=[a.id], panic=True)`; the reconcile races
  are last-writer-wins on rows that the book re-derives (heals at the next reconcile, only ever conservative).

## Findings (most severe first)

### 1. CONFIRMED — a filtered Exit of the hedge sells it beside a short whose buy-back is still working or has no clear reply: naked short (and the screen said "held" while the run sold)

**Severity: medium-high** (a naked sold option, margin and unlimited-style risk on one account; reachable in normal use
after an Exit whose buy-back stays "Still working" or ends "no reply from Kotak", then "Sell" on the hedge alone).

**Scenario.** A hedged pair is open. The owner sells the short alone (`symbols=[short]` or `opened="S"`). Its
buy-back part fills and keeps working (65 of 130), or Kotak answers with a gateway page (outcome unknown, maybe never
placed). Run 1 is "partial" with a pop-up. The owner then sells the hedge alone (`symbols=[hedge]`, or `opened="B"` in
the unknown case). `_hold_hedges` is meant to stop exactly this, but `stays_open` is built from `tradelab_open(...)`,
and a close "counts in full as soon as it may have gone through" (`tradelab_book`, `closes_by_fill=False` in the run).
So the short with its working or unknown buy-back is no longer "open", `stays_open` is empty and the hedge is sold:
short -65 (working) or -130 (never placed), hedge 0, run "done".
- The **preview** passes `closes_by_fill=True`, so for the *working* case it shows the hedge as "Not sold: the sold leg
  it hedges stays open" while the confirmed run sells it. The screen and the run disagree, in the dangerous direction.
- `symbols=[hedge]` does not cancel the short's working buy-back either (`_cancel_own_open_orders(section, symbols)`
  only touches the hedge). `opened="B"` cancels every working order of the section first, which is why the *working /
  opened* combination is safe; the *unknown* one is not (nothing to cancel, the close still counts as done).
- Unlike the full Exit, the filtered one never looks at `unfinished_keys` / `unconfirmed_orders` for the shorts it left
  out, so the "a close that isn't proven holds the hedge" rule of `_hedges_to_wait_for(plan, unclosed)` is missing here.

**Where.** `trading.py` l.3193-3197 (`stays_open` from `tradelab_open(..., closes_by_fill=preview)`), `_hold_hedges`
l.3312; `tradelab_book` l.2715 (`closes_by_fill` only discounts a close that has a `filled`; an unknown or
not-yet-shown close has none).

**Test** (three of four combinations fail: working+symbols, unknown+symbols, unknown+opened):

```python
@pytest.mark.parametrize("variant", ["symbols", "opened"])
@pytest.mark.parametrize("how", ["working", "unknown"])
def test_filtered_exit_hedge_not_sold_beside_short_whose_close_is_not_proven(client, owner, clock, sim, monkeypatch, how, variant):
    from test_kotak_replies_end_to_end import _pair_items
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "FINAL_CHECKS", (0.05,))
    acct, s_ts, h_ts, lot = _pair_items(client, owner, sim, 2)
    _, run = _run(client, owner, "fno", "buy")
    assert run.status == "done", run.summary
    if how == "working":
        acct.partial_next = lot                                   # the buy-back part fills, the rest keeps working
    else:
        acct.faults["place_order"] = [{"message": "Service Unavailable"}]   # outcome unknown, never placed
    _filtered_sell(client, owner, symbols=[s_ts])
    pv, run2 = _filtered_sell(client, owner, **({"symbols": [h_ts]} if variant == "symbols" else {"opened": "B"}))
    assert not (acct.net(s_ts, "NRML") < 0 and acct.net(h_ts, "NRML") == 0), "naked short"
```
(`_filtered_sell` is the helper of the e2e file: preview, then confirm with its fingerprint.) Output: working+symbols
"exit hedge: done 1 order placed short -65 hedge 0" although the preview row said "Not sold…"; unknown: "short -130 hedge 0".

**Smallest fix.** Build `stays_open` conservatively and the same for preview and run: sold contracts of the whole
section that are open when a close counts only by what Kotak *confirmed filled* (a close that is working, pending,
unknown or whose `filled` is None counts 0), i.e. add a `closes_unproven=True` mode to `tradelab_book` (non-final
close rows: `qty = min(qty, filled or 0)`, UNKNOWN/pending rows skipped) and call
`tradelab_open(..., closes_by_fill=True, ...)` with it for `whole` in both modes (no `preview` switch). Optionally also
add `k` of `unfinished_keys(user_id)` that are short in `whole` ignoring closes.

### 2. CONFIRMED (low) — a stale sold record blocks a filtered Exit of the hedge until the nightly true-up

**Severity: low** (fail-safe: the hedge is kept, an unfiltered Exit or the nightly `true_up_all` clears it; the cost is a
long option decaying and a misleading "the sold leg it hedges stays open").
If the holder closes the short in the Kotak app, `stays_open` still lists it: `whole` is TradeLab's record, and the
filtered Exit shrinks only `ledger` (the filtered keys), never the shorts it left out. `opened="B"` then holds the hedge
with "the sold leg stays open" although Kotak shows no short.

```python
def test_filtered_exit_stale_short(client, owner, clock, sim):
    from test_kotak_replies_end_to_end import _hedged_pair
    clock.set(IN_SESSION)
    acct, s_ts, h_ts, lot = _hedged_pair(client, owner, sim)
    acct.open_positions = {k: p for k, p in acct.open_positions.items() if k[1] != s_ts}   # closed in the Kotak app
    _filtered_sell(client, owner, opened="B")
    assert acct.net(h_ts, "NRML") == 0          # fails: 65, run "failed", row HELD_HEDGE
```
**Fix.** Drop from `stays_open` the shorts Kotak no longer shows (run `_shrink_to_broker(write=False)` over them, or
compare with `broker_view` for those keys before holding).

### 3. CONFIRMED (low) — one account's square-off from the Accounts screen drops another account's short leg

**Severity: low** (fail-safe: premium of a hedge bought for nothing, a missed trade, and a pop-up). `run_exit` sets
`_stopped_at[user_id]` for any `section == ALL` run, also with `account_ids=[one account]`. A Buy of a hedged pair in
*another* account that is between its hedge and its short then ends "Not sold: “Square off everything” was tapped while
the hedge was being bought" (the new wave check `_stopped_at >= began`, `_fire_in_waves` l.1279). Test: Buy in Acc A
(hedge slow), `POST /api/accounts/<Acc Z>/square-off` while the hedge is being sent: run "partial", A holds the hedge
only (net short 0, hedge +65). **Fix.** Key `_stopped_at` by `(user, account)` for an account-limited square-off, or
only honour it in `_fire_in_waves` for accounts the square-off covers.

### 4. PLAUSIBLE (low) — the funds pop-up can hide an entry problem in the same account

`alerts.list_alerts`: `set(names) - set(funds_short)` is empty when the account that had a funds-short skip also has
the failed or unknown row (a half-filled sold leg, an unknown reply, a hedge refused for another reason), so only
"Not enough funds" is shown and the "may be missing its hedge" text is not. The run is still `partial` and visible in
Activity; nothing is left unmanaged (unknown rows resolve from the book). **Fix.** Show `entry` whenever any failed or
unknown row exists that is not itself a funds skip (`funds_short` per row), not by account name.

### 5. PLAUSIBLE (info) — crash entries of the panic's carry-forward part are never written to the log

`account_close` / `close(key)` (`run_exit`, "Carry-forward close crashed", "… sell crashed") return `LogEntry`s with an
`account_id` that are only added to `entries` after `_write_logs` ran, so the run is partial/failed but no OrderLog row
names the account: the exit pop-up lists no account and Activity shows nothing for it. Only on a crash (database,
programming error); the closes themselves are unaffected. **Fix.** `_write_logs(user_id, run_id, res, key)` in the
`except` branches.

## Looked at and found sound (new code)

- `_fire_in_waves` opening mode: skipped hedges, refused / part-filled / working / unknown hedges, sliced hedge and sliced
  sold leg, a crashed account (returns a failed entry, other accounts unaffected), `_stopped_at` race (a panic after the
  check is safe because the panic waits for the Buy in `_wait_for_entries`, 120 s, longer than any wave).
- `_short_closes(closing=False)` rows do not enter `tradelab_book` (failed), do not duplicate the skip rows
  (`_run_buy` writes `_skip_entries(plan)` once), and make the run partial so the `entry` pop-up names the account.
- Panic per-account flow: ordering inside an account (intraday, cancel, reconcile, then fno || commodity), crash
  isolation, `_account_pool`; reconcile_orders races only produce last-writer-wins on `$set` of fields the order book
  re-derives (heals at the next reconcile, never in the unsafe direction).
- Odd-lot refusal and `_units_doubt`: no false positive found for NSE/BSE F&O (whole-lot fills, `_contract_changed` first,
  holder's partial closes in whole lots).
