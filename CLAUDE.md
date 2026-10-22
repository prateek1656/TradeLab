# TradeLab

Private app that trades many Kotak Neo family accounts at once: FastAPI + MongoDB backend in `backend/`,
React PWA in `frontend/`, deployed with Docker and Caddy (`docs/DEPLOY.md`).

- **Read `docs/HANDOFF.md` first.** It says what is in progress and what to do next; keep it up to date.
- Trading logic must fail safe: a Kotak reply TradeLab can't be sure of stops the trade and says why, never
  read as empty, refused or filled. Test trading changes through the real adapter with `backend/tests/kotak_sim.py`.
- Run `cd backend && pytest -q` (needs a local MongoDB replica set, see `docs/HANDOFF.md`) before committing.
- Work on `claude/intraday-trading-app-ci0hhp`, commit and push before ending a turn, never rewrite history.
