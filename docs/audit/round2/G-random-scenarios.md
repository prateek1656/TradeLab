# G — Randomized scenario test (round 2) — in progress

Harness: `backend/tests/wip_random_scenarios.py` (built from scratch in round 2; the round-1 file was never saved).
Run: `cd backend && .venv/bin/python -m pytest -q -p no:cacheprovider tests/wip_random_scenarios.py`
More seeds: `TL_FUZZ_SEEDS=2000 .venv/bin/python -m pytest -q -p no:cacheprovider tests/wip_random_scenarios.py -k fuzz`

## Status log
- Started: read audit_common/audit_G, round-1 design, HANDOFF, trading.py (entry, exit, sell plan, shrink), kotak_sim,
  end-to-end tests. Writing the harness.

## Findings
(none yet)
