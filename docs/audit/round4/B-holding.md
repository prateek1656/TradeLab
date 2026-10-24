# B — Holding (CNC) — round 4

Auditor B. Scratch test (deleted at the end; code copied below). Helpers as in `test_kotak_replies_end_to_end.py`
(`_add`, `_add_etf`, `_run`, `_age_buys`, `_back_notes`, `_trading_days_back`, `_holder_with_tradelabs_units_settling`).

## Checked so far
- `_delivery_sizes`, `_note_holders_sale`, `_note_holders_own`, `_holders_own_noted` read end to end.
- `_note_holders_sale` is idempotent across repeated looks (reads the frozen `own_before`, writes `own = own_before - her`).
  Mis-attribution of TradeLab's own sales to her: `_fills_today` counts only `success` rows (full quantity when final or
  filled unknown), so ordinary sells, part fills and rejected-after-part-fill are all subtracted correctly. Only a
  TradeLab sell still `unknown` at 15:40 is mis-read as hers (needs a lost reply the book never resolves; LOW, not reported).
- "nothing sent without a holder note": the note is written at the first 15:40 / 23:58 true-up after the first Buy. It is
  missing only if both true-ups skipped the key (key unfinished, session down). A missing note blocks the Sell while any
  buy of the symbol is inside the settling window (2 trading days), and for a daily buyer for ever (see B-R4-3, LOW).
- Unknown close not counted as closed from the next day, CNC: tomorrow's Sell is sized by `_delivery_sizes`; if the sale
  really went through and her pool is bigger than TradeLab's records it could be repeated, but this needs a close whose
  outcome Kotak's book never settles (tag echo proves absence otherwise): not reported, same as accepted G-2 trade-off.
- `unresolved_entries` on CNC: Buy ignores it (only `sp.lots`); Sell drops the key while Kotak lists it; unknown rows are
  proved failed by reconcile whenever tags are echoed, so it blocks only when they never are. Accepted G-2 behaviour.
- `_writedown_lock`: held around `tradelab_open` + `unfinished_keys` + insert only, no nested lock, no deadlock path;
  the holder-note writes are outside it but idempotent. Fine.

## Findings

### B-R4-1 — CONFIRMED — HIGH (availability + wrong message, safe direction): a holder with PLEDGED units of the same ETF can never have TradeLab's units sold

Severity: TradeLab's units of that ETF are stuck in that account for ever (Sell, Exit and the panic button's Holding part
send nothing), while the screen says "show at Kotak but can't be sold yet (not settled, or pledged)". Family members often
pledge liquid ETFs as collateral; the first live pledged holder hits it. No money is sold wrongly, but a position can't be closed in a crash.

Cause: `_delivery_sizes` (trading.py ~3157) hides `own` = the holder's units noted by `_note_holders_own(key, shown - ours)`,
where `shown` is `view.total` (ALL demat units, pledged too), but subtracts it from `view.held` (SELLABLE units only):
`sizes = max(held - own, 0)`. Pledged own units are in `own` yet not in `held`, so they are taken off twice. The
condition `view.total > sizes` that switches the hiding on is then true for as long as anything is pledged, so it never ends
(also 6 trading days later).

Test (fails: `0 == 19`):
```python
def _pledged_holder(client, owner, sim, pledged_all=True):
    mid = _add(client, owner, sim, "Mummy Ji", "CD202", cash=2000.0, demat={"ITBEES-EQ": [100, 0 if pledged_all else 40, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    m = sim.account("CD202"); bought = m.net("ITBEES-EQ", "CNC")
    trading.true_up_all()
    back = _trading_days_back(1); _age_buys(mid, back); _back_notes(back)
    m.settle_overnight()   # TL's units arrive, sellable
    return m, bought

@pytest.mark.parametrize("pledged_all", [True, False])
def test_pledged_holder_units_block_tradelabs_sale(client, owner, clock, sim, pledged_all):
    clock.set(IN_SESSION)
    m, bought = _pledged_holder(client, owner, sim, pledged_all)
    pv, run = _run(client, owner, "holding", "sell")
    assert sum(o["qty"] for o in m.sold()) == bought      # 0 == 19 in both cases
```
(With `_trading_days_back(6)` instead of 1 it still sells 0.)

