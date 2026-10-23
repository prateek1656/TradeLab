# F — Cross-cutting (round 1) — in progress

## Checked so far

- Read end to end: `trading.py` (launch, `_exit_lock`, `_wait_for_entries`, `_stopped_at`, `_use_confirmation`,
  arm / cancel / claim / firing, run_entry, run_exit incl. ALL, square_off_account, `_cancel_own_open_orders`,
  `_confirm_cancels`, build_entry_plan `ready_by`, build_sell_plan, fire_plan, recover_after_restart),
  `scheduler.py` (jobs, misfire grace, catch-ups, warm-up, retries), `broker/manager.py`, `broker/kotak.py`,
  `db.py` (session / claim semantics), routers (automation, sections, accounts, basket, alerts, holidays, auth),
  frontend panic button / api wrapper (a lost reply leaves the dialog open: "tap again").
- Sound so far: two taps on Buy / Sell with the same confirm id; Buy and Sell of one on-command section never
  overlap; Sell / panic / square-off serialize on `_exit_lock`; Let's Trade claim ("armed" -> "running" ->
  "firing") vs cancel / panic; the panic waits for a buy already firing; restart recovery (running runs ->
  failed "Interrupted", pending orders -> unknown, the catch-up square-off runs again); the SDK bounds every HTTP
  call at 30 s (httpx default), so a hung Kotak call can't hold `_exit_lock` for ever.
- Already fixed while this audit ran: `c384daf` (found by C) — the per-account "Square off" closed TradeLab's
  F&O / Commodity contracts in every account. F's own test of it passes on the current code.
- Re-checked after `f31496a` (A1 cancel watching, C2, C3 stray orders): F-1 and F-2 below still fail on the
  current code (line numbers below are for the code after `f31496a`).

## Findings

### F-1. Two square-offs back to back close the same units twice while Kotak's positions lag — CONFIRMED

Severity: high. A whole intraday position is sold (or bought back) a second time, so the account ends up with
the opposite position of the same size (e.g. 10 shares bought -> 10 sold short; on an MIS future, a short
future with unlimited risk) until someone closes it by hand or Kotak's 15:05 auto square-off does (with its
charges).

Scenario: the panic button's reply is lost ("Can't reach the TradeLab server" — the dialog stays open and says
nothing was placed twice), so it is tapped again; or "Square off now" is tapped at 14:30 while the scheduled
square-off starts; or two devices. `launch` never refuses an Intraday / ALL exit, so the two runs queue on
`_exit_lock` and the second starts the moment the first ends. Kotak's positions show the first run's close a
little late (its order book shows it complete at once — the lag the code already guards against inside one
run). The first run, after one VERIFY_DELAY (2 s), sees its close complete in the book and stops sending, but
its final check still sees the old position and reports "Position still open after square-off attempts —
check the Kotak Neo app". The second run starts with no memory of that close (`first` / `sent` are local to
one `square_off_account` call), reads the same stale position and sends a second close: the position flips.
The test shows run 1: sell 10 placed + "Position still open…"; run 2: sell 10 placed + "This position is now
-10 — on the other side…". A positions lag of just over VERIFY_DELAY (2.5 s here, with the production 2 s
delay) is enough, because the second run reads within milliseconds of the first run's last look.

Where: `backend/app/services/trading.py:1888-1889` (per-run memory only), `:1968-1969` (what's left to close is
judged only against this run's closes), `:2024` (the misleading "still open" message for a close Kotak's book
shows complete), `:2233` (runs serialize but share nothing), `:3363` (exits are never refused, so repeats queue).

Test (scratch, fails on the current code: net −10, two sells of 10):

```python
def lag_positions(acct: SimAccount, lag: float) -> None:
    """Kotak's positions show a fill only `lag` seconds after it happened; its order book shows it at once."""
    state = {"until": 0.0, "reply": None}
    real_place = acct.place_order

    def place_order(*a, **kw):
        before = SimAccount.positions(acct)
        res = real_place(*a, **kw)
        if time.monotonic() >= state["until"]:
            state["until"], state["reply"] = time.monotonic() + lag, before
        return res

    def positions(**kw):
        if time.monotonic() < state["until"]:
            acct.calls.append(("positions", kw))
            return copy.deepcopy(state["reply"])
        return SimAccount.positions(acct, **kw)

    acct.place_order = place_order
    acct.positions = positions


def test_F2_panic_tapped_twice_never_closes_twice_while_positions_lag(client, owner, clock, sim, monkeypatch):
    monkeypatch.setattr(trading, "VERIFY_DELAY", 2.0)  # production value
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")  # an intraday position
    lag_positions(acct, 2.5)
    first = client.post("/api/automation/square-off-all", headers=owner).json()["run_id"]
    time.sleep(0.05)
    second = client.post("/api/automation/square-off-all", headers=owner).json()["run_id"]
    runs = [_wait(first), _wait(second)]
    sells = [(o["trnsTp"], o["qty"]) for o in acct.orders if o["GuiOrdId"].startswith("TL")]
    assert acct.net("SBIN-EQ", "MIS") == 0, (sells, [(r.status, r.summary) for r in runs])
    assert sells == [("S", 10)], sells
```

