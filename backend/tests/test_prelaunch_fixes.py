"""Regressions for the pre-launch audit: each test reproduces a finding and checks the fix."""

import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from conftest import MARKET_MORNING, FakeKotak, buy, confirm, fake_for, kotak_payload, lets_trade
from neo_api_client.exceptions import ApiException

from app.broker import kotak
from app.broker.base import BrokerError, LoginFailed, Order, OrderOutcomeUnknown, OrderResult, Position, SessionExpired
from app.broker.manager import broker_manager
from app.config import IST
from app.db import session_scope
from app.models import ExecutionRun, Holiday, OrderLog, TradeSession
from app.services import trading
from test_kotak_adapter import FakeNeo

IN_SESSION = datetime(2026, 10, 14, 10, 0, tzinfo=IST)  # a Wednesday


def _account(client, owner, name="Main", ucc="UCC1", funds=100000.0):
    r = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner)
    assert r.status_code == 201, r.text
    acc = r.json()["id"]
    fake_for(acc).funds_val = funds
    return acc


def _stock(client, owner, sym="AAA", token="1", **extra):
    r = client.post("/api/basket", json={"symbol": sym, "trading_symbol": f"{sym}-EQ", "token": token,
                                         "sizing_mode": "qty", "sizing_value": 10, **extra}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _me(client, owner) -> int:
    return client.get("/api/auth/me", headers=owner).json()["id"]


def _wait(run_id: int, timeout: float = 10) -> ExecutionRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_scope() as db:
            run = db.get(ExecutionRun, run_id)
            if run.status != "running":
                db.expunge(run)
                return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


def _logs(**flt):
    with session_scope() as db:
        return db.find(OrderLog, flt, sort=[("id", 1)], track=False)


# ----------------------------------------------------------------------------- the Kotak adapter


@pytest.fixture
def adapter():
    b = kotak.KotakNeoBroker.__new__(kotak.KotakNeoBroker)
    b._creds = {"consumer_key": "ck", "mobile": "9876543210", "ucc": "AB123", "mpin": "123456",
                "totp_secret": "JBSWY3DPEHPK3PXP"}
    b._client = FakeNeo()
    b._lock = threading.Lock()
    b._last_totp_step = None
    b.logged_in = False
    return b


@pytest.mark.parametrize("reply", [
    {"error": ApiException(status=0, reason="Request timeout after 30 seconds")},  # the SDK's timeout
    {"error": ApiException(status=0, reason="Server disconnected without sending a response")},
    {"error": ApiException(status=504, reason="Gateway Time-out")},
    {"Error": json.JSONDecodeError("Expecting value", "<html>502</html>", 0)},  # an error page, not JSON
])
def test_a_lost_order_reply_is_outcome_unknown_not_failed(adapter, reply):
    adapter._client.responses = {"place_order": reply}
    with pytest.raises(OrderOutcomeUnknown):
        adapter.place_market_order("nse_cm", "SBIN-EQ", "B", 1, "MIS", tag="TL7")
    assert adapter._client.calls[0][1]["tag"] == "TL7"  # sent along, to find the order later


@pytest.mark.parametrize("reply", [
    {"error": ApiException(status=0, reason="Unable to connect to server")},  # never left the machine
    {"error": ApiException(status=429, reason="Rate limit exceeded")},  # the SDK's own limiter
    {"Error": ValueError("Invalid product")},  # the SDK's own checks
    {"stat": "Not_Ok", "emsg": "Insufficient balance.", "stCode": 1004},  # Kotak said no
])
def test_orders_that_were_not_placed_are_plain_failures(adapter, reply):
    adapter._client.responses = {"place_order": reply}
    res = adapter.place_market_order("nse_cm", "SBIN-EQ", "B", 1, "MIS")
    assert res.ok is False and res.message


def test_ip_not_whitelisted_is_a_clear_refusal_not_an_expired_session(adapter):
    adapter._client.responses = {"place_order": {"stCode": 100008, "errMsg": "unauthorized", "stat": "Not_Ok"},
                                 "positions": {"stCode": 100008, "errMsg": "unauthorized", "stat": "Not_Ok"}}
    res = adapter.place_market_order("nse_cm", "SBIN-EQ", "B", 1, "MIS")
    assert res.ok is False and "isn't whitelisted" in res.message
    with pytest.raises(BrokerError, match="isn't whitelisted") as exc:
        adapter.positions()
    assert not isinstance(exc.value, SessionExpired)


def test_invalid_session_code_still_means_log_in_again(adapter):
    adapter._client.responses = {"positions": {"stat": "Not_Ok", "emsg": "Invalid session", "stCode": 1003}}
    with pytest.raises(SessionExpired):
        adapter.positions()


def test_order_book_rows_carry_the_tag(adapter):
    adapter._client.responses = {"order_report": {"stat": "Ok", "data": [
        {"nOrdNo": "1", "ordSt": "complete", "trdSym": "SBIN-EQ", "exSeg": "nse_cm", "prod": "MIS", "trnsTp": "B",
         "qty": "5", "fldQty": "5", "avgPrc": "800.5", "GuiOrdId": "TL42"},
        {"nOrdNo": "2", "ordSt": "open", "trdSym": "SBIN-EQ", "exSeg": "nse_cm", "prod": "MIS", "trnsTp": "B",
         "qty": "5", "fldQty": "0", "avgPrc": "0", "GuiOrdId": "--"},
    ]}}
    assert [o.tag for o in adapter.orders()] == ["TL42", None]


# ----------------------------------------------------------------------------- sessions


def test_parallel_expired_sessions_log_in_once(client, owner, monkeypatch):
    acc = _account(client, owner)
    fake = fake_for(acc) if acc in broker_manager._entries else None
    broker_manager.ensure_session(acc)
    fake = fake_for(acc)
    before = fake.logins
    expired = {"n": 0}
    gate = threading.Barrier(6)

    def positions():
        gate.wait(timeout=5)
        if fake.logins == before:  # every caller still holds the old session
            expired["n"] += 1
            raise SessionExpired("Invalid session, please re-login")
        return []

    fake.positions = positions
    threads = [threading.Thread(target=broker_manager.call, args=(acc, lambda b: b.positions())) for _ in range(6)]
    gate_threads = threads
    for t in gate_threads:
        t.start()
    for t in gate_threads:
        t.join(10)
    assert fake.logins - before == 1  # one login between them, not one each


def test_a_failed_login_is_not_retried_for_every_request(client, owner):
    acc = _account(client, owner)
    fake = fake_for(acc)
    fake.creds["mpin"] = "000000"  # the MPIN was changed in Kotak
    with pytest.raises(LoginFailed):
        broker_manager.ensure_session(acc, force=True)
    attempts = fake.logins
    for _ in range(5):
        with pytest.raises(LoginFailed):
            broker_manager.call(acc, lambda b: b.funds())
    assert fake.logins == attempts  # no new login attempts (each could wait 30 s, and lock the account)


# ----------------------------------------------------------------------------- outcome of orders


def test_an_order_not_sent_because_login_failed_is_failed_not_unknown(client, owner):
    acc = _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)
    fake = fake_for(acc)
    fake.expire_next_order = True
    fake.creds["mpin"] = "000000"  # the re-login after the expired session fails
    trading.run_entry(_me(client, owner), "manual")
    (row,) = _logs(action="buy")
    assert row.status == "failed" and row.message.startswith("Not sent:")
    # So nothing counts as traded: the 14:30 square-off leaves the holder's own trades alone.
    assert trading.accounts_traded_today(_me(client, owner), trading.now_ist().date()) == set()


