"""Nothing is ever bought unless the user pressed "Let's Trade" and confirmed it for that day."""

import time
from datetime import datetime, timedelta

import pytest
from conftest import MARKET_MORNING, FakeKotak, confirm, fake_for, kotak_payload, lets_trade

from app.config import IST
from app.db import session_scope
from app.models import Holiday, TradeSession
from app.services import calendar, scheduler, trading


def _me(client, owner):
    return client.get("/api/auth/me", headers=owner).json()["id"]


def _account(client, owner, name="Main", ucc="UCC1", funds=100000):
    acc = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner).json()
    fake_for(acc["id"]).funds_val = funds
    return acc["id"]


def _stock(client, owner, sym, token="1", **extra):
    r = client.post("/api/basket", json={"symbol": sym, "trading_symbol": f"{sym}-EQ", "token": token, **extra},
                    headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _state(client, owner):
    return client.get("/api/automation/status", headers=owner).json()["lets_trade"]


def _wait_for_runs(client, owner, n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        runs = client.get("/api/automation/runs", headers=owner).json()
        if len(runs) >= n and all(r["status"] != "running" for r in runs):
            return runs
        time.sleep(0.05)
    raise AssertionError("run did not finish")


@pytest.fixture(autouse=True)
def quick_recheck(monkeypatch):
    monkeypatch.setattr(trading, "MARKET_RECHECK", timedelta(milliseconds=200))


# --- the confirmation gate ---------------------------------------------------------------


def test_without_lets_trade_nothing_is_ever_sent(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    logins = fake_for(acc).logins

    scheduler._warmup_job()                                  # 09:00: no confirmation -> no login
    assert trading.run_entry(user, "scheduled") is None      # 09:14: nothing confirmed -> nothing
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is None       # 14:30: nothing bought -> no square-off
    assert fake_for(acc).placed == [] and fake_for(acc).cancelled == []
    assert fake_for(acc).logins == logins
    assert client.get("/api/automation/runs", headers=owner).json() == []


def test_confirmed_day_buys_exactly_the_confirmed_list(client, owner):
    acc = _account(client, owner, funds=10000)
    _stock(client, owner, "AAA")
    st = _state(client, owner)
    assert st["trade_date"] == "2026-10-14" and st["session"] is None and st["buys_now"] is False

    r = lets_trade(client, owner, "2026-10-14").json()
    assert r["run_id"] is None and r["session"]["status"] == "armed" and r["session"]["stocks"] == ["AAA"]

    # Editing the list afterwards doesn't change what trades — the app flags it instead.
    _stock(client, owner, "BBB")
    assert _state(client, owner)["session"]["changed"] is True

    trading.run_entry(_me(client, owner), "scheduled")
    assert [(p[2], p[4]) for p in fake_for(acc).placed] == [("AAA-EQ", 100)]
    assert _state(client, owner)["session"]["status"] == "done"


def test_list_stays_but_each_day_needs_a_new_confirmation(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    assert len(fake_for(acc).placed) == 1

    clock.set(MARKET_MORNING + timedelta(days=1))
    fake_for(acc).placed.clear()
    assert [i["symbol"] for i in client.get("/api/basket", headers=owner).json()] == ["AAA"]  # list kept
    assert trading.run_entry(user, "scheduled") is None
    assert fake_for(acc).placed == []
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    assert len(fake_for(acc).placed) == 1


def test_cancel_after_confirming(client, owner):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    confirm(client, owner)
    r = client.post("/api/automation/lets-trade/cancel", headers=owner)
    assert r.status_code == 200 and r.json()["session"]["status"] == "cancelled"
    assert trading.run_entry(_me(client, owner), "scheduled") is None
    assert fake_for(acc).placed == []
    assert client.post("/api/automation/lets-trade/cancel", headers=owner).status_code == 409


def test_confirmation_checks(client, owner):
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 409 and "Add at least one stock" in r.json()["detail"]
    _stock(client, owner, "AAA")
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 409 and "autopilot" in r.json()["detail"]
    _account(client, owner)
    r = lets_trade(client, owner, "2026-10-15")
    assert r.status_code == 409 and "Wed, 14 Oct" in r.json()["detail"]  # the dialog showed another day
    off = client.get("/api/basket", headers=owner).json()[0]["id"]
    client.patch(f"/api/basket/{off}", json={"active": False}, headers=owner)
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 409  # only switched-on stocks count


def test_confirming_during_the_session_buys_right_away(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    clock.set(MARKET_MORNING.replace(hour=11))
    st = _state(client, owner)
    assert st["trade_date"] == "2026-10-14" and st["buys_now"] is True
    r = lets_trade(client, owner, "2026-10-14").json()
    assert r["run_id"] is not None
    _wait_for_runs(client, owner)
    assert len(fake_for(acc).placed) == 1
    # Same day again: refused, the day has already traded.
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 409 and "already traded" in r.json()["detail"]


def test_confirming_a_moment_before_entry_still_trades_at_entry_time(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    clock.set(MARKET_MORNING.replace(hour=9, minute=14, second=59, microsecond=700000))  # job already ran
    r = lets_trade(client, owner, "2026-10-14").json()
    assert r["run_id"] is not None
    _wait_for_runs(client, owner)
    placed_at = fake_for(acc).placed[0][0]
    assert placed_at >= MARKET_MORNING.replace(hour=9, minute=15).timestamp()


def test_after_square_off_time_confirmation_is_for_the_next_trading_day(client, owner, clock):
    _account(client, owner)
    _stock(client, owner, "AAA")
    clock.set(datetime(2026, 10, 16, 15, 0, tzinfo=IST))     # Friday afternoon
    with session_scope() as db:
        db.add(Holiday(day=datetime(2026, 10, 19).date(), description="Holiday", source="nse"))
    st = _state(client, owner)
    assert st["trade_date"] == "2026-10-20" and st["buys_now"] is False  # skips weekend + Monday holiday
    assert confirm(client, owner)["session"]["status"] == "armed"


def test_two_runs_can_never_trade_the_same_confirmation(client, owner):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "manual")
    trading.run_entry(user, "manual")
    trading.run_entry(user, "scheduled")
    assert len(fake_for(acc).placed) == 1


# --- square-off only where TradeLab bought ------------------------------------------------


def test_square_off_only_in_accounts_tradelab_bought_in(client, owner, clock):
    a = _account(client, owner, "A", "UCC1")
    b = _account(client, owner, "B", "UCC2")
    _stock(client, owner, "AAA", account_ids=[a])
    fake_for(b).place_market_order("nse_cm", "MANUAL-EQ", "B", 5)  # the user's own trade
    fake_for(b).placed.clear()
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is not None
    assert all(p.net_qty == 0 for p in fake_for(a).positions())
    assert fake_for(b).placed == [] and fake_for(b).positions()[0].net_qty == 5


def test_holiday_added_after_entry_cannot_block_square_off(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    with session_scope() as db:
        db.add(Holiday(day=MARKET_MORNING.date(), description="added by mistake"))
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is not None
    assert all(p.net_qty == 0 for p in fake_for(acc).positions())


# --- automatic holiday detection ----------------------------------------------------------


def test_live_check_skips_a_closed_market_without_any_order(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    FakeKotak.quote_time = (MARKET_MORNING - timedelta(days=1)).timestamp()  # nothing traded today
    fire_at = trading.now_ist() + timedelta(milliseconds=200)
    trading.run_entry(user, "scheduled", fire_at=fire_at)
    assert fake_for(acc).placed == []
    runs = client.get("/api/automation/runs", headers=owner).json()
    assert "Market closed today" in runs[0]["summary"]
    with session_scope() as db:
        assert db.get(Holiday, MARKET_MORNING.date()).source == "detected"
    with session_scope() as db:
        assert db.find_one(TradeSession, sort=[("id", 1)]).status == "skipped"
    assert _state(client, owner)["trade_date"] == "2026-10-15"  # the next confirmation is for tomorrow


def test_live_check_rechecks_after_the_open_before_giving_up(client, owner, monkeypatch):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    looks = iter([False, True])  # closed-looking before the open, trading after it
    monkeypatch.setattr(calendar, "market_open_today", lambda clients, now: next(looks))
    trading.run_entry(user, "scheduled", fire_at=trading.now_ist() + timedelta(milliseconds=100))
    assert len(fake_for(acc).placed) == 1


def test_live_check_unknown_or_open_trades_normally(client, owner):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    FakeKotak.quote_time = MARKET_MORNING.replace(hour=9, minute=7).timestamp()  # pre-open today
    trading.run_entry(user, "scheduled", fire_at=trading.now_ist() + timedelta(milliseconds=100))
    assert len(fake_for(acc).placed) == 1


def test_market_open_check_needs_several_quotes(clock):
    class OneQuote:
        def quote_times(self, instruments):
            return {instruments[0][1]: 0.0}

    class Old:
        def quote_times(self, instruments):
            return {tok: 1.0 for _, tok in instruments}

    now = clock.now()
    assert calendar.market_open_today([OneQuote()], now) is None   # too little evidence
    assert calendar.market_open_today([], now) is None
    assert calendar.market_open_today([object(), Old()], now) is False


NSE_SAMPLE = {
    "CM": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day", "Sr_no": 1},
        {"tradingDate": "21-Oct-2026", "weekDay": "Wednesday", "description": "Diwali Laxmi Pujan", "Sr_no": 2},
        {"tradingDate": "not a date", "description": "junk"},
    ],
    "FO": [{"tradingDate": "01-Jan-2026", "description": "should be ignored (CM wins)"}],
}


def test_nse_calendar_sync(client, owner):
    from datetime import date

    with session_scope() as db:
        db.add(Holiday(day=date(2026, 10, 21), description="My own note", source="manual"))
        db.add(Holiday(day=date(2026, 11, 30), description="Moved by NSE", source="nse"))
    status = calendar.sync_nse_holidays(lambda: NSE_SAMPLE, today=date(2026, 10, 14))
    assert status["last_error"] is None and status["count"] == 2
    with session_scope() as db:
        assert db.get(Holiday, date(2026, 1, 26)).source == "nse"
        assert db.get(Holiday, date(2026, 10, 21)).description == "My own note"  # manual entries win
        assert db.get(Holiday, date(2026, 11, 30)) is None                      # NSE no longer lists it
    body = client.get("/api/holidays", headers=owner).json()
    assert [h["day"] for h in body["items"]] == ["2026-10-21"]
    assert body["nse"]["count"] == 2


def test_nse_calendar_failure_is_reported_not_raised():
    def boom():
        raise ConnectionError("blocked")

    status = calendar.sync_nse_holidays(boom)
    assert "blocked" in status["last_error"]


def test_parse_nse_holidays_formats():
    out = calendar.parse_nse_holidays({"CM": [{"tradingDate": "2026-03-04", "description": "Holi"}]})
    assert [d.isoformat() for d in out] == ["2026-03-04"]
    assert calendar.parse_nse_holidays({}) == {}


# --- misc ---------------------------------------------------------------------------------


def test_morning_login_only_for_confirmed_users(client, owner):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    logins = fake_for(acc).logins
    scheduler._warmup_job()
    assert fake_for(acc).logins == logins
    confirm(client, owner)
    scheduler._warmup_job()
    assert fake_for(acc).logins == logins + 1


def test_old_buy_now_endpoint_is_gone(client, owner):
    assert client.post("/api/automation/run-entry", headers=owner).status_code == 404


def test_past_holidays_are_rejected(client, owner):
    assert client.post("/api/holidays", json={"day": "2026-10-13"}, headers=owner).status_code == 400
    assert client.post("/api/holidays", json={"day": "2026-10-14"}, headers=owner).status_code == 201


def test_prices_are_retried_when_the_first_request_fails(client, owner):
    acc = _account(client, owner, funds=10000)
    _stock(client, owner, "AAA")
    calls = {"n": 0}
    real = FakeKotak.ltp

    def flaky(self, instruments):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("quotes timed out")
        return real(self, instruments)

    FakeKotak.ltp = flaky
    try:
        confirm(client, owner)
        trading.run_entry(_me(client, owner), "manual")
    finally:
        FakeKotak.ltp = real
    assert [p[4] for p in fake_for(acc).placed] == [100]


def test_prices_fall_back_to_another_kotak_account(client, owner):
    a = _account(client, owner, "A", "UCC1", 10000)
    b = _account(client, owner, "B", "UCC2", 10000)

    def broken(instruments):
        raise ConnectionError("this key is rate limited")

    fake_for(a).ltp = broken
    _stock(client, owner, "AAA")
    confirm(client, owner)
    trading.run_entry(_me(client, owner), "manual")
    assert [p[4] for p in fake_for(a).placed] == [100]
    assert [p[4] for p in fake_for(b).placed] == [100]


def test_missing_prices_are_reported_as_failures(client, owner):
    acc = _account(client, owner, funds=10000)
    _stock(client, owner, "AAA", token=None)
    confirm(client, owner)
    trading.run_entry(_me(client, owner), "manual")
    assert fake_for(acc).placed == []
    runs = client.get("/api/automation/runs", headers=owner).json()
    assert runs[0]["status"] == "failed"


def test_preview_uses_the_current_list_and_prices_every_stock(client, owner, custom_sizing):
    _account(client, owner)
    FakeKotak.prices["7"] = 321.0
    _stock(client, owner, "AAA", token="7", sizing_mode="qty", sizing_value=2)
    rows = client.get("/api/automation/preview", headers=owner).json()
    assert rows[0]["quantity"] == 2 and rows[0]["price"] == 321.0


# --- fixes from the second review ------------------------------------------------------------


def _run_in_thread(fn, *args, **kw):
    import threading

    t = threading.Thread(target=fn, args=args, kwargs=kw)
    t.start()
    return t


def test_cancel_works_until_the_orders_go_out(client, owner):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    t = _run_in_thread(trading.run_entry, user, "scheduled", fire_at=trading.now_ist() + timedelta(seconds=1.5))
    time.sleep(0.5)  # preparation has started (session claimed), orders not sent yet
    r = client.post("/api/automation/lets-trade/cancel", headers=owner)
    assert r.status_code == 200, r.text
    t.join(5)
    assert fake_for(acc).placed == []
    assert "Cancelled before the buy" in client.get("/api/automation/runs", headers=owner).json()[0]["summary"]


def test_account_switched_off_during_preparation_is_left_out(client, owner):
    a = _account(client, owner, "A", "UCC1")
    b = _account(client, owner, "B", "UCC2")
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    confirm(client, owner)
    t = _run_in_thread(trading.run_entry, user, "scheduled", fire_at=trading.now_ist() + timedelta(seconds=1.5))
    time.sleep(0.5)
    client.patch(f"/api/accounts/{b}", json={"automation_enabled": False}, headers=owner)
    t.join(5)
    assert len(fake_for(a).placed) == 1 and fake_for(b).placed == []


def test_order_with_lost_reply_is_still_squared_off(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    fake = fake_for(acc)
    fake.record_fills = False  # Kotak's order book doesn't show it yet either
    real = fake.place_market_order

    def fills_but_times_out(*args, **kw):
        real(*args, **kw)                      # the exchange got it...
        raise TimeoutError("Read timed out")   # ...but the reply never arrived

    fake.place_market_order = fills_but_times_out
    user = _me(client, owner)
    confirm(client, owner)
    trading.run_entry(user, "scheduled")
    log = client.get("/api/automation/logs", headers=owner).json()[0]
    assert log["status"] == "unknown" and "may have gone through" in log["message"]
    fake.place_market_order = real
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is not None
    assert all(p.net_qty == 0 for p in fake.positions())


def test_orders_are_logged_before_they_are_sent(client, owner):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    seen = {}
    fake = fake_for(acc)
    real = fake.place_market_order

    def peek(*args, **kw):
        seen["logs"] = client.get("/api/automation/logs", headers=owner).json()
        return real(*args, **kw)

    fake.place_market_order = peek
    confirm(client, owner)
    trading.run_entry(_me(client, owner), "scheduled")
    assert [l["status"] for l in seen["logs"]] == ["pending"]
    assert [l["status"] for l in client.get("/api/automation/logs", headers=owner).json()] == ["success"]


def test_square_off_waits_for_a_buy_still_being_sent(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    fake = fake_for(acc)
    real = fake.place_market_order

    def slow(*args, **kw):
        time.sleep(1.0)
        return real(*args, **kw)

    fake.place_market_order = slow
    clock.set(MARKET_MORNING.replace(hour=14, minute=20))
    lets_trade(client, owner, "2026-10-14")  # buys now
    time.sleep(0.3)
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(_me(client, owner), "scheduled") is not None
    assert all(p.net_qty == 0 for p in fake.positions())


def test_no_same_day_buy_right_before_square_off(client, owner, clock):
    _account(client, owner)
    _stock(client, owner, "AAA")
    clock.set(MARKET_MORNING.replace(hour=14, minute=27))
    st = _state(client, owner)
    assert st["closing_soon"] is True
    r = lets_trade(client, owner, "2026-10-14")
    assert r.status_code == 409 and "Too close" in r.json()["detail"]


def test_other_confirmed_days_are_shown_and_can_be_cancelled(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    clock.set(MARKET_MORNING.replace(hour=14, minute=45))   # after square-off -> confirms Thursday
    assert confirm(client, owner)["trade_date"] == "2026-10-15"
    client.put("/api/automation/settings", json={"exit_time": "15:00"}, headers=owner)
    st = _state(client, owner)                              # the offered day moved back to today
    assert st["trade_date"] == "2026-10-14" and st["other_confirmed"] == ["2026-10-15"]
    r = client.post("/api/automation/lets-trade/cancel", json={"trade_date": "2026-10-15"}, headers=owner)
    assert r.status_code == 200 and r.json()["other_confirmed"] == []
    clock.set(MARKET_MORNING + timedelta(days=1))
    assert trading.run_entry(_me(client, owner), "scheduled") is None
    assert fake_for(acc).placed == []