Smallest fix: note the holder's FREE units too (`own_free = max(view.held - ours, 0)` at the same true-up, kept with the same
frozen-before-today rule and lowered by `_note_holders_sale`) and subtract `own_free`, not `own`, from `view.held` in
`_delivery_sizes`; hiding should also not start merely because `view.total > held` when nothing of TradeLab's can be settling
(then `held` already excludes the pledged units, and TradeLab's units are sellable).

### B-R4-2 — CONFIRMED — MEDIUM: the holder's own purchases of the same ETF are taken for TradeLab's while TradeLab's units settle (sells her units)

Severity: up to the units she bought herself are sold from her account. Needs her to buy the ETF herself on a day TradeLab's
units are settling (T1) and TradeLab to Sell that morning, or TradeLab to buy on consecutive days (the note is not refreshed
while an earlier buy is in the settling window, so her purchases in between are never noted).

(a) same morning: TradeLab's day-1 units settling; she buys 5 herself today; positions show her 5 as today's net, so
`held = 5 + 10 = 15`, `own = 10` noted, `sizes = 5` -> Sell sells her 5.
(b) streak: Buy day 1 (note 10), Buy day 2 (no refresh: earlier buys present), she buys 10 on day 2; day 3 morning her 10
arrive, TradeLab's day-2 units still settle: `held = 49`, noted own 10 -> sells 39 of 49, 10 of them hers.

```python
def test_T1_holder_buys_own_same_day_while_tradelab_settle(client, owner, clock, sim):
    clock.set(IN_SESSION)
    m, bought = _holder_with_tradelabs_units_settling(client, owner, sim)
    m.clear_day_positions()
    m.place_order("nse_cm", "CNC", "0", "MKT", "5", "DAY", "ITBEES-EQ", "B")   # she buys 5 herself this morning
    pv, run = _run(client, owner, "holding", "sell")
    assert [o["qty"] for o in m.sold()] == []          # fails: [5]

def test_T2_streak_stale_note(client, owner, clock, sim):
    clock.set(IN_SESSION)
    mid = _add(client, owner, sim, "Mummy Ji", "CD202", cash=3000.0, demat={"ITBEES-EQ": [10, 10, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy"); m = sim.account("CD202")
    trading.true_up_all()
    back = _trading_days_back(1); _age_buys(mid, back); _back_notes(back)
    m.settle_overnight(); m.cash = 3000.0
    _run(client, owner, "holding", "buy"); b2 = m.net("ITBEES-EQ", "CNC")
    m.place_order("nse_cm", "CNC", "0", "MKT", "10", "DAY", "ITBEES-EQ", "B")  # her own purchase on day 2
    trading.true_up_all()
    back2 = _trading_days_back(1); _age_buys(mid, back2); _back_notes(back2)
    m.settle_overnight(); h = m.demat["ITBEES-EQ"]; h[0] -= b2; h[1] -= b2     # TL's day-2 units still settle
    _run(client, owner, "holding", "sell")
    assert m.demat["ITBEES-EQ"][0] >= 20       # fails: 10 (sold 39)
```

Smallest fix: (a) in `_delivery_sizes` subtract today's units that are not TradeLab's: `max(view.day_bought - TradeLab's fills
today, 0)` from `sizes`; (b) at the true-up, in the "earlier buys present" branch also add her purchases of today to the
note (`_note_holders_buy`: `own += max(view.day_bought - tradelab_bought_today, 0)`, the mirror of `_note_holders_sale`).

### B-R4-3 — PLAUSIBLE — LOW (safe direction): a missing holder note blocks Sell for as long as TradeLab keeps buying the ETF

If the first Buy's 15:40 and 23:58 true-ups both skipped the key (unfinished order, session down), or for a position that
existed before this release, `_holders_own_noted` is None. While any buy of the symbol is inside the 2-day settling window
(`earlier`), the "earlier" branch of `_shrink_to_broker` never writes a note, so `_delivery_sizes` returns `unknown` and
sends nothing even when all of TradeLab's units are plainly in the demat. A daily buyer never leaves the window. Pre-existing
positions bought 2 trading days before the update are blocked for one more day. Fix: when `own is None` and the symbol's
older buys are all settled (the true-up sees `shown >= ours` with every earlier buy older than T+1), write the note.
