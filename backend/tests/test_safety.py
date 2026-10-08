"""Edge cases from the safety review: exactly what was confirmed trades, and nothing gets stuck."""

import threading
import time
from datetime import timedelta

import pytest
from conftest import MARKET_MORNING, FakeKotak, confirm, fake_for, kotak_payload, signup
from sqlalchemy import select

from app.db import session_scope
from app.models import ExecutionRun, Holiday, TradeSession
from app.services import trading

YESTERDAY = (MARKET_MORNING - timedelta(days=1)).timestamp()


@pytest.fixture(autouse=True)
def quick_recheck(monkeypatch):
    monkeypatch.setattr(trading, "MARKET_RECHECK", timedelta(milliseconds=200))


def _me(client, h):
    return client.get("/api/auth/me", headers=h).json()["id"]


def _account(client, h, name="Main", ucc="UCC1", **extra):
    acc = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=h).json()["id"]
    if extra:
        client.patch(f"/api/accounts/{acc}", json=extra, headers=h)
    return acc


def _stock(client, h, sym="AAA", **extra):
    r = client.post("/api/basket", json={"symbol": sym, "trading_symbol": f"{sym}-EQ", "token": "1", **extra},
                    headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _state(client, h):
    return client.get("/api/automation/status", headers=h).json()["lets_trade"]


def _yes(client, h, st):
    return client.post("/api/automation/lets-trade",
                       json={"trade_date": st["trade_date"], "fingerprint": st["fingerprint"]}, headers=h)


def _wait_idle(user_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not trading._entry_busy(user_id):
            return
        time.sleep(0.05)
    raise AssertionError("entry still running")


# --- only the confirmed accounts --------------------------------------------------------


def test_account_added_after_confirming_is_not_traded(client, owner):
    a = _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)
    b = _account(client, owner, "Second", "UCC2")  # autopilot is on for new accounts
    assert _state(client, owner)["session"]["changed"] is True  # the app asks to confirm again

    trading.run_entry(_me(client, owner), "scheduled")
    assert len(fake_for(a).placed) == 1 and fake_for(b).placed == []


def test_account_switched_on_after_confirming_is_not_traded(client, owner):
    a = _account(client, owner)
    b = _account(client, owner, "Second", "UCC2", automation_enabled=False)
    _stock(client, owner)
    confirm(client, owner)
    client.patch(f"/api/accounts/{b}", json={"automation_enabled": True}, headers=owner)
    assert _state(client, owner)["session"]["changed"] is True
    trading.run_entry(_me(client, owner), "scheduled")
    assert len(fake_for(a).placed) == 1 and fake_for(b).placed == []


def test_account_switched_off_after_confirming_is_left_out(client, owner):
    a = _account(client, owner)
    b = _account(client, owner, "Second", "UCC2")
    _stock(client, owner)
    confirm(client, owner)
    client.patch(f"/api/accounts/{b}", json={"automation_enabled": False}, headers=owner)
    trading.run_entry(_me(client, owner), "scheduled")
    assert len(fake_for(a).placed) == 1 and fake_for(b).placed == []


# --- "Yes" confirms exactly what the dialog showed ----------------------------------------


def test_yes_is_refused_if_the_list_changed_after_the_dialog_opened(client, owner):
    a = _account(client, owner)
    _stock(client, owner)
    shown = _state(client, owner)
    _stock(client, owner, "BBB")  # e.g. from another device
    r = _yes(client, owner, shown)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert _state(client, owner)["session"] is None
    assert _yes(client, owner, _state(client, owner)).status_code == 200
    trading.run_entry(_me(client, owner), "scheduled")
    assert sorted(p[2] for p in fake_for(a).placed) == ["AAA-EQ", "BBB-EQ"]


def test_an_evening_dialog_cannot_buy_now_the_next_morning(client, owner, clock):
    a = _account(client, owner)
    _stock(client, owner)
    clock.set(MARKET_MORNING.replace(day=13, hour=15))
    shown = _state(client, owner)  # "Trade on Wed 14 Oct at 09:15"
    assert shown["trade_date"] == "2026-10-14" and shown["buys_now"] is False
    clock.set(MARKET_MORNING.replace(hour=11))
    r = _yes(client, owner, shown)
    assert r.status_code == 409
    assert fake_for(a).placed == []


# --- live "market closed" check on every entry ---------------------------------------------


def test_buy_now_on_a_closed_day_sends_nothing(client, owner, clock):
    a = _account(client, owner)
    _stock(client, owner)
    FakeKotak.quote_time = YESTERDAY  # big stocks haven't traded today
    clock.set(MARKET_MORNING.replace(hour=10))
    st = _state(client, owner)
    assert st["buys_now"] is True
    assert _yes(client, owner, st).status_code == 200
    _wait_idle(_me(client, owner))
    assert fake_for(a).placed == []
    with session_scope() as db:
        assert db.get(Holiday, MARKET_MORNING.date()).source == "detected"
        assert db.scalar(select(TradeSession.status)) == "skipped"


def test_confirming_in_the_last_minute_still_checks_the_market(client, owner, clock):
    a = _account(client, owner)
    _stock(client, owner)
    FakeKotak.quote_time = YESTERDAY
    clock.set(MARKET_MORNING.replace(hour=9, minute=14, second=59, microsecond=500000))
    assert _yes(client, owner, _state(client, owner)).status_code == 200
    time.sleep(1.0)
    _wait_idle(_me(client, owner))
    assert fake_for(a).placed == []


def test_holiday_added_while_preparing_stops_the_orders(client, owner):
    a = _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)
    t = threading.Thread(target=trading.run_entry, args=(_me(client, owner), "scheduled"),
                         kwargs={"fire_at": trading.now_ist() + timedelta(seconds=1)})
    t.start()
    time.sleep(0.3)
    assert client.post("/api/holidays", json={"day": "2026-10-14", "description": "Closed"},
                       headers=owner).status_code == 201
    t.join(5)
    assert fake_for(a).placed == []