def test_unknown_message_tells_the_truth_when_auto_square_off_is_off(client, owner):
    acc = _account(client, owner)
    _stock(client, owner)
    client.put("/api/automation/settings", json={"auto_exit": False}, headers=owner)
    fake = fake_for(acc)

    def lost(*a, **kw):
        raise OrderOutcomeUnknown("Placing order: Request timeout after 30 seconds")

    fake.place_market_order = lost
    buy(client, owner)
    (row,) = _logs(action="buy")
    assert row.status == "unknown" and "Automatic square-off is off" in row.message
    run = client.get("/api/automation/runs", headers=owner).json()[0]
    assert run["status"] == "partial" and "no reply from Kotak" in run["summary"]  # never just "failed"


def test_a_late_rejection_after_the_first_check_is_still_caught(client, owner):
    acc = _account(client, owner)
    _stock(client, owner)
    fake = fake_for(acc)
    real = fake.place_market_order
    seen = {"n": 0}

    def accepted(*a, **kw):
        res = real(*a, **kw)
        fake.order_book.append(Order(res.order_id, "validation pending", "AAA-EQ", "nse_cm", "MIS", "B", 10,
                                     tag=kw.get("tag")))
        return res

    real_orders = fake.orders

    def orders():
        seen["n"] += 1
        if seen["n"] >= 2:  # rejected a few seconds later
            for o in fake.order_book:
                o.status, o.message = "rejected", "RMS:Margin Exceeds,Required:52000, Available:100"
        return real_orders()

    fake.place_market_order, fake.orders = accepted, orders
    buy(client, owner)
    (row,) = _logs(action="buy")
    assert row.status == "failed" and "Margin Exceeds" in row.message and row.final
    assert trading.accounts_traded_today(_me(client, owner), trading.now_ist().date()) == set()


