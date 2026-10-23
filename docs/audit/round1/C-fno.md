# C — F&O (round 1) — partial, written by the main session after the auditor stopped at the usage limit

Checked so far: the auditor was reading `build_sell_plan` and the panic / per-account square-off paths when it
stopped (23 Oct, ~08:30 UTC).

## Lead (being confirmed): one account's "Square off" may close TradeLab's contracts in other accounts

`build_sell_plan` reads the ledger for every account (`tradelab_open(user_id, section, None, ...)`) and ignores
`account_ids`, which `run_exit(..., account_ids=[...], section=ALL)` passes for the Accounts screen's per-account
square-off. The auditor's scratch test (kept below) checks that squaring off Acc A leaves Acc B's NIFTY FUT open.

```python
"""Review C scratch tests (F&O). Deleted before the review ends."""

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import kotak_payload

import kotak_sim
from app.broker import kotak
from app.broker.manager import broker_manager
from app.config import IST
from app.db import session_scope
from app.models import ExecutionRun, OrderLog
from app.services import trading
from kotak_sim import KotakSim

IN_SESSION = datetime(2026, 10, 14, 10, 0, tzinfo=IST)  # a Wednesday


@pytest.fixture
def sim(monkeypatch):
    s = KotakSim()
    broker_manager.kotak_factory = s.factory
    monkeypatch.setattr(kotak, "time", SimpleNamespace(time=time.time, sleep=lambda _s: None))
    kotak_sim.PRICES.clear()
    kotak_sim.TOKEN_PRICES.clear()
    return s


def _add(client, owner, sim, name, ucc, **acct):
    sim.account(ucc, **acct)
    r = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait(run_id: int, timeout: float = 15) -> ExecutionRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_scope() as db:
            run = db.get(ExecutionRun, run_id)
            if run.status != "running":
                db.expunge(run)
                return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


def _command(client, owner, section, action, symbols=None, opened=None):
    params = {"action": action} | ({"symbols": symbols} if symbols else {}) | ({"opened": opened} if opened else {})
    pv = client.get(f"/api/sections/{section}/preview", params=params, headers=owner)
    assert pv.status_code == 200, pv.text
    body = {"fingerprint": pv.json()["fingerprint"], "confirm_id": pv.json()["confirm_id"], "symbols": symbols}
    if opened:
        body["opened"] = opened
    r = client.post(f"/api/sections/{section}/{action}", json=body, headers=owner)
    return pv.json(), r


def _run(client, owner, section, action, symbols=None, opened=None):
    pv, r = _command(client, owner, section, action, symbols, opened)
    assert r.status_code == 200, r.text
    return pv, _wait(r.json()["run_id"])


def _preview(client, owner, section, action):
    pv = client.get(f"/api/sections/{section}/preview", params={"action": action}, headers=owner)
    assert pv.status_code == 200, pv.text
    return pv.json()


def _add_contract(client, owner, section, query, side="B", lots=1):
    c = client.get("/api/stocks/contracts", params={"section": section, "q": query}, headers=owner).json()["results"][0]
    r = client.post("/api/basket", json={"section": section, "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "side": side, "sizing_mode": "lots",
                                         "sizing_value": lots}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _logs(**flt):
    with session_scope() as db:
        return db.find(OrderLog, flt, sort=[("id", 1)], track=False)


def _user(client, owner):
    return client.get("/api/auth/me", headers=owner).json()["id"]


# ----------------------------------------------------------------------------- 1. one account's square-off


def test_one_accounts_square_off_closes_contracts_only_in_that_account(client, owner, clock, sim):
    """The Accounts screen's "Square off" on Acc A must not close TradeLab's contracts in Acc B."""
    clock.set(IN_SESSION)
    a = _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    _, run = _run(client, owner, "fno", "buy")
    assert run.status == "done", run.summary
    acc_a, acc_b = sim.account("AB101"), sim.account("CD202")
    assert acc_a.net(ts, "NRML") == lot and acc_b.net(ts, "NRML") == lot

    r = client.post(f"/api/accounts/{a}/square-off", headers=owner)
    assert r.status_code == 200, r.text
    run = _wait(r.json()["run_id"])
    assert acc_a.net(ts, "NRML") == 0, run.summary
    # Acc B was not asked to be squared off: its contract stays open.
    assert acc_b.net(ts, "NRML") == lot, run.summary
```