def test_two_users_detecting_a_closed_market_together_both_skip(client):
    users = []
    for i in range(4):
        h = {"Authorization": f"Bearer {signup(client, f'u{i}@example.com', f'U{i}').json()['token']}"}
        _account(client, h, f"A{i}", f"UCC{i}")
        _stock(client, h)
        confirm(client, h)
        users.append(_me(client, h))
    FakeKotak.quote_time = YESTERDAY
    fire_at = trading.now_ist() + timedelta(milliseconds=300)
    ts = [threading.Thread(target=trading.run_entry, args=(u, "scheduled"), kwargs={"fire_at": fire_at})
          for u in users]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    with session_scope() as db:
        assert [s.status for s in db.scalars(select(TradeSession))] == ["skipped"] * 4


# --- nothing stays "Confirmed" when it can no longer happen --------------------------------


def test_reconfirming_while_the_cancelled_run_finishes_is_refused_cleanly(client, owner, clock):
    _account(client, owner)
    _stock(client, owner)
    user = _me(client, owner)
    confirm(client, owner)
    clock.set(MARKET_MORNING.replace(hour=9, minute=14))
    t = threading.Thread(target=trading.run_entry, args=(user, "scheduled"),
                         kwargs={"fire_at": trading.now_ist() + timedelta(seconds=1)})
    t.start()
    time.sleep(0.3)
    assert client.post("/api/automation/lets-trade/cancel", headers=owner).status_code == 200
    r = _yes(client, owner, _state(client, owner))
    assert r.status_code == 409 and "still finishing" in r.json()["detail"]
    t.join(5)
    assert _state(client, owner)["session"]["status"] == "cancelled"  # not a dead "Confirmed"
    r = _yes(client, owner, _state(client, owner))  # once it's done, confirming again buys now
    assert r.status_code == 200 and r.json()["run_id"] is not None


def test_a_missed_confirmation_says_so_and_can_be_confirmed_again(client, owner, clock):
    a = _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)  # 09:00 for 09:15, but the 09:14 job never ran (e.g. server restarting)
    clock.set(MARKET_MORNING.replace(hour=9, minute=30))
    st = _state(client, owner)
    assert st["session"]["status"] == "missed" and "didn't run" in st["session"]["note"]
    r = _yes(client, owner, st)
    assert r.status_code == 200 and r.json()["run_id"] is not None
    _wait_idle(_me(client, owner))
    assert len(fake_for(a).placed) == 1


