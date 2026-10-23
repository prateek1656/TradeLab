# Work in progress

Where the work stands, so any session can pick it up. Update this file whenever a step finishes.

Last updated: 23 Oct 2026. Branch: `claude/intraday-trading-app-ci0hhp` (the default and only branch).

## The current task: no trading bug may reach real money

On 22 Oct 2026 a live Holding **Sell** failed in one account. Its demat was otherwise empty, so Kotak answered
the holdings call with *"No holdings found for this client &lt;UCC&gt;"*, and TradeLab took that for an error.
The owner asked for the whole trading side (Intraday, Holding, F&O, Commodity) to be made and tested so that
nothing like it can happen again.

| Commit | What it did |
| --- | --- |
| `11ad513` | First fix: "No holdings found" reads as an empty demat |
| `b0bc437` | Every Kotak reply read strictly: only Kotak's documented "No Data" (stCode 5203) and explicit "no … found" answers mean empty; gateway pages, unknown layouts and replies without a balance raise; an order reply with neither an order number nor a Kotak refusal is unknown and looked up in the order book; a cancel counts only when Kotak confirms it. Added the simulated Kotak SDK `backend/tests/kotak_sim.py` and `backend/tests/test_kotak_replies_end_to_end.py`, which run every section through the real adapter |
| `d216b48` | Review #1 fixes: client codes inside messages no longer read as words; a part-filled order is never counted twice; square-off waits until another working order on a position (a stop-loss) is confirmed cancelled; undecodable replies and 5xx error envelopes are unknown, not refusals |
| `f36d97b` | Review #2 fixes: working orders count by what Kotak's book shows filled (`OrderLog.filled`); an order the book never showed blocks Exit; benign "no … found" phrasings accepted; session words count inside other words; a failed stop-loss cancel is remembered across square-off runs |
| `7ebd566` | Review #3 fixes: the panic button closes only what the book confirmed is TradeLab's (never by Kotak's figure, which could be the holder's own contract) and reports the rest; delivery settlement guard by quantity (previous trading day's buys, and today's buys today's positions don't show yet); failed-cancel memory by segment/symbol/product; order replies with a 5xx code are looked up, never resent (only stCode 1003/403 or the SDK's pre-send "2fa" refusal renew the login and resend); rejected-after-part-fill keeps the filled units; Sell's "Yes" survives a fill arriving after the screen; only this account's UCC is taken off "nothing found" messages; no write-down beside any unfinished TradeLab order |
| `6a24b40` | Review #4 fixes: a key is never written below Kotak's figure plus TradeLab's own fills of today that today's positions don't show yet (`Position.day_buy_qty / day_sell_qty` from flBuyQty / flSellQty), and units kept that way are reported as "not closed" errors (never "done" or "nothing left"); previous-day delivery units are protected only beyond today's sales outside TradeLab; previews (Buy and Sell screens) never write records; a close that ends short (part filled then rejected or cancelled) or is still working when TradeLab stops following it gets a failed entry for the rest, so the run is partial and the exit pop-up names the account; delivery units Kotak shows but won't let be sold (not settled, pledged) are reported |
| `503b6e3` | Review #5 fixes: after cancelling TradeLab's own working orders, Kotak's book must show each stopped (looked at up to CANCEL_CHECKS times) or a failed cancel is reported, so Exit / panic are never "done" while an order may still fill; the Exit screen and positions list count a working close by what filled, so "close again" is possible (runs still count it in full until the book shows it stopped); the settling allowance never covers units Kotak already shows in the demat |
| `c595e6c` | Review #6 fixes: the settling allowance counts as "already in the demat" only demat units beyond TradeLab's own older units (two days of buys of the same ETF no longer lose day 2's settling units); cancel checks look at every account's order book at once |
| `8bd6ec7` | Review #7 fix: while previous-day delivery units may be settling, only today's sales outside TradeLab are written off (capped by those units), from the records as they stood before today's write-offs, so repeated looks never write off more |

Tests: 542 pass (`cd backend && pytest -q`). Each new end-to-end test fails on the code before its fix.

## Next steps

1. Review rounds: #3 found 4 problems, #4 three, #5 two, #6 one, #7 one (both in the delivery settling rule,
   now rewritten in `8bd6ec7` to be simple and idempotent). Next: a narrow review #8 of `8bd6ec7`; stop once a round
   finds nothing real.
   Known and accepted (rare): a working order whose final state was never read before the book cleared
   overnight keeps its last seen fill; when the holder trades the same contract the same day, a lagging fill
   of TradeLab's can still be hidden by the holder's own trades; if the 15:40 / 23:58 true-ups didn't run on
   the day the holder sold TradeLab's delivery units, the next day may keep them until the day after.
2. Done 23 Oct (rev 41): the user manual's Part 18 lists the new messages. Keep it in step if messages change:
   "Kotak's order book hasn't confirmed TradeLab's last order for this yet, so nothing is sent for it…",
   "Only what Kotak's order book has confirmed", "Only N filled; the rest was rejected by the broker: …",
   "Not closed: another order on this position (a stop-loss, for example) is still working…",
   "Kotak's positions don't show N of TradeLab's units here yet…", "N units TradeLab bought on the previous
   trading day don't show at Kotak yet (still settling)…", "N of TradeLab's units here show at Kotak but can't
   be sold yet…", "Only N of M closed (…). R still open…", "Still working at Kotak (N of M filled so far)…".
3. Tell the owner the outcome, in plain words, and what they must do:
   - update the server in the evening or at the weekend (never 08:55–15:30 on a trading day):
     `cd ~/tradelab && ~/tradelab-backup.sh && git pull && docker compose up -d --build`;
   - the small account still holds the ETF units the failed sale left behind: sell them with TradeLab after
     the update, or in the Kotak Neo app.
4. Remind the owner what only a real 1-share / 1-lot test can prove: the first next-day Holding sale (how Kotak
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