def test_orders_interrupted_by_a_restart_are_found_by_their_tag(client, owner, clock):
    clock.set(IN_SESSION)
    acc = _account(client, owner)
    user = _me(client, owner)
    run = trading._start_run(user, "entry", "command", "fno")
    plan = [trading.PlannedOrder(acc, "Main", "NIFTY", "NIFTY26OCTFUT", "nse_fo", "S", 65, product="NRML", lots=1)]
    (log_id,) = trading._reserve_logs(user, run, plan, "fno")
    # The server died right after sending: Kotak has the order (with TradeLab's tag), the row says pending.
    fake = fake_for(acc)
    fake.order_book.append(Order("K1", "complete", "NIFTY26OCTFUT", "nse_fo", "NRML", "S", 65, filled_qty=65,
                                 avg_price=120.0, tag=f"TL{log_id}"))
    fake.positions_map[("NIFTY26OCTFUT", "NRML")] = Position("nse_fo", "NIFTY26OCTFUT", "NRML", -65)
    trading.recover_after_restart()
    assert _logs(id=log_id)[0].status == "unknown"
    trading.reconcile_orders(user)
    row = _logs(id=log_id)[0]
    assert (row.status, row.order_id, row.quantity, row.price, row.final) == ("success", "K1", 65, 120.0, True)
    # So Exit and the panic button know about the written future again.
    assert list(trading.tradelab_open(user, "fno").values()) == [-65]


def test_an_order_kotak_never_got_is_marked_not_placed(client, owner, clock, monkeypatch):
    clock.set(IN_SESSION)
    acc = _account(client, owner)
    user = _me(client, owner)
    run = trading._start_run(user, "entry", "command", "fno")
    plan = [trading.PlannedOrder(acc, "Main", "NIFTY", "NIFTY26OCTFUT", "nse_fo", "B", 65, product="NRML", lots=1)]
    (log_id,) = trading._reserve_logs(user, run, plan, "fno")
    trading.recover_after_restart()
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    trading.reconcile_orders(user)
    row = _logs(id=log_id)[0]
    assert row.status == "failed" and "no such order" in row.message


# ----------------------------------------------------------------------------- square-off


def test_square_off_never_flips_a_position_when_kotak_positions_lag(client, owner):
    acc = _account(client, owner)
    user = _me(client, owner)
    fake = fake_for(acc)
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 10)
    fake.placed.clear()
    real = fake.place_market_order
    lagging = {}

    def close(seg, ts, side, qty, product="MIS", tag=None):
        res = real(seg, ts, side, qty, product, tag=tag)
        # The order book shows the fill at once; the positions view still shows the old position.
        fake.order_book.append(Order(res.order_id, "complete", ts, seg, product, side, qty, filled_qty=qty, tag=tag))
        lagging[(ts, product)] = 10
        return res

    real_positions = fake.positions

    def positions():
        out = real_positions()
        for p in out:
            if (p.trading_symbol, p.product) in lagging:
                p = Position(p.exchange_segment, p.trading_symbol, p.product, lagging[(p.trading_symbol, p.product)])
        return [Position(p.exchange_segment, p.trading_symbol, p.product,
                         lagging.get((p.trading_symbol, p.product), p.net_qty)) for p in out]

    fake.place_market_order, fake.positions = close, positions
    trading.run_exit(user, "manual", account_ids=[acc], section=trading.INTRADAY)
    assert [(p[2], p[3], p[4]) for p in fake.placed] == [("SBIN-EQ", "S", 10)]  # one close, never a second
    assert fake.positions_map[("SBIN-EQ", "MIS")].net_qty == 0


def test_square_off_leaves_cover_orders_and_their_stop_loss_alone(client, owner):
    acc = _account(client, owner)
    user = _me(client, owner)
    fake = fake_for(acc)
    fake.positions_map[("SBIN-EQ", "CO")] = Position("nse_cm", "SBIN-EQ", "CO", 10)
    fake.order_book.append(Order("SL1", "trigger pending", "SBIN-EQ", "nse_cm", "CO", "S", 10))
    trading.run_exit(user, "manual", account_ids=[acc], section=trading.ALL)
    assert fake.cancelled == [] and fake.placed == []


def test_big_contract_orders_go_in_slices_under_the_freeze_quantity(client, owner, clock):
    clock.set(IN_SESSION)
    acc = _account(client, owner)
    user = _me(client, owner)
    run = trading._start_run(user, "exit", "command", "fno")
    plan = [trading.PlannedOrder(acc, "Main", "NIFTY", "NIFTY26OCTFUT", "nse_fo", "S", 65 * 50, product="NRML",
                                 lots=50)]
    trading.fire_plan(plan, user, run, "fno")
    sizes = [p[4] for p in fake_for(acc).placed]
    assert sum(sizes) == 65 * 50 and max(sizes) < 1800 and all(q % 65 == 0 for q in sizes)
    assert len(_logs(run_id=run)) == len(sizes)  # each slice recorded on its own


# ----------------------------------------------------------------------------- the Intraday day


def test_an_entry_that_wakes_up_late_never_buys(client, owner, clock, monkeypatch):
    acc = _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)
    fire_at = trading.now_ist() + timedelta(milliseconds=300)
    real_sleep = trading.sleep_until

    def suspended(target, still_wanted=None):
        clock.set(trading.now_ist() + timedelta(days=1, minutes=1))  # the laptop slept until tomorrow
        return real_sleep(target, still_wanted)

    monkeypatch.setattr(trading, "sleep_until", suspended)
    trading.run_entry(_me(client, owner), "scheduled", fire_at=fire_at)
    assert fake_for(acc).placed == []
    with session_scope() as db:
        assert db.find(TradeSession)[0].status == "skipped"


