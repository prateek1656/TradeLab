"""Safety rules: nothing is ordered unless stocks were given for that very day."""

from datetime import datetime, timedelta

from conftest import MARKET_MORNING, FakeKotak, fake_for, kotak_payload

from app.config import IST
from app.db import session_scope
from app.models import BasketItem, Holiday
from app.services import scheduler, trading


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


def test_stocks_belong_to_one_day_and_never_repeat(client, owner, clock):
    acc = _account(client, owner)
    item = _stock(client, owner, "AAA")
    assert item["trade_date"] == "2026-10-14"
    user = _me(client, owner)

    trading.run_entry(user, "scheduled")
    assert len(fake_for(acc).placed) == 1

    # Next morning: yesterday's list does NOT roll over, so nothing is ordered.
    clock.set(MARKET_MORNING + timedelta(days=1))
    fake_for(acc).placed.clear()
    assert trading.run_entry(user, "scheduled") is not None
    assert fake_for(acc).placed == []
    runs = client.get("/api/automation/runs", headers=owner).json()
    assert runs[0]["summary"].startswith("Nothing to trade")


def test_no_stocks_means_no_square_off_and_own_trades_are_left_alone(client, owner, clock):
    acc = _account(client, owner)
    fake = fake_for(acc)
    fake.place_market_order("nse_cm", "MYTRADE-EQ", "B", 50)  # placed by the user in the Kotak app
    fake.placed.clear()
    user = _me(client, owner)

    trading.run_entry(user, "scheduled")              # no stocks given today
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is None
    assert trading.run_exit(user, "catchup") is None
    assert fake.placed == [] and fake.cancelled == []
    assert {p.trading_symbol: p.net_qty for p in fake.positions()} == {"MYTRADE-EQ": 50}


def test_square_off_only_in_accounts_tradelab_bought_in(client, owner, clock):
    a = _account(client, owner, "A", "UCC1")
    b = _account(client, owner, "B", "UCC2")
    _stock(client, owner, "AAA", account_ids=[a])     # today's stock only for account A
    fake_for(b).place_market_order("nse_cm", "MANUAL-EQ", "B", 5)
    fake_for(b).placed.clear()
    user = _me(client, owner)

    trading.run_entry(user, "scheduled")
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is not None
    assert all(p.net_qty == 0 for p in fake_for(a).positions())
    assert fake_for(b).placed == []                   # B untouched
    assert fake_for(b).positions()[0].net_qty == 5


