# C — F&O (round 4)

Auditor C, round 4. Confirmed findings come with failing tests through the real adapter (`kotak_sim`); the scratch file
`backend/tests/review_C_scratch_test.py` is deleted at the end. Helpers are those of
`backend/tests/test_kotak_replies_end_to_end.py` (`_add`, `_add_contract`, `_run`, `_hedged_pair`, `_filtered_sell`,
`_blank_tags`, `_day_after`, `_placed`) and `conftest` fixtures.

## Checked so far

- Read the diff `f360223..HEAD` for `backend/app` (tag_proofs / `_note_tags`, `_look_alike`, `unresolved_closes`,
  `unresolved_entries`, `closes_proved`, `_hold_hedges`, `_delivery_sizes`, write-down lock, `_earlier_closes`).
- Round-3 C findings: C3-1 (filtered Exit of the hedge beside an unproven buy-back) is fixed (`closes_proved`); C3-3 / C3-4 /
  C3-5 fixed; **C3-2 (stale sold record holds the hedge) is still there** (finding 4 below).
- Unknown close of an earlier day on a carried NRML contract that went through, no holder in the contract: written down, nothing
  sent (existing MCX test; same code for NSE).
- Unknown close that never reached Kotak, yesterday: closed once today (same).

## Findings (most severe first)

### 1. CONFIRMED (high) — one unknown Buy of an earlier day, once Kotak no longer lists the contract, makes every Exit preview and the panic button crash for the whole account / section (KeyError)

**Severity: high.** The panic button then does **not close any F&O (or Commodity / Holding) position of that account**; the
Exit screen (`/preview`) answers 500 for every account of the section; the run reports "failed" (loud, not silent), but the
positions stay open until the owner closes them in the Kotak Neo app.

**Scenario.** A Buy order of day 1 gets no clear reply (gateway page) and stays UNKNOWN (never found in the book: no tag
echo and a look-alike order in the book, or the day-end reconcile never read the book, or the app was down). `reconcile_orders`
only looks at today's rows, so the row is UNKNOWN forever. `build_sell_plan` keeps the key for later days "only while Kotak lists
it": `gone = {k for k in old if k not in broker_view(...).listed}`. Once the key is gone (the order never reached Kotak, the
owner closed the position by hand as the message says, or the contract expired), the key stays in `keys` but is in no ledger:
`before = {k: ledger[k] for k in keys}` raises KeyError, the `except` handler then evaluates `ledger[k]` again (also KeyError),
and the exception leaves `prepare` through `ex.map`.

**Where.** `trading.py` `build_sell_plan.prepare` l.3453-3481: `keys = [k for k in keys if k not in unknown_buys or k in gone]`
keeps a key that is not in `ledger`; l.3475 `before = {k: ledger[k] ...}` and l.3481 `order(acc, k, abs(ledger[k]) ...)`.
(Same-day rows are never "old", so only the next days hit it.)

**Test (fails today: KeyError in preview; second test: panic leaves the NIFTY future open).**
```python
def test_unknown_buy_yesterday_key_gone_today(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts = item["trading_symbol"]
    acct = sim.account("AB101")
    database()["tag_proofs"].delete_many({})
    acct.faults["place_order"] = [GATEWAY_PAGE]          # the order never reached Kotak
    _blank_tags(acct)                                    # (no tag echo)
    acct.orders.append({"nOrdNo": "778", "ordSt": "cancelled", "trdSym": ts, "exSeg": "nse_fo", "prod": "NRML",
                        "trnsTp": "B", "qty": 1, "fldQty": 0, "avgPrc": "0.00", "prcTp": "MKT", "rejRsn": "--",
                        "GuiOrdId": "", "ordDtTm": "22-Oct-2026 10:00:00"})   # a look-alike: absence can't be proved
    _run(client, owner, "fno", "buy")
    trading.reconcile_orders(client.get("/api/auth/me", headers=owner).json()["id"])   # row stays 'unknown'
    _day_after(acct, clock)
    _run(client, owner, "fno", "sell")                   # KeyError: Lot(... 'NIFTY26OCTFUT' ...) in build_sell_plan
```
Panic variant: two contracts in the Buy list (NIFTY future filled, BANKNIFTY future's order gets the gateway page and the
look-alike), next day `POST /api/automation/square-off-all`: log "Selling fno contracts failed", run "failed 0 orders placed",
`acct.net(nifty) == 65` still open.

**Smallest fix.** After computing `gone`, drop keys that have no ledger entry: `keys = [k for k in keys if k in ledger]`
(a gone unknown-buy key with no records is simply nothing to close), and make the `except` branch use `ledger.get(k, 0)`.
Also let `reconcile_orders` / the nightly job finish old UNKNOWN rows whose key Kotak no longer lists (mark them
`final`, "Not placed or closed since"), so they stop turning up every day.