def test_a_cancelled_entry_stops_waiting_at_once(client, owner):
    _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)
    user = _me(client, owner)
    fire_at = trading.now_ist() + timedelta(seconds=30)
    t = threading.Thread(target=trading.run_entry, args=(user, "scheduled", fire_at))
    t.start()
    time.sleep(0.5)
    started = time.monotonic()
    client.post("/api/automation/square-off-all", headers=owner)  # the panic cancels today's confirmation
    t.join(10)
    assert not t.is_alive() and time.monotonic() - started < 5  # not 30 s later


def test_a_day_where_nothing_was_sent_can_be_confirmed_again(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner)
    clock.set(MARKET_MORNING.replace(hour=11))
    fake = fake_for(acc)
    fake.creds["mpin"] = "000000"  # every login fails
    broker_manager._entries[acc].logged_in_at = None
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 200, r.text
    _wait(r.json()["run_id"])
    assert fake.placed == []
    fake.creds["mpin"] = "123456"
    broker_manager._entries[acc].failed_at = None
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 200, r.text  # not "already traded this day"
    _wait(r.json()["run_id"])
    assert len(fake.placed) == 1


def test_the_confirmation_covers_the_square_off_shown(client, owner):
    _account(client, owner)
    _stock(client, owner)
    st = client.get("/api/automation/status", headers=owner).json()["lets_trade"]
    client.put("/api/automation/settings", json={"auto_exit": False}, headers=owner)  # changed on another device
    r = client.post("/api/automation/lets-trade", json={"trade_date": st["trade_date"],
                                                        "fingerprint": st["fingerprint"]}, headers=owner)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    r = client.post("/api/automation/lets-trade", json={"trade_date": st["trade_date"]}, headers=owner)
    assert r.status_code == 409  # a confirmation without the fingerprint is refused


def test_a_missed_confirmation_for_today_is_not_listed_as_still_coming(client, owner, clock):
    _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)  # for today; the server was down at 09:15, so it never ran
    clock.set(MARKET_MORNING.replace(hour=15, minute=0))
    st = client.get("/api/automation/status", headers=owner).json()["lets_trade"]
    assert st["trade_date"] == "2026-10-15" and st["other_confirmed"] == []


def test_a_detected_closure_never_blocks_selling(client, owner, clock):
    clock.set(IN_SESSION)
    with session_scope() as db:
        db.add(Holiday(day=IN_SESSION.date(), description="Market closed (detected)", source="detected"))
    assert trading.trade_window_error("fno", "sell") is None
    assert trading.trade_window_error("holding", "sell") is None
    assert "closed today" in trading.trade_window_error("fno", "buy")


# ----------------------------------------------------------------------------- runs and confirmations


def test_a_repeated_yes_never_buys_twice(client, owner, clock):
    from test_sections import _add_etf

    clock.set(IN_SESSION)
    acc = _account(client, owner)
    _add_etf(client, owner)
    pv = client.get("/api/sections/holding/preview", params={"action": "buy"}, headers=owner).json()
    body = {"fingerprint": pv["fingerprint"], "confirm_id": pv["confirm_id"]}
    first = client.post("/api/sections/holding/buy", json=body, headers=owner).json()["run_id"]
    _wait(first)
    again = client.post("/api/sections/holding/buy", json=body, headers=owner)  # the reply was lost; tapped again
    assert again.status_code == 200 and again.json()["run_id"] == first
    assert len(fake_for(acc).placed) == 1
    r = client.post("/api/sections/holding/buy", json={"fingerprint": pv["fingerprint"]}, headers=owner)
    assert r.status_code == 409  # no confirmation id: refused


def test_a_buy_that_took_too_long_to_prepare_is_not_sent(client, owner, clock, monkeypatch):
    from test_sections import _add_etf

    clock.set(IN_SESSION)
    acc = _account(client, owner)
    _add_etf(client, owner)
    monkeypatch.setattr(trading, "COMMAND_DEADLINE", timedelta(seconds=0.2))
    real = trading.build_entry_plan

    def slow(*a, **kw):
        time.sleep(0.4)  # Kotak's quotes and margins answered slowly
        return real(*a, **kw)

    monkeypatch.setattr(trading, "build_entry_plan", slow)
    pv = client.get("/api/sections/holding/preview", params={"action": "buy"}, headers=owner).json()
    r = client.post("/api/sections/holding/buy", json={"fingerprint": pv["fingerprint"],
                                                       "confirm_id": pv["confirm_id"]}, headers=owner)
    run = _wait(r.json()["run_id"])
    assert fake_for(acc).placed == [] and "too slow" in run.summary