Smallest fix: keep each account's square-off memory (`first`, `sent`) across runs for the day (a module-level
dict keyed by account id, under a lock; `square_off_account` starts from it and saves it back), so a run that
follows another treats the earlier closes like its own: while Kotak's book shows them filled and the positions
still show the old figure, nothing more is sent (`left = min(net, first - filled)` = 0). Drop a key's memory once
the positions have shown it flat (a new position the holder opens later is then a fresh one). And in the final
check, when the book shows this position's closes filled, say "Kotak's order book shows it closed; its positions
don't show that yet — check again in a moment" instead of "Position still open", which invites another tap.

### F-2. The square-off / panic closes MIS positions next to a stop-loss it never saw when the order book can't be read — CONFIRMED

Severity: medium–high. The holder's stop-loss (set in the Kotak Neo app on TradeLab's intraday position, a common
habit) stays working beside a now-flat position; when the price reaches it, it sells again and the account is
short the whole quantity (or long, for a short position) with nobody watching, until Kotak's 15:05 auto
square-off closes it with charges. On an MIS future the new position has unlimited risk.

Scenario: at the panic (or the 14:30 square-off, or "Square off now"), Kotak's order report fails (a gateway page,
5xx — the failure the code already treats as normal elsewhere) while its positions still read. `square_off_account`
fills `held_back` (positions with another order working on them) only from an order book it has read; with the
book unreadable at the first attempt, `held_back` is empty and `remembered()` only covers positions whose cancel
failed in an earlier run today, so every position is closed at once. The stop-loss is never cancelled. The run is
"partial", but its only messages are "Could not read orders: …" — nothing says a close went out beside an order
that couldn't be checked, and the Kotak app shows the position flat. The rule the square-off follows when the book
IS read ("Closing next to an order still working would flip the position when it fills", review #1) is skipped
exactly when TradeLab knows least. (C3's `_stray_orders` covers only the NRML closes of Exit / panic, and only by
reporting.)

Where: `backend/app/services/trading.py:1902-1903` (`remembered` only for earlier failed cancels), `:1914-1921`
(`held_back` is set only from a readable book), `:1961` (the hold-back check that lets the close through).

Test (scratch, fails on the current code: a TL close of 10 is sent while the stop-loss is "trigger pending";
messages: "Order placed", 2 x "Could not read orders: …", "Could not read orders to cancel TradeLab's working
orders: …"):

```python
def test_F3_panic_never_closes_next_to_a_working_order_it_could_not_check(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")  # an intraday position
    sl = acct.add_open_order("SBIN-EQ", "nse_cm", "MIS", "S", 10)  # the holder's stop-loss on it
    acct.faults["order_report"] = GATEWAY_PAGE  # the order book can't be read during the run
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    closes = [o for o in acct.orders if o["GuiOrdId"].startswith("TL")]
    assert acct.order(sl)["ordSt"] == "trigger pending"
    assert closes == [], ([(o["trnsTp"], o["qty"]) for o in closes], run.status, run.summary)
```

Smallest fix: until this run has read the order book, hold back every position (`remembered()` returns True for
all keys while `not book_read`), keep retrying, and if the book never reads, report each position as "Not closed:
Kotak's order book couldn't be read, so an order on this position (a stop-loss, for example) couldn't be checked or
cancelled, and closing beside it could open the opposite position. Square off again, or close it in the Kotak Neo
app." The automatic retry (every minute until 15:00) and Kotak's own 15:05 square-off still close it, so nothing is
left open for the night. (If the owner prefers closing anyway, the minimum is a per-position failure saying a
close went out beside an order that couldn't be checked, like C3's message for NRML.)