### 2. CONFIRMED (medium, needs an unresolved close; likely only where Kotak's book doesn't echo the tag, or the book wasn't read before midnight) — yesterday's unknown close that went through is closed again today against the holder's own lot of the same contract

**Severity: medium** (a family member's own contract is sold; the rule "never touch the holder's contracts").
**Scenario.** The holder has her own lot in the same NRML contract (Kotak pools it: 2 lots). TradeLab's Exit sends its close,
the reply is lost, the order went through (net 1 lot = the holder's). The row stays UNKNOWN (book without tag echo + the order
itself as untagged look-alike, or the day's reconcile never read the book). The next day `tradelab_book` counts an UNKNOWN
close of an earlier day as 0 ("not closed"), so the record says TradeLab still holds 1 lot; Kotak shows 1 lot (hers):
`shown >= ours`, nothing is written down, `qty = min(mine, broker) = 1 lot` and Exit **sells the holder's lot** ("done 1 order").
The same-day case is protected (`unresolved_closes`); the next-day case is not, and there is no holder note for F&O as there is for
Holding. Existing tests only use a different symbol for the holder's lot.

**Where.** `tradelab_book` l.2817-2822 (`qty = min(qty, filled or 0)` for an old UNKNOWN close) together with
`unresolved_closes` (today's rows only, l.3024-3035) and `_shrink_to_broker` (`shown >= abs(ours)` → keep).

**Test (fails: net 0, the holder's lot is gone).**
```python
def test_unknown_fno_close_yesterday_beside_holders_same_contract(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "B")   # the holder's own lot
    _run(client, owner, "fno", "buy")
    database()["tag_proofs"].delete_many({})
    acct.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]
    _blank_tags(acct)
    _run(client, owner, "fno", "sell")
    acct.faults.clear()
    trading.reconcile_orders(client.get("/api/auth/me", headers=owner).json()["id"])   # row 'unknown', net == lot
    _day_after(acct, clock)
    _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == lot, "the holder's own lot was sold"
```
**Smallest fix.** Treat an earlier day's UNKNOWN close like an earlier day's unknown entry: keep the key reported ("an earlier
close of this got no reply … nothing is sent, check the Kotak Neo app") for as long as Kotak lists the key; drop it when
Kotak no longer lists it (then the write-down brings the record to 0). Costs a manual look in the rare "close never reached
Kotak" case, never touches the holder's lot.

### 3. CONFIRMED (low) — a close that was accepted but whose book state was never read before midnight holds the hedge forever on a filtered Exit

`closes_proved` counts a non-final close as 0. A short bought back yesterday (success, `final` False, `filled` None because the
book was unreadable all day) is "still open" for `stays_open` for ever (the nightly job and `reconcile_orders` only look at
today's rows; the main ledger counts the close in full, so nothing writes the short down). A filtered Exit of the hedge
(`symbols=[hedge]` / `opened="B"`) answers "Not sold: the sold leg it hedges stays open" every day while Kotak shows no short.
Fail-safe; an unfiltered Exit sells the hedge. Test: `_hedged_pair`, `acct.faults["order_report"] = GATEWAY_PAGE`,
`_filtered_sell(symbols=[s_ts])`, clear fault, `_day_after`, `_filtered_sell(symbols=[h_ts])` → hedge still 65.
**Fix.** In `stays_open` drop sold keys of earlier days that Kotak no longer lists (or let the 15:40 job settle old
non-final close rows as final with the quantity as sent).

### 4. CONFIRMED (low, repeat of C3-2, not fixed) — a stale sold record blocks a filtered Exit of the hedge

Unchanged since round 3: a short closed in the Kotak app (or expired) stays in `stays_open` (`tradelab_open(closes_proved=True)` of
the whole section, never shrunk to Kotak's figure), so `opened="B"` / `symbols=[hedge]` answers "the sold leg stays open" until the
15:40 true-up writes the record down. Weekly short + monthly hedge: after the weekly expiry the hedge cannot be exited alone until the
true-up. Fail-safe. Test and fix as in round 3 finding 2 (run `_shrink_to_broker(write=False)` over the sold keys before holding).

## Notes (not findings, live checks)

- The proof of absence for an F&O close without tag echo rests on Kotak's order book spelling the contract exactly like TradeLab's
  `trading_symbol` (prefix tolerance only). If `trdSym` of `nse_fo` orders were spelled differently (the positions code already
  guards that case with `respelled`), an order that went through would be taken for "Not placed". The live 1-lot test must check
  `trdSym`/`exSeg`/`GuiOrdId` of a TradeLab F&O order in the order book (HANDOFF lists the tag echo; add `trdSym`).
