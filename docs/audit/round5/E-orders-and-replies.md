# E — Order lifecycle and Kotak replies (round 5) — final report

## Checked so far

- Round 1-4 E files, HANDOFF and `git diff eddbe8a HEAD -- backend/app` read (trading.py, models.py; alerts/scheduler are area F).
- **Zero-row reply (adapter, `kotak.py` `_report_rows` / `_is_no_data`).** A positions or order-book call returns `[]` for exactly
  these replies: a bare list `[]`; `{"data": []}`; `{"stat": "Ok"}` / `{"stat":"Ok","data":None}` (an "Ok" with no data key at
  all); stCode 5203 with any message that is not "not empty" wording; and `errMsg`/`emsg`/`error[]` messages that fully match
  "No Data / data not found / no positions|holdings|orders|trades|records [found]" (client code and a few tails stripped).
  Everything else (gateway pages, `data` that is a dict/str, "no data" inside a longer sentence, "no data" beside rows, an Ok
  reply with a non-list `data`) raises. 5xx / non-JSON / timeouts raise from the SDK wrapper. A transient reply therefore
  qualifies only if it is one of the shapes above; I found none that Kotak is known to send by accident. The only soft spot is
  `{"stat":"Ok"}` without a `data` key (see note N1).
- **`_traded_today` rule.** `view.rows == 0` is evidence of zero only for a derivative key with no TradeLab row of today in
  status success / pending / unknown. Reconciled against the lag defences: a today's TradeLab fill, a still-unknown order and
  a sending order all keep the record. Holder-only trades today do not matter (net is pooled by Kotak). OK.
- **Old unknown closes (`unresolved_old_closes`, `_finish_unknown` in `_shrink_to_broker`).** Finishing only runs inside
  `_writedown_lock`, only with `write`, so never in a preview and never 00:00-09:00. The condition is "key not listed AND keep
  == 0" (no lag, no settling) AND not a CNC key. Old rows are always of an earlier IST day and orders go out as DAY / amo=NO, so
  an old order cannot fill later: finishing cannot double count a later close. A finished close counts as fully sent, and the
  re-read `tradelab_open` result is what is compared, so ledger and records land on the same figure whichever order the rows
  were finished in (checked: d0 +2, d1 unknown close, d2 confirmed close, d4 new +2 -> ends at 0 either way, no phantom short).
  Evidence is the same "nothing listed" that already justifies an ADJUSTED write-down.
- **Lock interplay.** The finish happens before the `now != ours / unfinished_keys` re-read, so it is committed even when the
  write-down itself is then skipped. That is safe: the evidence for it (Kotak lists nothing, read before the lock) does not
  depend on the skipped writer, and a concurrent Buy filling the key afterwards cannot revive an order of an earlier day.
  `_finish_unknown` in `build_sell_plan` (entries) runs outside the lock but touches only UNKNOWN opening rows, which
  `tradelab_book` never counts, so no ledger figure moves. No nested locks, no lock across a Kotak call.
- `_flat_at_kotak`, `STALE_RECORD`, `ExecutionRun.scope_*`: only read for their E effects (no order is sent by them).
- Not changed since round 4 and re-read for regressions: `_place`, `_reserve_logs`, `_settle_logs`, `_await_final`,
  `reconcile_orders` / `_apply_order`, `MISSING_AFTER`, tag proofs, manager `call()` retry (only SessionExpired from a refusal
  that proves nothing was placed re-sends; `OrderOutcomeUnknown` never does). Nothing new.
- Randomized harness: `TL_FUZZ_PROFILE=derivs` 400 seeds from 70000: passed. holding / broad / race / hedge 300 seeds each
  from 81000: see the bottom of this file.

## Findings

### 1. CONFIRMED — an unknown Holding Buy of an earlier day is finished while its units are still settling, then forgotten for good

