from datetime import timedelta

from conftest import confirm, fake_for, kotak_payload

from app.broker.base import Order
from app.db import session_scope
from app.models import Holiday, OrderLog
from app.services import trading


def _setup(client, owner, n_accounts=2):
    ids = []
    for i in range(n_accounts):
        r = client.post("/api/accounts", json=kotak_payload(name=f"Acc {i}", ucc=f"UCC{i}"), headers=owner)
        ids.append(r.json()["id"])
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                     "sizing_mode": "qty", "sizing_value": 10}, headers=owner)
    client.post("/api/basket", json={"symbol": "TCS", "trading_symbol": "TCS-EQ", "token": "11536",
                                     "sizing_mode": "amount", "sizing_value": 1050}, headers=owner)
    me = client.get("/api/auth/me", headers=owner).json()
    return me["id"], ids


def test_entry_fires_market_orders_exactly_at_time(client, owner, custom_sizing):
    user_id, ids = _setup(client, owner)
    fire_at = trading.now_ist() + timedelta(milliseconds=400)
    confirm(client, owner)
    trading.run_entry(user_id, "manual", fire_at=fire_at)
    for acc_id in ids:
        placed = fake_for(acc_id).placed
        assert sorted((p[2], p[3], p[4], p[5]) for p in placed) == [
            ("SBIN-EQ", "B", 10, "MIS"),
            ("TCS-EQ", "B", 10, "MIS"),  # ₹1050 / ₹100 LTP -> 10 shares
        ]
        assert all(p[0] >= fire_at.timestamp() for p in placed), "orders must not go out early"
    logs = client.get("/api/automation/logs", headers=owner).json()
    assert sum(1 for l in logs if l["status"] == "success") == 4
    runs = client.get("/api/automation/runs", headers=owner).json()
    assert runs[0]["status"] == "done"
    assert runs[0]["summary"].startswith("4 orders placed")


def test_square_off_cancels_pending_and_closes_intraday_positions(client, owner):
    user_id, ids = _setup(client, owner, n_accounts=1)
    fake = fake_for(ids[0])
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 10)
    fake.place_market_order("nse_cm", "INFY-EQ", "S", 4)  # short position gets bought back
    fake.place_market_order("nse_cm", "ITC-EQ", "B", 7, product="CNC")
    fake.order_book.append(Order("P1", "open", "SBIN-EQ", "nse_cm", "MIS", "B", 5))
    fake.placed.clear()

    trading.run_exit(user_id, "manual")
    assert fake.cancelled == ["P1"]
    # Delivery (CNC) units are never sold by a square-off: they may belong to Holding.
    assert {p.trading_symbol: p.net_qty for p in fake.positions()} == {"SBIN-EQ": 0, "INFY-EQ": 0, "ITC-EQ": 7}
    assert sorted((p[2], p[3], p[4]) for p in fake.placed) == [("INFY-EQ", "B", 4), ("SBIN-EQ", "S", 10)]


def test_square_off_intraday_scope_keeps_delivery(client, owner):
    user_id, ids = _setup(client, owner, n_accounts=1)
    client.put("/api/automation/settings", json={"squareoff_scope": "intraday"}, headers=owner)
    fake = fake_for(ids[0])
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 10)
    fake.place_market_order("nse_cm", "ITC-EQ", "B", 7, product="CNC")
    trading.run_exit(user_id, "manual")
    left = {p.trading_symbol: p.net_qty for p in fake.positions()}
    assert left == {"SBIN-EQ": 0, "ITC-EQ": 7}


def test_square_off_retries_until_flat(client, owner):
    user_id, ids = _setup(client, owner, n_accounts=1)
    fake = fake_for(ids[0])
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 10)
    real_place = fake.place_market_order
    calls = {"n": 0}

    def flaky(seg, ts, side, qty, product="MIS"):
        calls["n"] += 1
        if calls["n"] == 1:
            from app.broker.base import OrderResult
            return OrderResult(ok=False, message="RMS: temporary rejection")
        return real_place(seg, ts, side, qty, product)

    fake.place_market_order = flaky
    trading.run_exit(user_id, "manual")
    assert all(p.net_qty == 0 for p in fake.positions())
    assert calls["n"] == 2


def test_expired_session_relogs_and_retries(client, owner, custom_sizing):
    user_id, ids = _setup(client, owner, n_accounts=1)
    fake = fake_for(ids[0])
    logins_before = fake.logins
    fake.expire_next_order = True
    confirm(client, owner)
    trading.run_entry(user_id, "manual")
    assert fake.logins == logins_before + 1
    assert len(fake.placed) == 2


