# B — Holding (CNC) — round 3 (in progress)

Auditor B. Scratch test file `backend/tests/review_B_scratch_test.py` (deleted at the end; test code of each confirmed
finding is copied below; helpers as in `test_kotak_replies_end_to_end.py`: `_add`, `_add_etf`, `_run`, `_age_buys`,
`_trading_days_back`; `_back_notes(back)` = move every `holder_units` doc's `seen_at` back by `back`).

## Checked so far

- B3 frozen `holder_units` rule over multi-day sequences: 120 seeded random multi-day scenarios (5 days + 4 settle
  days, with and without a clearing holiday; TradeLab buys, holder buys/sells, arrivals in the afternoon, true-ups at
  random times) against an ownership oracle (a holder's sale takes her own visible units first). TradeLab never
  forgot a unit (no under-claim) in any; the only deviations are the rare over-claim in B-R3-2 below.
- `_units_doubt`: returns None for CNC on its first line; `_lot_of` / `_contract_changed` / odd-lot check also
  skip CNC. It never reaches Holding.
- Positions `settling` list: right for the simple case; it cannot list units that the holder's own units in the
  demat cover (`have` counts all demat units), display only. Frontend renders it also when `rows` is empty.
- Two-trading-day window with write-offs/true-ups: units truly sold outside TradeLab on day 2 are written off the
  same day (outside sales rule); with no true-up that day they are kept one day longer and Sell reports them as
  "still settling" (the accepted case), never sold. A wider window can only protect more; no loss found.
- Partial fills at Sell time (close part fills, stays working): the second Sell cancels it first, sells only the
  rest, records and Positions right (own 30 + TradeLab 19 case too). Pledged rows at Sell time (sellable < TradeLab's
  units): sells the sellable part, reports the rest "can't be sold yet", records keep the rest, a later Sell after the
  release finishes, the holder's own units stay. Fine (except B-R3-1: the units sold may be hers).

## Findings

### B-R3-1 — CONFIRMED — the morning-after Sell sells the HOLDER'S OWN units of the same ETF while TradeLab's are settling (or not yet sellable)

Severity: HIGH (core rule: a family member's own shares are never touched). Up to min(TradeLab's units, holder's own
units) of her own ETF are sold in her account, in every account whose holder owns the same ETF (Mummy Ji type:
hundreds of units). TradeLab's units arrive later and sit in her demat, but TradeLab's records were reduced by the
sale, so they are now forgotten (shown as hers) for good; the run says "partial" with a "still settling" row only.
Premise: Kotak's holdings do not show/sell TradeLab's T+1 units yet (the very case the settling rule, B1-B5, is
built for; the open live-test question in HANDOFF step 4). It happens both when the T+1 units are absent from the
holdings and when they are listed with `sellableQuantity` not including them.

Scenario: Mummy Ji owns 10 ITBEES. Day 1 Buy now buys 19 for her account. Day 2, 10:00, Sell: positions are cleared,
TradeLab's 19 are settling, the demat shows her own 10. `build_sell_plan` sizes `qty = min(ours 19, Kotak's sellable
10) = 10` and sells her 10; the other 9 get the "still settling" error row.

Where: `backend/app/services/trading.py` `build_sell_plan.prepare` (`mine, broker = ledger[k], have.get(k, 0)`;
`qty = min(mine, max(broker, 0))`, ~line 3263-3264 at HEAD) with `have = view.held` (= positions + sellable demat of
the symbol, all owners). `_shrink_to_broker` keeps the records (settling) but Sell then spends the holder's units on
them. The holder's own figure is already stored (`holder_units`, `_holders_own_before_today`).

Test (fails today: `Sell sold [10] of her own 10 while TradeLab's 19 were settling`):

```python
def test_sell_next_morning_sells_holders_own_units_while_tradelab_settle(client, owner, clock, sim):
    clock.set(IN_SESSION)
    mid = _add(client, owner, sim, "Mummy Ji", "CD202", cash=2000.0, demat={"ITBEES-EQ": [10, 10, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")            # day 1
    m = sim.account("CD202")
    bought = m.net("ITBEES-EQ", "CNC")
    assert bought > 10
    trading.true_up_all()                            # 15:40 day 1: sees her own 10
    back = _trading_days_back(1)
    _age_buys(mid, back); _back_notes(back)
    m.clear_day_positions()                          # day 2 morning: TradeLab's units are settling (not shown)
    pv, run = _run(client, owner, "holding", "sell")
    sold = [o["qty"] for o in m.sold()]
    assert (sold, m.demat["ITBEES-EQ"][0]) == ([], 10)
```

Second variant (units listed but not sellable): same set-up, `m.settle_overnight(); m.demat["ITBEES-EQ"][1] = 10`
instead of `clear_day_positions()`: also sells 10.

Smallest fix: in `prepare`, for a CNC long key whose units may still be settling or not sellable
(`_previous_day_delivery_buys(...)` has the symbol, or `view.total[k] > view.held[k]`), size
`qty = min(mine, max(broker - own, 0))` with `own = _holders_own_before_today(k)`; when there is no `holder_units`
note at all and `broker > 0` while TradeLab's units settle, send nothing and report "can't tell which of the N units
Kotak shows are TradeLab's while its units settle". Today's own buys in positions can be left out of `broker` via
`view.day_bought - TradeLab's fills today`. Previews show the same quantity.

### B-R3-2 — CONFIRMED (random scenarios), LOW — the holder's own units that arrive after TradeLab's loss are counted as TradeLab's

Severity: LOW, needs the holder to sell TradeLab's units (beyond her own visible units) while a purchase of her own
is still settling. Then the write-down goes to Kotak's total, which includes her arriving units; TradeLab's records
over-claim by up to what she bought, and a later Sell sells those units of hers.

Scenario (seed 34 of my model; 6 of 220 seeded multi-day scenarios, all with this shape, none with a forgotten unit):
Mummy Ji, TradeLab holds ~950 ITBEES in her account. Day 3 she buys 13 ITBEES herself (settle on day 4); day 4 morning
she sells 71 (only TradeLab's units are visible, so they are what she sold); her 13 arrive in the afternoon. The
15:40 true-up sees 24, writes TradeLab's records down to 24 (TradeLab really holds 11); Sell later sells 24.
The day-3 look already shows the cause: today's positions list her 13 bought units (`view.day_bought` minus TradeLab's
own fills today) and `shown` counts them as TradeLab's.

Where: `_shrink_to_broker` (`shown = max(side * broker, 0)`, `broker = view.total`), `broker_view`.

Smallest fix (partial): leave the holder's own buys of today out of `shown`
(`shown -= max(0, view.day_bought[key] - TradeLab's bought today)` for CNC), and add her purchases of the last
settling days (`day_bought` seen by the true-up minus TradeLab's fills) to `holder_units` as "own, arriving". Honestly
an inherent ambiguity of Kotak's totals; fine to accept.

## Final report

1. B-R3-1 (HIGH, CONFIRMED): the morning-after Sell sells the holder's own units of the same ETF while TradeLab's are
   settling / not sellable (qty = min(ours, all sellable units of the symbol)).
2. B-R3-2 (LOW, CONFIRMED by seeded scenarios): holder's late-arriving own units mask TradeLab's loss.

New round-2 code otherwise holds: B3's freeze (120 + 100 seeded scenarios incl. clearing holidays; TradeLab never
forgot a unit), the `settling` list (display only), the two-day window with write-offs/true-ups, `_units_doubt`
(returns None for CNC), partial Sell fills and pledged rows at Sell time.
