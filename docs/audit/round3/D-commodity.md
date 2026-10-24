# D - Commodity (MCX, NRML) - round 3 (work in progress)

## Checked so far
- Read `git show 00f240b` (all MCX hardening) and the code around it: `_units_doubt`, `_lot_of`, `broker_view.respelled`,
  `_shrink_to_broker`, `build_sell_plan` odd-lot refusal, `_tags_echoed` (two call sites), `MCX_FAMILIES`, `AFTER_MCX_GRACE`.

## Finding 1 (HIGH, CONFIRMED): a lost-reply close that never reached Kotak is never "Not placed" on the day after the entry; the next Exit says "done" with the position open
`trading.py:1561-1562` (`reconcile_orders`) needs `_tags_echoed(orders, segment)`: some TradeLab-tagged order of TODAY
in the same segment in the same account's book. A carried-forward NRML position (Commodity, F&O) is normally closed
by the first TradeLab order of that segment that day, so the book has no TradeLab tag. If that close's reply is lost
(timeout / 5xx) and the order never reached Kotak, the row stays `unknown` for ever (until midnight it also blocks
`_shrink_to_broker`; afterwards nobody looks at it). But `tradelab_book` counts a close in status `unknown` as done
(`trading.py:2713`), so the ledger is 0: the next Exit/panic says "Nothing left to close", the run is "done", and the
position (here 2 lots of CRUDEOILM) stays open at Kotak with no record and no warning. Before 00f240b the row became
"Not placed" after 60 s and the next Exit re-sent the close. Same for NSE F&O (test below); Holding Sell (first
order of the day in nse_cm) goes through the same lines (not run).
Test (fails; the same flow with `_tags_echoed` forced True passes):
```python
def test_lost_never_placed_exit_close_is_forgotten(client, owner, clock, sim, monkeypatch):
    acct, ts, lot = _mcx_two_lots(client, owner, clock, sim)
    _next_morning(acct, clock)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    acct.faults["place_order"] = [{"message": "Endpoint request timed out"}]  # never reached Kotak
    _, run1 = _run(client, owner, "commodity", "sell")          # partial, 1 row unknown (correct)
    trading.reconcile_orders(client.get("/api/auth/me", headers=owner).json()["id"])
    pv, run2 = _run(client, owner, "commodity", "sell")          # "done", "Commodity holds nothing", 0 orders
    assert acct.net(ts, "NRML") == 0      # fails: 20 units still open
```
Smallest fix: remember the proof durably. When any book read shows a `TL`/`SQ` tag for (account, segment), store it
(e.g. `tag_echo_seen` on the account or a small collection) and let `_tags_echoed` return True if either the current
book shows a tag or that proof exists for the account/segment. (The echo is a property of Kotak's API for the segment, not of the day.)
Also have `tradelab_book` not count an `unknown` close older than N minutes as closed while it is unresolved, or
make Exit refuse ("an earlier close is still unknown") instead of "Nothing left to close".
