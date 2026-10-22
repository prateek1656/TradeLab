# Work in progress

Where the work stands, so any session can pick it up. Update this file whenever a step finishes.

Last updated: 22 Oct 2026. Branch: `claude/intraday-trading-app-ci0hhp` (the default and only branch).

## The current task: no trading bug may reach real money

On 22 Oct 2026 a live Holding **Sell** failed in one account. Its demat was otherwise empty, so Kotak answered
the holdings call with *"No holdings found for this client &lt;UCC&gt;"*, and TradeLab took that for an error.
The owner asked for the whole trading side (Intraday, Holding, F&O, Commodity) to be made and tested so that
nothing like it can happen again.

| Commit | What it did |
| --- | --- |
| `11ad513` | First fix: "No holdings found" reads as an empty demat |
| `b0bc437` | Every Kotak reply read strictly: only Kotak's documented "No Data" (stCode 5203) and explicit "no … found" answers mean empty; gateway pages, unknown layouts and replies without a balance raise; an order reply with neither an order number nor a Kotak refusal is unknown and looked up in the order book; a cancel counts only when Kotak confirms it. Added the simulated Kotak SDK `backend/tests/kotak_sim.py` and `backend/tests/test_kotak_replies_end_to_end.py`, which run every section through the real adapter |
| `d216b48` | Fixes from independent review #1: client codes inside messages (IP3K7, 12FA9) no longer read as words; a part-filled order is never counted twice (records stay put while a TradeLab order is unfinished; Sell/Exit cancels it first and sends nothing if it can't); square-off waits until another working order on a position (a stop-loss) is confirmed cancelled; undecodable replies and 5xx error envelopes are unknown, not refusals; delivery units bought in the last 4 days aren't written off in the T+1 gap |

Tests: 511 pass (`cd backend && pytest -q`). The new end-to-end tests fail on the code before each fix.

## Next steps

1. **Review #2 of `d216b48` is applied** in the commit after `6689566` (see `git log`): working orders count by
   what Kotak's book shows filled (`OrderLog.filled`); only a TradeLab order the book never showed blocks Exit
   (the panic button then closes by what Kotak shows); a trailing client code is the only part taken off messages,
   and benign "no … found" phrasings are accepted; session words count inside other words; delivery buys are
   protected only since the previous trading day and when today's positions have no row; a failed stop-loss
   cancel is remembered across square-off runs. 511 tests pass; each new test fails on `d216b48`.
   **Review #3 of `f36d97b` found (in progress, fix each with a `kotak_sim` test):**
   1. Panic fallback min(records, Kotak) for an unconfirmed order can close the holder's own carried contract
      (book unreadable, TradeLab's order rejected). Fix: the panic closes only the confirmed part and alerts.
   2. Delivery settlement guard is by symbol: if the symbol trades again on day 2, Monday's T1 units are written
      off. Fix: by quantity: never bring records below Kotak's figure + units bought since the previous trading day.
   3. Square-off memory of failed cancels is by bare symbol (a CNC/NRML cancel failure blocks the MIS close while
      the book is unreadable). Fix: log segment/product on cancels, match the full key.
   4. An order reply with a 5xx code and "session" in its text raised SessionExpired, so the manager logged in again
      and re-sent the order (double order). Fix: for orders, 5xx/unclear first; only stCode 1003/403 or the SDK's
      pre-send "2fa" refusal is a session error.
   5. Minor: rejected-after-part-fill forgets the filled units (keep them); Sell fingerprint changes as a working
      order fills (leave working fills out); trailing-word strip ("… for this account temporarily" read as empty:
      strip only this account's UCC); true-up beside working orders (skip shrinking keys with any unfinished
      order). Not fixed (rare, noted): a working order whose final state was never read before the book cleared
      overnight keeps its last seen fill.
2. Tell the owner the outcome, in plain words, and what they must do:
   - update the server in the evening or at the weekend (never 08:55–15:30 on a trading day):
     `cd ~/tradelab && ~/tradelab-backup.sh && git pull && docker compose up -d --build`;
   - the small account still holds the ETF units the failed sale left behind: sell them with TradeLab after
     the update, or in the Kotak Neo app.
3. Remind the owner what only a real 1-share / 1-lot test can prove: the first next-day Holding sale (how Kotak
   shows T1 units in its holdings), the quantity of the first MCX order, one sell-first F&O trade and its Exit,
   and the first 14:30 square-off.

## Rules for this work

- Trading logic fails safe: a Kotak reply TradeLab can't be sure of stops that account's trade and says why.
  It is never read as "empty", "refused" or "filled".
- Test trading changes through the real adapter with `kotak_sim` (Kotak's own reply formats, empty accounts,
  faults), not only with the `FakeKotak` stand-in in `conftest.py`.
- The owner is very sensitive to bugs and to session usage: work lean, verify before claiming, and commit and
  push before ending a turn. Never rewrite git history.

## Running the tests in a fresh container

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -c constraints.txt
# MongoDB replica set on 127.0.0.1:27017, e.g. with Docker:
docker run -d --name mongo -p 27017:27017 mongo:8.0 --replSet rs0 --bind_ip_all
docker exec mongo mongosh --quiet --eval "rs.initiate({_id: 'rs0', members: [{_id: 0, host: '127.0.0.1:27017'}]})"
pytest -q
```

## Related material

- User manual (Claude Doc, owned by the owner): https://claude.ai/code/artifact/16c7a9b6-71b8-4d2e-8d71-f067263ba372
  Part 18 lists TradeLab's messages; keep it in step when messages change.
- Deployment guides: `docs/DEPLOY.md`, `docs/DEPLOY-HOSTINGER.md`.
