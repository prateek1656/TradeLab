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
