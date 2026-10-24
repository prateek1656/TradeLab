# Work in progress

Where the work stands, so any session can pick it up. Update this file whenever a step finishes.

Last updated: 25 Oct 2026. Branch: `claude/great-hopper-8kg8ra` (this session's branch; it carries all work since 06deceb).

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
| `9d6ebe5` | Review #8 fix: no write-downs between 00:00 and 09:00 IST (`NIGHT_ENDS`): until Kotak's start of day it may still show yesterday's trades while TradeLab's day has turned (a late 23:58 true-up). The simulator no longer counts a delivery sale twice when the symbol is bought later the same day |

Tests: 571 pass (`cd backend && python -m pytest -q`; a bare `pytest` can't import `app`). Each new end-to-end test fails on the code before its fix.

## Next steps

1. **Broad audit, round 1 (started 23 Oct; the owner: effort is no object, there must be no trading bug).**
   Rounds #3–#8 reviewed each latest diff (4, 3, 2, 1, 1, 1 findings, all fixed through `9d6ebe5`). Round 1 of the
   broad audit = six independent area audits (A Intraday, B Holding, C F&O, D Commodity, E orders and Kotak
   replies, F cross-cutting) + a randomized scenario test (G). **How to resume in any session:**
   - The exact instructions for each agent are in `docs/audit/PROMPTS.md`; findings land in
     `docs/audit/round1/<letter>-<name>.md`. An area is done when its file holds a final report.
   - On 23 Oct the session ended (owner's weekly limit) before any area wrote a final report: restart all six
     audits and G with their prompts after fixing the open findings.
   - Any area without a final report: start a fresh agent with its prompt (agents of an earlier session can't be
     resumed). On 23 Oct all seven stopped at the owner's usage limit with little saved; they were resumed in
     the same session at ~10:00 UTC.
   - Then fix every confirmed finding (a `kotak_sim` test that fails before the fix, full suite, commit, push),
     and start round 2 the same way. Stop when a full round finds nothing real.
   - Fixed so far in round 1: `c384daf` C1 (one account's "Square off" closed contracts in every account);
     `f31496a` A1 (the square-off took an accepted cancel as done), C2 (a Buy beside an unconfirmed TradeLab order
     could close the holder's lot), C3 (Exit / panic "done" beside a stop-loss that can open a new position).
   - Fixed after f31496a: E1, E2 (`97122db`, checked with the full suite on 23 Oct), and in the next commit F-1 (square-off
     memory per account per day, `_squared`: a second square-off never closes the same units twice while positions
     lag), C4 (`OrderLog.lot_size / expiry` + `_contract_changed`: an adjusted contract after a bonus / split is
     never closed short or written off; Exit says to close it in the Kotak Neo app), B1 (`holder_units`
     collection: the holder's own units seen before today absorb her sales first while TradeLab's units settle).
   - Fixed on 24 Oct, each with tests:
     - F-2 (`00fa3e7`): square-off, panic and "Square off now" hold back every intraday position until the order
       book has been read; if it never reads, each position is reported "Not closed: Kotak's order book couldn't
       be read…". The trade-off: the position stays open until the book reads or Kotak's own 15:05 auto square-off.
     - C5 (`d23f902`): Exit and the panic button close a hedged F&O/MCX account in two waves, buy-backs of sold
       legs first; a hedge whose sold leg wasn't fully bought back is not sold ("Not sold: the sold leg it hedges
       couldn't be bought back first…").
     - B2 (`4b03488`): NSE clearing holidays are downloaded daily at 08:35 and at startup; units bought before a
       clearing holiday count as still settling. If the list is missing, the last two trading days are protected.
   - Round 2 of the broad audit started 24 Oct (areas A–F + G); findings in `docs/audit/round2/`.

   **Work of 24–25 Oct:**
   - Round 2 (findings in `docs/audit/round2/`) found about 20 confirmed issues. All are fixed (merges `fe472fd`, `56cde50`, `cb5e4ec`, `4787f46`, `4227a36`, `f360223`). Highlights:
     - Holding never takes the holder's own units while TradeLab's settle, and settling units show on Positions.
     - The automatic square-off survives a database blip, respects the switch, and never runs after 15:00.
     - Orders left "pending" by a crash are looked up.
     - The square-off remembers earlier closes after a restart.
     - Buy sends a hedge before its sold leg; filtered Exit never sells a hedge alone.
     - The panic button's F&O/Commodity closes don't wait for other accounts.
     - MCX replies TradeLab can't be sure of (lots vs units, tag echo, spelling) stop and report.
   - Round 3 (`docs/audit/round3/`) found 3 serious issues and a few small ones. All are fixed (merges `d15329e`, `5805f10`, `635ab81`, `f3a3474` and the A3-1 merge):
     - A close with no reply from Kotak can no longer make Exit say "done" while the position is open. It blocks that day, and from the next day it counts as not closed until Kotak's figures settle it.
     - Sell no longer sells the holder's own ETF units while TradeLab's settle.
     - Square-off after a restart closes partly closed or reopened positions correctly.
     - The panic button no longer cancels its own close.
     - A square-off missed because the server was down gets a pop-up, and old failed pop-ups clear.
   - Randomized scenario test (`backend/tests/test_random_scenarios.py`, notes in `docs/audit/round2/G-random-scenarios.md`) found 4 issues (G-1, G-2, G-4, G-5). All are fixed in `eddbe8a`. Run more seeds with the environment variables described in the test file's header.
   - Round 4 started 25 Oct (`docs/audit/round4/`), including fuzz seed 350 (records too high after a holder takes TradeLab's side on MCX).
   - Tests: 697 pass, 2 skipped (`cd backend && python -m pytest -q --ignore=tests/wip_random_scenarios.py`).
   - The audits' scratch tests (`backend/tests/review_*_scratch_test.py`) are collected by a plain `python -m pytest -q`
     while they exist; they are deleted when each audit ends.
   Known and accepted (rare): a working order whose final state was never read before the book cleared
   overnight keeps its last seen fill; when the holder trades the same contract the same day, a lagging fill
   of TradeLab's can still be hidden by the holder's own trades; if the 15:40 / 23:58 true-ups didn't run on
   the day the holder sold TradeLab's delivery units, the next day may keep them until the day after.
2. Done 23 Oct (rev 41): the user manual's Part 18 lists the new messages. Keep it in step if messages change.
   Part 18 still needs three changes for the 24 Oct fixes (the owner's Claude Doc can't be edited from this
   session): add the two new "Not closed" / "Not sold" messages below, and change "N units TradeLab bought on the
   previous trading day" to "a recent trading day". The messages:
   "Kotak's order book hasn't confirmed TradeLab's last order for this yet, so nothing is sent for it…",
   "Only what Kotak's order book has confirmed", "Only N filled; the rest was rejected by the broker: …",
   "Not closed: another order on this position (a stop-loss, for example) is still working…",
   "Kotak's positions don't show N of TradeLab's units here yet…", "N units TradeLab bought on a recent
   trading day don't show at Kotak yet (still settling)…", "N of TradeLab's units here show at Kotak but can't
   be sold yet…", "Only N of M closed (…). R still open…", "Still working at Kotak (N of M filled so far)…",
   "Not closed: Kotak's order book couldn't be read, so an order on this position…", "Not sold: the sold leg it
   hedges couldn't be bought back first…".
   Rounds 2–4 added or changed many messages. The exact texts are in the round 2 and 3 fix commits (`git log 7632274..HEAD`). Examples, shortened:
   - "Not sold: its hedge couldn't be bought first…"
   - "Not sold: the sold leg it hedges stays open…"
   - "An earlier close of this (sent at HH:MM) got no reply from Kotak…"
   - "An earlier buy of this … got no reply from Kotak…"
   - "Can't tell which of the N units Kotak shows are TradeLab's while its units of this settle…"
   - "Still settling, not at Kotak yet…"
   - "Kotak shows N here but TradeLab opened M (… units to a lot)…"
   - "Not closed: N units is not a whole number of lots…"
   - "The square-off didn't run before 15:00: TradeLab was not running…"
   - "Not closed yet: part of this position was closed earlier today…"
3. Tell the owner the outcome, in plain words, and what they must do:
   - update the server in the evening or at the weekend (never 08:55–15:30 on a trading day):
     `cd ~/tradelab && ~/tradelab-backup.sh && git pull && docker compose up -d --build`;
   - the small account still holds the ETF units the failed sale left behind: sell them with TradeLab after
     the update, or in the Kotak Neo app.
4. Remind the owner what only a real 1-share / 1-lot test can prove: the first next-day Holding sale (how Kotak
   shows T1 units in its holdings), the quantity of the first MCX order, one sell-first F&O trade and its Exit,
   and the first 14:30 square-off. Also: does Kotak's holdings `quantity` drop on the day a delivery unit is sold
   from the demat? `broker_view` counts delivery as max(today's net, 0) + holdings quantity; if the quantity
   drops at once and the same symbol is then bought the same day, a sale is counted twice (TradeLab then
   forgets units, never sells extra). Choose the formula once the live test shows it.
   - MCX, with one CRUDEOILM future (lot 10), not GOLD. Do a read-only step first: buy 1 lot by hand in the Kotak app, then let TradeLab read positions and the order book. Check that positions show units (10), not lots (1); that the order book shows `GuiOrdId = TL<id>` for `mcx_fo`; and that `exSeg` is `mcx_fo` and `trdSym` equals the contract list's `pTrdSymbol`. Full checklist: `docs/audit/round3/D-commodity.md`.
   - F&O: Kotak accepts an NRML option market order (TradeLab sends market protection "0"), and the order book echoes the TL tag for `nse_fo`.

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
scripts/dev_mongo.sh          # MongoDB replica set rs0 on 127.0.0.1:27017, no Docker needed (conda-forge package)
python -m pytest -q
```

With Docker instead: `docker run -d --name mongo -p 27017:27017 mongo:8.0 --replSet rs0 --bind_ip_all`, then
`docker exec mongo mongosh --quiet --eval "rs.initiate({_id: 'rs0', members: [{_id: 0, host: '127.0.0.1:27017'}]})"`.

## Related material

- User manual (Claude Doc, owned by the owner): https://claude.ai/code/artifact/16c7a9b6-71b8-4d2e-8d71-f067263ba372
  Part 18 lists TradeLab's messages; keep it in step when messages change.
- Deployment guides: `docs/DEPLOY.md`, `docs/DEPLOY-HOSTINGER.md`.