def test_moving_the_buy_time_into_the_past_shows_missed(client, owner, clock):
    _account(client, owner)
    _stock(client, owner)
    client.put("/api/automation/settings", json={"entry_time": "10:00:00"}, headers=owner)
    confirm(client, owner)
    clock.set(MARKET_MORNING.replace(hour=9, minute=30))
    client.put("/api/automation/settings", json={"entry_time": "09:20:00"}, headers=owner)
    assert _state(client, owner)["session"]["status"] == "missed"


def test_restart_recovery_unblocks_square_off_and_ends_half_done_days(client, owner, clock):
    a = _account(client, owner)
    _stock(client, owner)
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    assert len(fake_for(a).placed) == 1
    # The server died in the middle of the 14:30 square-off.
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    dead = trading._start_run(user, "exit", "scheduled")
    trading.recover_after_restart()
    with session_scope() as db:
        assert (db.get(ExecutionRun, dead).status, db.get(ExecutionRun, dead).summary) == ("failed", trading.INTERRUPTED)

    clock.set(MARKET_MORNING.replace(hour=14, minute=33))
    assert trading.run_exit(user, "catchup") is not None  # catch-up isn't blocked by the dead run
    assert fake_for(a).positions_map[("AAA-EQ", "MIS")].net_qty == 0
    assert client.post("/api/automation/square-off-all", headers=owner).status_code == 200  # not "in progress"


def test_restart_during_preparation_ends_the_session(client, owner):
    _account(client, owner)
    _stock(client, owner)
    user = _me(client, owner)
    confirm(client, owner)
    assert trading._claim_session(user, MARKET_MORNING.date()) is not None  # then the process died
    trading.recover_after_restart()
    st = _state(client, owner)["session"]
    assert st["status"] == "skipped" and "restarted" in st["note"]


# --- square-off ----------------------------------------------------------------------------


def test_switching_autopilot_off_after_the_buy_still_squares_off(client, owner, clock):
    a = _account(client, owner)
    _stock(client, owner)
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    client.patch(f"/api/accounts/{a}", json={"automation_enabled": False}, headers=owner)
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is not None
    assert fake_for(a).positions_map[("AAA-EQ", "MIS")].net_qty == 0


# --- accounts and shared settings ----------------------------------------------------------


def test_deleting_an_account_frees_stocks_limited_to_it(client, owner):
    a = _account(client, owner)
    b = _account(client, owner, "Second", "UCC2")
    item = _stock(client, owner, account_ids=[b])
    fake_for(b).funds_val = 0
    assert client.delete(f"/api/accounts/{b}", headers=owner).status_code == 204
    c = _account(client, owner, "Third", "UCC3")
    assert c != b  # ids are never reused
    stock = next(i for i in client.get("/api/basket", headers=owner).json() if i["id"] == item)
    assert stock["active"] is False and not stock["account_ids"]
    assert fake_for(a).placed == []


def test_only_the_owner_can_change_market_holidays(client, owner):
    assert client.get("/api/auth/me", headers=owner).json()["is_owner"] is True
    other = {"Authorization": f"Bearer {signup(client, 'friend@example.com').json()['token']}"}
    assert client.get("/api/auth/me", headers=other).json()["is_owner"] is False
    assert client.post("/api/holidays", json={"day": "2026-10-14", "description": "x"},
                       headers=other).status_code == 403
    assert client.post("/api/holidays/sync", headers=other).status_code == 403
    assert client.post("/api/holidays", json={"day": "2026-10-14", "description": "x"},
                       headers=owner).status_code == 201
    assert client.delete("/api/holidays/2026-10-14", headers=other).status_code == 403
    assert client.get("/api/holidays", headers=other).status_code == 200


def test_short_stocks_are_listed_for_the_dialog(client, owner):
    _account(client, owner)
    _stock(client, owner, "AAA")
    _stock(client, owner, "BBB", side="S")
    st = _state(client, owner)
    assert st["stocks"] == ["AAA", "BBB"] and st["shorts"] == ["BBB"]
