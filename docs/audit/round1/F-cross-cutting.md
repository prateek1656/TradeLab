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

Where: `backend/app/services/trading.py:1823-1824` (per-run memory only), `:1892-1893` (what's left to close is
judged only against this run's closes), `:1942` (the misleading "still open" message for a close Kotak's book
shows complete), `:2151` (runs serialize but share nothing), `:3273` (exits are never refused, so repeats queue).

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