Severity: medium. Money terms: TradeLab's bought ETF units are silently left out of TradeLab's records; every later Sell/Exit
says "Holding holds nothing bought by TradeLab" and the run is `done`, so the owner believes the position is closed or never
existed while the units sit in the demat. No units are lost or sold wrongly, but "a TradeLab position is never forgotten or left
open silently" is broken. Likelihood: low per Buy (needs a lost Buy reply that the book did not resolve: no tag echo / unreadable
book at the time, the known open live check) AND an Exit/Sell run (or panic) while the units are in the T+1 gap. That gap is the
whole next trading day, so any Sell/Exit press on the morning after is enough.

Scenario: Holding Buy of ITBEES, reply lost, the order did fill; the row stays UNKNOWN to the end of the day. Next day Kotak's
positions have cleared and the demat does not show the units yet (T+1). Sell/Exit: `build_sell_plan.prepare()` sees the old
unknown entry, calls `broker_view(...).listed` for it, and a CNC key counts as listed only if today's positions or the demat
hold the symbol, so it is "gone" and `_finish_unknown` sets `final=True`. The units reach the demat the following morning; the
row no longer exists as a guard, the ledger never had them, so Sell reports nothing and finishes `done`. (Before the finishing
write the key came back as "An earlier buy of this ... nothing is sent" as soon as the units appeared in the demat.)

Where: `trading.py` `build_sell_plan.prepare()` (`old` / `gone` / `_finish_unknown([unknown_buys[k] for k in gone])`, about
lines 3592-3604). Introduced by the round-4 C4-1 fix (fa85882); `_shrink_to_broker`'s own finishing is already limited to
`key.product != "CNC"`, this one is not.

Test (fails today): `backend/tests/review_E_scratch_test.py::test_E_cnc_unknown_buy_finished_while_settling` (deleted with the
scratch file; it is reproduced here):

```python
def test_E_cnc_unknown_buy_finished_while_settling(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=50000.0)
    _add_etf(client, owner, "ITBEES")
    acct = sim.account("AB101")
    database()["tag_proofs"].delete_many({})
    acct.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]
    _blank_tags(acct)
    _run(client, owner, "holding", "buy")
    acct.faults.clear()
    trading.reconcile_orders(_user_id(client, owner))
    (row,) = _logs(action="buy")
    assert row.status == "unknown"
    bought = acct.net("ITBEES-EQ", "CNC")
    acct.clear_day_positions()                     # next day: positions cleared, demat not credited yet
    _age_rows(_trading_days_back(1))
    clock.set(datetime(2026, 10, 15, 10, 0, tzinfo=IST))
    _run(client, owner, "holding", "sell")         # -> "Holding holds nothing bought by TradeLab", row now final
    acct.demat["ITBEES-EQ"] = [bought, bought, 30.0]   # the units arrive
    clock.set(datetime(2026, 10, 16, 10, 0, tzinfo=IST))
    pv, run = _run(client, owner, "holding", "sell")   # still "done", 0 orders, nothing reported
    assert [r.final for r in _logs(action="buy")] == [False]   # fails: [True]
```

Smallest fix: in `prepare()` take `gone` only for non-CNC keys; for a CNC key finish the old unknown buy only once its units
cannot still be settling (`_aware(r.created_at).date() < _settling_since(today)`, the same window `_previous_day_delivery_buys`
uses) and the symbol is in neither positions nor demat. Until then keep it reported like a listed key.

## Notes (not findings)

- N1. `_report_rows` reads `{"stat":"Ok"}` with no `data` key as an empty report (a unit test pins it). Zero rows now justifies
  write-downs (`_traded_today` rule), the finishing above and `_flat_at_kotak`. If Kotak ever answers a positions call with an
  Ok envelope whose rows sit under another key, every derivative record of the account would be written off at once. No such
  reply is documented; requiring `data` to be present (list) or stCode 5203 for positions/orders would remove the risk at no cost.
- N2. CNC old unknown closes are never finished (by design: sized by the holder note). They keep every later Holding Sell of
  that ETF on the stricter note-based sizing; tested: a later Buy and Sell still sell fully when a note exists.
