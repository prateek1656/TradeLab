# E — Order lifecycle and Kotak replies (round 4) — final report

## Checked so far

- Round 1-3 E files, HANDOFF and `git diff f360223 HEAD -- backend/app` read.
- `_note_tags` / `tag_proofs` / `_tags_echoed(account_id)` / `_look_alike`: proof is per account and segment, written from every
  book read in `reconcile_orders` and `_stray_orders`; matching is exact on tag and order id; `_look_alike` tolerates `-EQ` and
  prefix spellings and is conservative on side/segment. No path found that marks a placed order "Not placed" other than the
  already-accepted "Kotak's book shows it later than MISSING_AFTER (60 s after the reply settled, `sent_at`)". With a proof, a
  holder's untagged order never blocks; without any proof it blocks the row for the rest of that day only (see finding 2).
- `reconcile_orders` rows from `_unsettled_to_unknown`, `_abandoned_to_unknown`, `recover_after_restart`: all get `sent_at`
  (restart: now), so the 60 s wait starts after the conversion; a still-sending run's rows stay `pending` and are skipped.
- `_writedown_lock`: one non-reentrant lock per account, taken at one site, never nested, never held across a Kotak call; the
  re-read (`tradelab_open` with the same arguments as the caller's ledger, plus `unfinished_keys`) is the same function the ledger
  came from, so `now != ours` is a faithful "someone changed it" test. A concurrent write-down, close or Buy makes the second
  writer skip. No deadlock, no double write-down found.
- `unresolved_closes` (today only, `final != True`, entries excluded) and `unresolved_entries` (any day, MIS of earlier days
  excluded) read for day boundaries at IST midnight; `tradelab_book` counts an older UNKNOWN close as 0 unless filled.

## Findings

### 1. CONFIRMED — an earlier day's unknown Holding sale is "not closed" the next day, so Exit sells the HOLDER's own units when she holds at least as many as TradeLab sold

Severity: high in money terms (a family member's own shares sold; the core rule). Likelihood: low per sale (needs a sale whose reply
was lost AND whose row is still unknown at the end of its day), but the precondition "holder owns the same ETF" is the normal
family setup.

Scenario: TradeLab bought 492 ITBEES for an account whose holder owns 100000. The next day Sell's order is placed but the reply is
lost; the row stays UNKNOWN to the end of the day. That happens when Kotak's book does not echo the tag (the live check round 3
left open), when the book could not be read after the 60 s wait, or when the server was down from the sale until the next morning
(`recover_after_restart` turns `pending` into UNKNOWN, and `reconcile_orders` never looks at rows of earlier days). Today the row
blocks (`unresolved_closes`). From the next day `tradelab_book` counts it as NOT closed ("Kotak's figure decides"), but Kotak's
figure is the holder's 100000, which is at least TradeLab's 492, so `_shrink_to_broker` writes nothing down and Exit sells 492 more:
the holder's. (Delivery `_delivery_sizes` only subtracts the holder's units while TradeLab's are settling or pledged, which is
not the case the day after a sale.)

Where: `trading.py` `tradelab_book` (the `not opening and status == UNKNOWN and not final and created < day_start` branch,
about line 2815), `build_sell_plan` (no check for an older unknown close), `_delivery_sizes`.

Test (fails today; copy at `/tmp/claude-0/-home-user-TradeLab/e000eb7b-c0d1-569a-a23f-04bde988e670/scratchpad/E4_scratch_copy.py`,
`test_E1_unknown_holding_sale_yesterday_sells_holders_units`): same as `test_an_unknown_holding_sale_of_yesterday_is_never_sent_twice`
(went_through=True) but the account's demat starts with `{"ITBEES-EQ": [100000, 100000, 30.0]}`; ends with
`assert acct.demat["ITBEES-EQ"][0] == 100000` failing with `(99508, 100000, 492)`.

Smallest fix: in `build_sell_plan`, for an UNKNOWN non-final close of an earlier day on a key, size at
`min(ours, shown - holder_noted_own)` (`_holders_own_noted`, the 15:40 note) and send nothing when there is no note; for F&O/MCX
keys (holder's own contract is rare) keep the key reported while Kotak lists it, like `unresolved_entries` does.

### 2. CONFIRMED — an unresolved opening order of an earlier day blocks the whole key for ever, including TradeLab's later, confirmed units

Severity: medium-low (fails safe, always reported, but TradeLab can never sell those units; the owner must do it in the Kotak
app, and nothing in TradeLab clears the row). Likelihood: low.

Scenario: the account's first-ever Holding Buy (so no tag proof yet) loses its reply and never reaches Kotak, and the holder's own
untagged buy of the same ETF is in the book that day, so `_look_alike` keeps the row UNKNOWN (also: server down overnight after a
crash mid-Buy). The next day `reconcile_orders` ignores it (the book is gone), `unresolved_entries` keeps it while Kotak lists the
key, and the holder's own ITBEES keeps the key listed. A later Buy succeeds (tags are now proved), but every Sell of that key
answers "An earlier buy of this ... got no reply ... nothing is sent" and "0 orders placed · 1 failed": the 492 confirmed
units are stranded. `prepare()` drops the whole key (`keys = [k for k in keys if k not in unknown_buys or k in gone]`), not just
the unknown part.

Where: `build_sell_plan.prepare` (the `mystery` block, about line 3440), `unresolved_entries`.

Test: `test_E2_old_unknown_buy_strands_later_confirmed_units` in the same copy; ends `assert acct.demat["ITBEES-EQ"][0] == 1000`
failing with `1492`, run summary `0 orders placed · 1 failed`.

Smallest fix: keep reporting the old unknown entry (error row, run partial), but do not remove the key from `keys` when the
ledger holds confirmed units: size those as usual (they are TradeLab's by its own records; the cap `min(ours, Kotak's figure)`
already protects the holder). Optionally also: once a tag proof exists for the account/segment, let a later reconcile call mark an
older unknown row of a key that no longer appears in Kotak's positions as final.

## Nothing else real found

- Late/differently spelled order books: the only way to a resend is the book lagging more than MISSING_AFTER, already accepted.
  Spelling differences in the symbol only matter with no tag proof, and `_look_alike` is prefix based both ways; a spelling it
  cannot connect (e.g. a renamed contract) would be needed.
- `_writedown_lock` is correct and cannot deadlock (see the checked list).
- Day boundaries of `unresolved_closes` / `unresolved_entries`: between 00:00 and the book reset an older row is not looked up,
  but nothing is written then (NIGHT_ENDS) and the positions decide; no extra issue.

## Live checks that only Kotak can settle

- That `order_report` echoes the tag as `GuiOrdId` for every segment and product; findings 1 and 2 only arise when it does not
  (or when the server was down / the book unreadable all day).
- How long after an accepted order the order book may take to list it (MISSING_AFTER is 60 s).