def test_the_panic_button_stops_a_buy_still_preparing(client, owner, clock, monkeypatch):
    from test_sections import _add_etf

    clock.set(IN_SESSION)
    acc = _account(client, owner)
    _add_etf(client, owner)
    real = trading.build_entry_plan
    go = threading.Event()

    def slow(*a, **kw):
        plan = real(*a, **kw)
        go.wait(5)
        return plan

    monkeypatch.setattr(trading, "build_entry_plan", slow)
    pv = client.get("/api/sections/holding/preview", params={"action": "buy"}, headers=owner).json()
    buy_run = client.post("/api/sections/holding/buy", json={"fingerprint": pv["fingerprint"],
                                                             "confirm_id": pv["confirm_id"]}, headers=owner).json()
    time.sleep(0.2)
    panic = client.post("/api/automation/square-off-all", headers=owner).json()["run_id"]
    time.sleep(0.2)
    go.set()
    _wait(panic)
    run = _wait(buy_run["run_id"])
    assert fake_for(acc).placed == [] and "Square off everything" in run.summary


def test_a_crash_while_squaring_off_never_leaves_the_run_running(client, owner, monkeypatch):
    acc = _account(client, owner)
    user = _me(client, owner)
    fake_for(acc).place_market_order("nse_cm", "SBIN-EQ", "B", 10)

    def broken(*a, **kw):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(trading, "_cancel_armed_today", broken)
    run_id = trading.launch("exit", user, section=trading.ALL)
    run = _wait(run_id)
    # Cancelling today's confirmations failed, but the positions were still closed, and the run finished.
    assert run.status == "partial"
    assert fake_for(acc).positions_map[("SBIN-EQ", "MIS")].net_qty == 0
    monkeypatch.setattr(trading, "_write_logs", broken)  # now saving the log fails too
    r = client.post("/api/automation/square-off-all", headers=owner)  # pressing it again still works
    assert r.status_code == 200
    assert _wait(r.json()["run_id"]).status in ("failed", "partial", "done")


def test_a_failed_automatic_square_off_is_tried_again(client, owner):
    acc = _account(client, owner)
    _stock(client, owner)
    buy(client, owner)
    user = _me(client, owner)
    fake = fake_for(acc)
    real_positions = fake.positions

    def down():
        raise BrokerError("Fetching positions: 503 Service Unavailable")

    fake.positions = down
    first = trading.run_exit(user, "scheduled")
    with session_scope() as db:
        assert db.get(ExecutionRun, first).status != "done"
    fake.positions = real_positions  # Kotak is back a minute later
    second = trading.run_exit(user, "catchup")
    assert second is not None and second != first
    assert all(p.net_qty == 0 for p in fake.positions())