def test_holiday_added_after_entry_cannot_block_square_off(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    trading.run_entry(user, "scheduled")
    with session_scope() as db:
        db.add(Holiday(day=MARKET_MORNING.date(), description="added by mistake"))
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is not None
    assert all(p.net_qty == 0 for p in fake_for(acc).positions())


def test_autopilot_off_after_entry_keeps_account_out_of_square_off(client, owner, clock):
    acc = _account(client, owner)
    _stock(client, owner, "AAA")
    user = _me(client, owner)
    trading.run_entry(user, "scheduled")
    client.patch(f"/api/accounts/{acc}", json={"automation_enabled": False}, headers=owner)
    clock.set(MARKET_MORNING.replace(hour=14, minute=30))
    assert trading.run_exit(user, "scheduled") is None
    assert fake_for(acc).positions()[0].net_qty > 0


def test_after_square_off_time_stocks_go_to_the_next_trading_day(client, owner, clock):
    clock.set(datetime(2026, 10, 16, 15, 0, tzinfo=IST))   # Friday afternoon
    assert _stock(client, owner, "AAA")["trade_date"] == "2026-10-19"  # Monday
    clock.set(datetime(2026, 10, 16, 14, 0, tzinfo=IST))   # Friday, before 14:30
    assert _stock(client, owner, "BBB")["trade_date"] == "2026-10-16"
    with session_scope() as db:
        db.add(Holiday(day=datetime(2026, 10, 19).date(), description="Holiday"))
    clock.set(datetime(2026, 10, 17, 10, 0, tzinfo=IST))   # Saturday
    body = client.get("/api/basket", headers=owner).json()
    assert body["plan_date"] == "2026-10-20"                # skips the weekend and Monday's holiday


def test_reuse_puts_earlier_stocks_on_the_new_day(client, owner, clock):
    _account(client, owner)
    old = _stock(client, owner, "AAA", sizing_mode="qty", sizing_value=3, side="S")
    _stock(client, owner, "BBB")
    clock.set(MARKET_MORNING + timedelta(days=1))
    body = client.get("/api/basket", headers=owner).json()
    assert body["plan_date"] == "2026-10-15"
    assert {i["trade_date"] for i in body["items"]} == {"2026-10-14"}

    r = client.post("/api/basket/reuse", json={"ids": [old["id"]]}, headers=owner)
    assert [i["symbol"] for i in r.json()] == ["AAA"]
    items = {i["symbol"]: i for i in client.get("/api/basket", headers=owner).json()["items"]}
    assert items["AAA"]["trade_date"] == "2026-10-15" and items["AAA"]["side"] == "S"
    assert items["BBB"]["trade_date"] == "2026-10-14"
    client.post("/api/basket/reuse", json={}, headers=owner)   # all of them
    assert {i["trade_date"] for i in client.get("/api/basket", headers=owner).json()["items"]} == {"2026-10-15"}


def test_adding_a_stock_from_an_earlier_day_moves_it(client, owner, clock):
    first = _stock(client, owner, "AAA")
    clock.set(MARKET_MORNING + timedelta(days=1))
    again = _stock(client, owner, "AAA")
    assert again["id"] == first["id"] and again["trade_date"] == "2026-10-15"
    assert client.post("/api/basket", json={"symbol": "AAA", "trading_symbol": "AAA-EQ"}, headers=owner).status_code == 409


def test_status_reports_lists_and_whether_tradelab_traded(client, owner):
    _account(client, owner)
    s = client.get("/api/automation/status", headers=owner).json()
    assert (s["plan_date"], s["plan_stocks"], s["today_stocks"], s["traded_today"]) == ("2026-10-14", 0, 0, False)
    _stock(client, owner, "AAA")
    trading.run_entry(_me(client, owner), "manual")
    s = client.get("/api/automation/status", headers=owner).json()
    assert (s["plan_stocks"], s["today_stocks"], s["traded_today"]) == (1, 1, True)


def test_morning_login_only_for_people_with_stocks_today(client, owner, monkeypatch):
    acc = _account(client, owner)
    logins = fake_for(acc).logins
    scheduler._warmup_job()
    assert fake_for(acc).logins == logins           # no stocks today -> no login attempt
    _stock(client, owner, "AAA")
    scheduler._warmup_job()
    assert fake_for(acc).logins == logins + 1


def test_past_holidays_are_rejected(client, owner):
    assert client.post("/api/holidays", json={"day": "2026-10-13"}, headers=owner).status_code == 400
    assert client.post("/api/holidays", json={"day": "2026-10-14"}, headers=owner).status_code == 201


def test_legacy_stocks_without_a_date_never_trade(client, owner):
    acc = _account(client, owner)
    item = _stock(client, owner, "AAA")
    with session_scope() as db:
        db.get(BasketItem, item["id"]).trade_date = None
    trading.run_entry(_me(client, owner), "manual")
    assert fake_for(acc).placed == []


# --- price fetching robustness ---------------------------------------------------------


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
        trading.run_entry(_me(client, owner), "manual")
    finally:
        FakeKotak.ltp = real
    assert [p[4] for p in fake_for(acc).placed] == [100]
    assert calls["n"] == 2


def test_prices_fall_back_to_another_kotak_account(client, owner):
    a = _account(client, owner, "A", "UCC1", 10000)
    b = _account(client, owner, "B", "UCC2", 10000)

    def broken(instruments):
        raise ConnectionError("this key is rate limited")

    fake_for(a).ltp = broken
    _stock(client, owner, "AAA")
    trading.run_entry(_me(client, owner), "manual")
    assert [p[4] for p in fake_for(a).placed] == [100]
    assert [p[4] for p in fake_for(b).placed] == [100]


def test_missing_prices_are_reported_as_failures(client, owner):
    acc = _account(client, owner, funds=10000)
    _stock(client, owner, "AAA", token=None)
    trading.run_entry(_me(client, owner), "manual")
    assert fake_for(acc).placed == []
    runs = client.get("/api/automation/runs", headers=owner).json()
    assert runs[0]["status"] == "failed"
    log = client.get("/api/automation/logs", headers=owner).json()[0]
    assert log["status"] == "failed" and "No live price" in log["message"]


def test_preview_prices_every_stock_in_per_stock_mode(client, owner, custom_sizing):
    _account(client, owner)
    FakeKotak.prices["7"] = 321.0
    _stock(client, owner, "AAA", token="7", sizing_mode="qty", sizing_value=2)
    rows = client.get("/api/automation/preview", headers=owner).json()
    assert rows[0]["quantity"] == 2 and rows[0]["price"] == 321.0


def test_buy_now_only_during_todays_session(client, owner, clock):
    _account(client, owner)
    _stock(client, owner, "AAA")
    assert client.post("/api/automation/run-entry", headers=owner).status_code == 200
    clock.set(MARKET_MORNING.replace(hour=15))   # after square-off time: the list is for tomorrow now
    r = client.post("/api/automation/run-entry", headers=owner)
    assert r.status_code == 409 and "Thu, 15 Oct" in r.json()["detail"]