def test_failed_login_skips_that_account_only(client, owner, custom_sizing):
    user_id, ids = _setup(client, owner)
    bad = fake_for(ids[1])
    bad.creds["mpin"] = "000000"
    trading.broker_manager._entries[ids[1]].logged_in_at = None
    confirm(client, owner)
    trading.run_entry(user_id, "manual")
    assert len(fake_for(ids[0]).placed) == 2
    assert bad.placed == []
    accounts = {a["id"]: a for a in client.get("/api/accounts", headers=owner).json()}
    assert accounts[ids[1]]["status"] == "error"
    runs = client.get("/api/automation/runs", headers=owner).json()
    assert runs[0]["summary"] == "2 orders placed · 2 failed"  # login failures count as failures


def test_missed_entry_window_places_nothing(client, owner):
    user_id, ids = _setup(client, owner, n_accounts=1)
    confirm(client, owner)
    trading.run_entry(user_id, "manual", fire_at=trading.now_ist() - timedelta(minutes=2))
    assert fake_for(ids[0]).placed == []
    with session_scope() as db:
        assert {l.status for l in db.find(OrderLog)} == {"skipped"}


def test_scheduled_runs_skip_holidays_and_duplicates(client, owner):
    user_id, ids = _setup(client, owner, n_accounts=1)
    today = trading.now_ist().date()
    with session_scope() as db:
        db.add(Holiday(day=today, description="Test holiday"))
    assert trading.run_entry(user_id, "scheduled") is None
    assert trading.run_exit(user_id, "scheduled") is None
    with session_scope() as db:
        db.delete(db.get(Holiday, today))
    if today.weekday() < 5:
        confirm(client, owner)
        assert trading.run_entry(user_id, "scheduled") is not None
        assert trading.run_entry(user_id, "scheduled") is None  # only once per day


def test_automation_only_accounts_and_per_stock_accounts(client, owner, custom_sizing):
    user_id, ids = _setup(client, owner, n_accounts=3)
    client.patch(f"/api/accounts/{ids[2]}", json={"automation_enabled": False}, headers=owner)
    items = client.get("/api/basket", headers=owner).json()
    tcs = next(i for i in items if i["symbol"] == "TCS")
    client.patch(f"/api/basket/{tcs['id']}", json={"account_ids": [ids[0]]}, headers=owner)
    confirm(client, owner)
    trading.run_entry(user_id, "manual")
    assert len(fake_for(ids[0]).placed) == 2
    assert len(fake_for(ids[1]).placed) == 1
    assert ids[2] not in trading.broker_manager._entries or fake_for(ids[2]).placed == []


def test_percent_sizing_uses_available_funds(client, owner, custom_sizing):
    user_id, ids = _setup(client, owner, n_accounts=1)
    fake_for(ids[0]).funds_val = 20000
    items = client.get("/api/basket", headers=owner).json()
    for it in items:
        client.delete(f"/api/basket/{it['id']}", headers=owner)
    client.post("/api/basket", json={"symbol": "TCS", "trading_symbol": "TCS-EQ", "token": "11536",
                                     "sizing_mode": "percent", "sizing_value": 25}, headers=owner)
    confirm(client, owner)
    trading.run_entry(user_id, "manual")
    assert [(p[2], p[4]) for p in fake_for(ids[0]).placed] == [("TCS-EQ", 50)]  # 25% of 20k / ₹100


def test_settings_validation(client, owner):
    r = client.put("/api/automation/settings", json={"entry_time": "09:10"}, headers=owner)
    assert r.status_code == 400
    r = client.put("/api/automation/settings", json={"exit_time": "16:00"}, headers=owner)
    assert r.status_code == 400
    r = client.put("/api/automation/settings", json={"entry_time": "09:15", "exit_time": "14:30"}, headers=owner)
    assert r.status_code == 200
    assert r.json()["entry_time"] == "09:15:00"
    s = client.get("/api/automation/status", headers=owner).json()
    assert s["phase"] in {"closed", "preopen", "open", "weekend", "holiday"}


def test_basket_crud(client, owner):
    r = client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ"}, headers=owner)
    assert r.status_code == 201
    assert client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ"}, headers=owner).status_code == 409
    item = r.json()
    r = client.patch(f"/api/basket/{item['id']}", json={"sizing_mode": "amount"}, headers=owner)
    assert r.json()["sizing_value"] == 10000
    assert client.patch(f"/api/basket/{item['id']}", json={"sizing_mode": "qty", "sizing_value": 2.5}, headers=owner).status_code == 400
    r = client.patch(f"/api/basket/{item['id']}", json={"side": "S", "active": False}, headers=owner)
    assert r.json()["side"] == "S" and r.json()["active"] is False
    assert client.delete(f"/api/basket/{item['id']}", headers=owner).status_code == 204


def test_stock_search_has_offline_list(client, owner):
    r = client.get("/api/stocks/search?q=reli", headers=owner).json()
    assert r["results"][0]["symbol"] == "RELIANCE"
    assert r["source"] == "builtin"
