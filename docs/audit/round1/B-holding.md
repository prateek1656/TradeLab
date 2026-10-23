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

### B1 — CONFIRMED — the holder sells her OWN units the day after TradeLab bought the same ETF: TradeLab writes off its settling units and forgets them for good

Severity: TradeLab silently forgets up to k of its units (k = what the holder sold of her own that day); they stay in
her demat, TradeLab never sells them, and the later Sell reports "done". No money leaves the family, but a TradeLab
position is left open silently (for Mummy Ji-type accounts that own the same ETF: easily hundreds of units).

Scenario: Mummy Ji owns 10 ITBEES herself. Day 1 TradeLab buys 19 ITBEES for her account. Day 2 those 19 are still
settling (in neither Kotak's positions nor the demat — the T+1 gap the code allows for) and she sells 5 of her own
10 in the Kotak Neo app. The 15:40 true-up writes off 5 of TradeLab's 19 ("Closed outside TradeLab"). Day 3 the 19
arrive; Sell sells 14 and reports done; she is left with 10 instead of her own 5.

Where: `backend/app/services/trading.py:2506-2513` (`_shrink_to_broker`, the previous-day branch):
`outside = day_sold - sold; cap = min(cap + prev, max(cap, before_today - outside))` attributes every sale made
outside TradeLab today to TradeLab's records whenever Kotak shows less than they count — but here Kotak shows less
only because TradeLab's units haven't arrived yet; the units sold were the holder's own (the repo's ownership rule,
`docs/audit/PROMPTS.md` G: a holder's sale takes the holder's own units first). It happens whether or not Kotak's
holdings `quantity` drops on a same-day sale (shown 5 or 10 < 19 both trigger it), and only when the holder's
remaining own units are fewer than TradeLab's settling units. If Kotak shows T1 units in its holdings (the open
live-test question in HANDOFF step 4) it can't happen; the code is built for the case where it doesn't.

Trade-off to keep in mind: from today's Kotak figures alone this can't be told apart from "TradeLab's units arrived
during the day and the holder sold *them*" (holder owns none), which the current rule handles. Today's figures are
the same in both cases (same shown, same day_sold, same records).

Test (fails today: `TradeLab wrote off [5] of its 19 units and left her 10 (her own: 5)`):

```python
def test_b1_holder_selling_her_own_units_while_tradelab_units_settle_forgets_nothing(client, owner, clock, sim):
    clock.set(IN_SESSION)
    mummy_id = _add(client, owner, sim, "Mummy Ji", "CD202", cash=2000.0, demat={"ITBEES-EQ": [10, 10, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")  # day 1: TradeLab buys for her account
    mummy = sim.account("CD202")
    bought = mummy.net("ITBEES-EQ", "CNC")
    assert bought > 5
    # Day 2: TradeLab's units are settling (in neither positions nor the demat yet, the T+1 gap the code allows for).
    _age_buys(mummy_id, _trading_days_back(1))
    mummy.clear_day_positions()
    mummy.place_order("nse_cm", "CNC", "0", "MKT", "5", "DAY", "ITBEES-EQ", "S")  # she sells 5 of her own 10
    trading.true_up_all()  # the 15:40 true-up
    written = [a.quantity for a in _logs(status="adjusted")]
    # Day 3: TradeLab's units arrive; Sell sells what TradeLab bought.
    mummy.demat["ITBEES-EQ"][0] += bought
    mummy.demat["ITBEES-EQ"][1] += bought
    _age_all(mummy_id, timedelta(days=1))
    mummy.clear_day_positions()
    _, run = _run(client, owner, "holding", "sell")
    left = mummy.demat["ITBEES-EQ"][0]
    assert (written, left) == ([], 5), (
        f"TradeLab wrote off {written} of its {bought} units and left her {left} (her own: 5); run: {run.summary}")
```

(helpers as in `test_kotak_replies_end_to_end.py`; `_age_all` = `_age_buys` for every record of the account.)

Smallest fix: remember how many of the holder's own units Kotak showed at the last true-up before today (per key:
`max(0, total - records)` whenever `shown >= records`, e.g. in a small collection written by `_shrink_to_broker`),
and in the previous-day branch attribute today's outside sales to those units first:
`outside = max(0, day_sold - sold - own_seen_before_today)`. Yesterday's 15:40 true-up saw her 10 own units (total
29 = 19 bought today + 10 in the demat, records 19), so her sale of 5 writes nothing off; with no own units seen
(the other case) the rule behaves as now. A simpler alternative without new state — treat demat units beyond
TradeLab's older units as the holder's while prev units settle — gets the other case wrong for a day.
