# B — Holding (CNC) — round 5 (draft, being extended)

Auditor B. Scratch test `backend/tests/review_B_scratch_test.py` (deleted at the end; the tests are copied below).
Clock-consistent tests: `patch_clock(monkeypatch, clock)` from the randomized harness is applied, so records, notes
and the 15:40 / 23:58 true-ups all see the same days (the e2e tests' `_age_buys` mixes the patched and the real clock,
which made `tradelab_book`'s "earlier day" test never see aged rows).

## Findings so far

### B5-1 — CONFIRMED — MEDIUM: an old unknown CNC sale that DID go through is repeated on her units two days later
### B5-2 — CONFIRMED — MEDIUM: her units still in the T+1 gap when the note is taken are missing from it