def test_the_panic_cancels_tradelabs_working_carry_forward_orders(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    fake = fake_for(acc)
    real = fake.place_market_order

    def resting(seg, ts, side, qty, product="MIS", tag=None):
        res = OrderResult(ok=True, order_id=f"W{len(fake.placed) + 1}", message="Order placed")
        fake.placed.append((trading.now_ist().timestamp(), seg, ts, side, qty, product))
        fake.order_book.append(Order(res.order_id, "open", ts, seg, product, side, qty, tag=tag))
        return res  # accepted, not filled yet: nothing in positions

    fake.place_market_order = resting
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake.place_market_order = real
    panic = client.post("/api/automation/square-off-all", headers=owner).json()["run_id"]
    _wait(panic)
    assert fake.cancelled == ["W1"]  # it can't fill after the user asked to close everything


# ----------------------------------------------------------------------------- batch 2: schedule, records, limits


def test_timed_jobs_never_hold_the_scheduler_pool(client, owner, monkeypatch):
    from app.services import scheduler

    started = threading.Event()
    release = threading.Event()

    def slow_entry(*a, **kw):
        started.set()
        release.wait(5)  # an entry waits about a minute for its moment

    monkeypatch.setattr(scheduler, "_start_entry", slow_entry)
    t0 = time.monotonic()
    scheduler._entry_job(_me(client, owner), "intraday")
    assert time.monotonic() - t0 < 0.5 and started.wait(2)  # returned at once; the entry runs on its own
    release.set()


def test_a_buy_missed_by_a_restart_a_few_seconds_before_still_happens(client, owner, clock, monkeypatch):
    from app.services import scheduler

    acc = _account(client, owner)
    _stock(client, owner)
    confirm(client, owner)
    clock.set(MARKET_MORNING.replace(hour=9, minute=14, second=50))  # back up 10 s before the buy
    calls = []
    monkeypatch.setattr(scheduler, "_in_thread", lambda fn, *a, **kw: calls.append((fn.__name__, a)))
    scheduler.catch_up_entries()
    assert calls == [("_start_entry", (_me(client, owner), "intraday", "catchup"))]
    clock.set(MARKET_MORNING.replace(hour=9, minute=16))  # too late: never buys late
    calls.clear()
    scheduler.catch_up_entries()
    assert calls == [] and fake_for(acc).placed == []


def test_moving_the_square_off_into_the_past_squares_off_now(client, owner, clock, monkeypatch):
    from app.services import scheduler

    acc = _account(client, owner)
    _stock(client, owner)
    buy(client, owner)  # bought at 09:15
    clock.set(MARKET_MORNING.replace(hour=12))
    from types import SimpleNamespace

    # A running scheduler (its jobs aren't exercised here).
    monkeypatch.setattr(scheduler, "_scheduler", SimpleNamespace(shutdown=lambda wait=False: None))
    monkeypatch.setattr(scheduler, "sync_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "_in_thread", lambda fn, *a, **kw: fn(*a, **kw))
    r = client.put("/api/automation/settings", json={"exit_time": "11:00"}, headers=owner)
    assert r.status_code == 200
    assert fake_for(acc).positions_map[("AAA-EQ", "MIS")].net_qty == 0  # squared off straight away


def test_records_shrink_to_what_kotak_shows_and_an_opposite_order_is_then_a_fresh_one(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    ts = item["trading_symbol"]
    fake = fake_for(acc)
    fake.positions_map[(ts, "NRML")].net_qty = 0  # Kotak's risk system closed it (the row is still listed)
    client.patch(f"/api/basket/{item['id']}", json={"side": "S"}, headers=owner)
    pv, r = _command(client, owner, "fno", "buy")
    assert not any("First closes" in (o["note"] or "") for o in pv["orders"])
    _wait(r.json()["run_id"])
    # The short it opened is TradeLab's own and tracked: Exit would buy it back.
    assert list(trading.tradelab_open(_me(client, owner), "fno").values()) == [-65]
    assert [x.status for x in _logs(status="adjusted")] == ["adjusted"]


def test_contracts_are_not_forgotten_on_an_empty_positions_reply(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake_for(acc).positions_map.clear()  # Kotak answered "no data" (a glitch, or a new day)
    client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner)
    assert list(trading.tradelab_open(_me(client, owner), "fno").values()) == [65]  # not forgotten


def test_an_order_whose_run_is_gone_opens_nothing(client, owner, clock):
    clock.set(IN_SESSION)
    acc = _account(client, owner)
    with session_scope() as db:
        db.add(OrderLog(user_id=_me(client, owner), run_id=None, account_id=acc, account_name="Main", action="buy",
                        symbol="NIFTY26OCTFUT", quantity=65, status="unknown", exchange_segment="nse_fo",
                        product="NRML", section="fno"))
    assert trading.tradelab_open(_me(client, owner), "fno") == {}


def test_a_bought_future_is_not_counted_at_its_full_contract_value(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    _account(client, owner, funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    data = client.get("/api/sections/fno/positions", headers=owner).json()
    assert [row["value"] for row in data["rows"]] == [None] and data["value"] == 0


def test_the_exit_confirmation_covers_what_tradelab_holds(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    _account(client, owner, funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    pv = client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner).json()
    _, r = _command(client, owner, "fno", "buy")  # another device buys more meanwhile
    _wait(r.json()["run_id"])
    r = client.post("/api/sections/fno/sell", json={"fingerprint": pv["fingerprint"], "confirm_id": pv["confirm_id"]},
                    headers=owner)
    assert r.status_code == 409 and "changed" in r.json()["detail"]


def test_exit_stops_tradelabs_own_working_orders_first(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    fake = fake_for(acc)
    real = fake.place_market_order

    def resting(seg, ts, side, qty, product="MIS", tag=None):
        fake.placed.append((trading.now_ist().timestamp(), seg, ts, side, qty, product))
        fake.order_book.append(Order("W1", "open", ts, seg, product, side, qty, tag=tag))
        return OrderResult(ok=True, order_id="W1", message="Order placed")

    fake.place_market_order = resting
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake.place_market_order = real
    _, r = _command(client, owner, "fno", "sell")
    _wait(r.json()["run_id"])
    assert fake.cancelled == ["W1"]


def test_an_item_cannot_be_saved_with_no_accounts_and_a_detached_one_trades_nowhere(client, owner):
    a = _account(client, owner, "A", "UCC1")
    b = _account(client, owner, "B", "UCC2")
    item = _stock(client, owner)
    r = client.patch(f"/api/basket/{item['id']}", json={"account_ids": []}, headers=owner)
    assert r.status_code == 400 and "at least one account" in r.json()["detail"]
    client.patch(f"/api/basket/{item['id']}", json={"account_ids": [b]}, headers=owner)
    fake_for(b).funds_val = 0.0  # so it can be deleted
    assert client.delete(f"/api/accounts/{b}", headers=owner).status_code == 204
    client.patch(f"/api/basket/{item['id']}", json={"active": True}, headers=owner)  # switched back on
    plan = trading.build_entry_plan(_me(client, owner), [trading.PlanItem.from_item(
        trading.BasketItem(**{k: v for k, v in client.get("/api/basket", headers=owner).json()[0].items()
                              if k in ("symbol", "trading_symbol", "token", "exchange_segment", "side", "sizing_mode",
                                       "sizing_value", "account_ids")}, user_id=1))], "custom")
    assert all(p.account_id != a for p in plan)  # never "all accounts"


@pytest.mark.parametrize("value", ["Infinity", 1e309, 5e9])
def test_impossible_sizes_are_refused(client, owner, value):
    r = client.post("/api/basket", content=json.dumps({"symbol": "AAA", "trading_symbol": "AAA-EQ", "token": "1",
                                                        "sizing_mode": "amount", "sizing_value": value}).replace(
        '"Infinity"', "Infinity"), headers={**owner, "content-type": "application/json"})
    assert r.status_code == 422


def test_square_off_time_is_before_kotaks_and_after_the_buy(client, owner):
    r = client.put("/api/automation/settings", json={"exit_time": "15:05"}, headers=owner)
    assert r.status_code == 400
    r = client.put("/api/automation/settings", json={"entry_time": "14:00", "exit_time": "14:02"}, headers=owner)
    assert r.status_code == 400 and "5 minutes" in r.json()["detail"]


# ----------------------------------------------------------------------------- sizing (Kotak's protected price)


@pytest.mark.protected
def test_protected_price_follows_kotaks_grid():
    pp = trading.protected_price
    assert pp(40.0, False) == pytest.approx(40.8)       # equity under ₹100: 2%
    assert pp(250.0, False) == pytest.approx(252.5)     # ₹100-500: 1%
    assert pp(1000.0, False) == pytest.approx(1005.0)   # above ₹500: 0.5%
    assert pp(0.5, True) == pytest.approx(0.6)          # option under ₹1: +₹0.10
    assert pp(3.0, True) == pytest.approx(3.3)          # ₹1-5: 10%
    assert pp(8.0, True) == pytest.approx(8.4)          # ₹5-10: 5%
    assert pp(50.0, True) == pytest.approx(51.5)        # ₹10-100: 3%
    assert pp(200.0, True) == pytest.approx(204.0)      # ₹100-500: 2%
    assert pp(800.0, True) == pytest.approx(808.0)      # above ₹500: 1%


@pytest.mark.protected
def test_a_delivery_buy_of_a_cheap_etf_never_needs_more_than_the_cash(client, owner, clock):
    from test_sections import _add_etf

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=100000.0)
    FakeKotak.prices["10576"] = 40.0  # NIFTYBEES at ₹40
    _add_etf(client, owner, "NIFTYBEES")
    pv = client.get("/api/sections/holding/preview", params={"action": "buy"}, headers=owner).json()
    (order,) = pv["orders"]
    # Kotak blocks a market buy at ₹40.80, so the order must fit at that price.
    assert order["quantity"] * 40.8 <= 100000.0 * (1 - trading.CNC_HEADROOM)
    assert order["quantity"] == int(100000.0 * (1 - trading.CNC_HEADROOM) // 40.8)


@pytest.mark.protected
def test_option_buys_ask_the_margin_at_the_protected_price(client, owner, clock):
    from test_sections import _add_contract

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=100000.0)
    opt = _add_contract(client, owner, "nifty 25000 ce")
    FakeKotak.prices[opt["token"]] = 3.0
    client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner)
    asked = [c[5] for c in fake_for(acc).margin_calls]
    assert asked and asked[0] == pytest.approx(3.3)  # premium blocked at ₹3.30, not ₹3


def test_futures_use_at_most_twice_the_cash(client, owner, clock):
    from test_sections import _add_contract

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=100000.0)
    fake_for(acc).collateral = 90000.0  # ₹10,000 cash, ₹90,000 from pledged shares
    client.put("/api/sections/fno", json={"max_lots": 25}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    FakeKotak.margins[fut["trading_symbol"]] = 100.0  # ₹6,500 a lot
    pv = client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()
    (order,) = pv["orders"]
    cash = (100000.0 - 90000.0) * (1 - trading.CNC_HEADROOM)
    assert order["lots"] * 6500 * (1 + trading.LOT_HEADROOM) <= 2 * cash  # not the whole ₹1,00,000


def test_fixed_delivery_sizes_are_capped_by_the_cash(client, owner, clock):
    from test_sections import _add_etf

    clock.set(IN_SESSION)
    acc = _account(client, owner, funds=10000.0)
    client.put("/api/sections/holding", json={"sizing_mode": "custom"}, headers=owner)
    item = _add_etf(client, owner, "NIFTYBEES")
    client.patch(f"/api/basket/{item['id']}", json={"sizing_mode": "qty", "sizing_value": 500}, headers=owner)
    pv = client.get("/api/sections/holding/preview", params={"action": "buy"}, headers=owner).json()
    (order,) = pv["orders"]
    assert order["quantity"] == int(10000.0 * (1 - trading.CNC_HEADROOM) // 100.0) and "capped" in order["note"].lower()


def test_an_item_amount_below_one_share_is_not_called_lack_of_money(client, owner):
    acc = _account(client, owner, funds=10_00_000.0)
    client.put("/api/automation/settings", json={"sizing_mode": "custom"}, headers=owner)
    client.post("/api/basket", json={"symbol": "AAA", "trading_symbol": "AAA-EQ", "token": "1",
                                     "sizing_mode": "amount", "sizing_value": 50}, headers=owner)
    plan = trading.build_entry_plan(_me(client, owner), trading.live_items(_me(client, owner)).items, "custom")
    (p,) = plan
    assert p.skip.startswith("The ₹50 set for this item") and not p.funds_short


def test_keep_aside_applies_to_percent_sizes_too(client, owner):
    acc = _account(client, owner, funds=100000.0)
    client.put("/api/automation/settings", json={"sizing_mode": "custom", "reserve_pct": 50}, headers=owner)
    client.post("/api/basket", json={"symbol": "AAA", "trading_symbol": "AAA-EQ", "token": "1",
                                     "sizing_mode": "percent", "sizing_value": 100}, headers=owner)
    live = trading.live_items(_me(client, owner))
    (p,) = trading.build_entry_plan(_me(client, owner), live.items, "custom", live.reserve_pct)
    assert p.quantity == 500  # half of ₹1,00,000 at ₹100, not all of it


# ----------------------------------------------------------------------------- batch 4: accounts, holidays, alerts


def test_the_kotak_account_behind_an_entry_cannot_be_swapped(client, owner):
    acc = _account(client, owner)
    r = client.put(f"/api/accounts/{acc}/credentials", json={"ucc": "OTHER9"}, headers=owner)
    assert r.status_code == 400 and "can't be changed" in r.json()["detail"]
    r = client.put(f"/api/accounts/{acc}/credentials", json={"mpin": "654321"}, headers=owner)
    assert r.status_code == 200, r.text  # other details can still be updated


def test_one_kotak_account_cannot_be_added_by_two_users(client, owner):
    from conftest import signup

    _account(client, owner, ucc="UCC1")
    other = {"Authorization": f"Bearer {signup(client, 'b@example.com', 'Brother').json()['token']}"}
    r = client.post("/api/accounts", json=kotak_payload(name="Same", ucc="ucc1"), headers=other)
    assert r.status_code == 409


def test_exit_closes_tradelabs_positions_in_an_account_taken_out_of_the_section(client, owner, clock):
    from test_sections import _add_contract, _command

    clock.set(IN_SESSION)
    a = _account(client, owner, "A", "UCC1", funds=10_00_000)
    b = _account(client, owner, "B", "UCC2", funds=10_00_000)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    client.put("/api/sections/fno", json={"account_ids": [a]}, headers=owner)  # B taken out afterwards
    _, r = _command(client, owner, "fno", "sell")
    _wait(r.json()["run_id"])
    assert all(p.net_qty == 0 for p in fake_for(b).positions())  # nothing TradeLab opened is stranded


def test_an_nse_holiday_removed_by_the_owner_stays_removed(client, owner, clock):
    from app.services import calendar

    clock.set(IN_SESSION)
    day = IN_SESSION.date() + timedelta(days=7)
    nse = {"CM": [{"tradingDate": day.strftime("%d-%b-%Y"), "description": "Old date"}]}
    calendar.sync_nse_holidays(lambda: nse, today=IN_SESSION.date())
    assert trading.holiday_name(day) == "Old date"
    assert client.delete(f"/api/holidays/{day.isoformat()}", headers=owner).status_code == 204
    calendar.sync_nse_holidays(lambda: nse, today=IN_SESSION.date())  # the next morning's download
    assert trading.holiday_name(day) is None and trading.is_trading_day(day)
    days = [h["day"] for h in client.get("/api/holidays", headers=owner).json()["items"]]
    assert day.isoformat() not in days


def test_a_square_off_that_left_positions_open_raises_an_alert(client, owner):
    acc = _account(client, owner)
    _stock(client, owner)
    buy(client, owner)
    fake = fake_for(acc)
    real = fake.place_market_order

    def refused(seg, ts, side, qty, product="MIS", tag=None):
        return OrderResult(ok=False, message="RMS: square-off refused")

    fake.place_market_order = refused
    trading.run_exit(_me(client, owner), "scheduled")
    alerts = client.get("/api/alerts", headers=owner).json()
    assert [(a["kind"], a["accounts"]) for a in alerts] == [("exit", ["Main"])]
    fake.place_market_order = real


@pytest.mark.parametrize("msg,funds", [
    ("RMS:Margin Exceeds,Required:5000, Available:100 including holding collateral", True),
    ("Funds not sufficient for this order", True),
    ("Insufficient collateral", True),
    ("Check Cash Margin, Required:12000.00, Available:3000.00", True),
    ("Insufficient holding quantity", False),
    ("Scrip is in ban period", False),
    ("Order price is outside the circuit limit", False),
])
def test_funds_rejections_are_recognised(msg, funds):
    assert trading.is_funds_rejection(msg) is funds


def test_shutdown_waits_for_runs_still_sending():
    run_id = trading._start_run(1, "entry", "command", "holding")
    threading.Timer(0.3, trading._finish_run, args=(run_id, [])).start()
    assert trading.wait_for_runs(5) is True
    stuck = trading._start_run(1, "entry", "command", "holding")
    assert trading.wait_for_runs(0.3) is False
    trading._finish_run(stuck, [])
