"""Sections: Intraday, Holding, F&O and Commodity stay out of each other's way."""

import time
from datetime import datetime

from conftest import FakeKotak, fake_for, kotak_payload

from app.broker.base import Holding, Position
from app.config import IST
from app.db import session_scope
from app.models import ExecutionRun, OrderLog, TradeSession
from app.services import trading

IN_SESSION = datetime(2026, 10, 14, 10, 0, tzinfo=IST)  # a Wednesday


def _accounts(client, owner, n=2):
    ids = []
    for i in range(n):
        r = client.post("/api/accounts", json=kotak_payload(name=f"Acc {i}", ucc=f"UCC{i}"), headers=owner)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids


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


def _add_etf(client, owner, symbol="NIFTYBEES"):
    r = client.post("/api/basket", json={"section": "holding", "symbol": symbol, "trading_symbol": f"{symbol}-EQ",
                                         "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _add_contract(client, owner, query="nifty fut", section="fno"):
    found = client.get("/api/stocks/contracts", params={"section": section, "q": query}, headers=owner).json()
    c = found["results"][0]
    r = client.post("/api/basket", json={"section": section, "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "sizing_mode": "lots",
                                         "sizing_value": 1}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _command(client, owner, section, action, symbols=None):
    params = {"action": action}
    if symbols:
        params["symbols"] = symbols
    pv = client.get(f"/api/sections/{section}/preview", params=params, headers=owner)
    assert pv.status_code == 200, pv.text
    body = {"fingerprint": pv.json()["fingerprint"], "symbols": symbols}
    r = client.post(f"/api/sections/{section}/{action}", json=body, headers=owner)
    return pv.json(), r


def _confirm_section(client, owner, section):
    st = client.get("/api/automation/status", headers=owner).json()["sections"][section]
    r = client.post("/api/automation/lets-trade", json={"trade_date": st["trade_date"], "fingerprint": st["fingerprint"],
                                                        "section": section}, headers=owner)
    assert r.status_code == 200, r.text
    return r.json()


def test_sections_are_listed_with_their_rules(client, owner):
    data = client.get("/api/sections", headers=owner).json()["sections"]
    assert [s["section"] for s in data] == ["intraday", "holding", "fno", "commodity"]
    by = {s["section"]: s for s in data}
    assert by["intraday"]["scheduled"] and by["intraday"]["has_exit"] and not by["intraday"]["can_sell"]
    assert by["holding"]["scheduled"] and by["holding"]["can_sell"] and by["holding"]["product"] == "CNC"
    assert not by["fno"]["scheduled"] and by["fno"]["lots"] and by["fno"]["product"] == "NRML"
    assert by["commodity"]["buy_until"] == "23:00" and by["commodity"]["segments"] == ["mcx_fo"]
    assert all(s["account_ids"] is None and s["reserve_pct"] == 0 for s in data)


def test_section_settings_are_validated(client, owner):
    r = client.put("/api/sections/holding", json={"entry_time": "15:20"}, headers=owner)
    assert r.status_code == 400 and "between 09:15 and 15:15" in r.json()["detail"]
    r = client.put("/api/sections/holding", json={"product": "MIS"}, headers=owner)
    assert r.status_code == 400
    r = client.put("/api/sections/fno", json={"product": "MIS", "reserve_pct": 20, "max_lots": 4,
                                              "entry_time": "10:00"}, headers=owner)
    assert r.status_code == 200, r.text
    assert (r.json()["product"], r.json()["reserve_pct"], r.json()["max_lots"], r.json()["entry_time"]) == \
        ("MIS", 20, 4, None)
    r = client.put("/api/sections/fno", json={"account_ids": [999]}, headers=owner)
    assert r.status_code == 400


def test_holding_accepts_index_etfs_only_and_never_shorts(client, owner):
    _add_etf(client, owner)
    r = client.post("/api/basket", json={"section": "holding", "symbol": "SBIN", "trading_symbol": "SBIN-EQ"},
                    headers=owner)
    assert r.status_code == 400 and "index ETF" in r.json()["detail"]
    r = client.post("/api/basket", json={"section": "holding", "symbol": "GOLDBEES", "trading_symbol": "GOLDBEES-EQ"},
                    headers=owner)
    assert r.status_code == 400
    r = client.post("/api/basket", json={"section": "holding", "symbol": "BANKBEES", "trading_symbol": "BANKBEES-EQ",
                                         "side": "S"}, headers=owner)
    assert r.status_code == 400 and "only buys" in r.json()["detail"]
    etfs = client.get("/api/stocks/etfs", headers=owner).json()
    assert etfs["results"][0]["symbol"] == "NIFTYBEES" and etfs["total"] > 200


def test_holding_buys_for_delivery_with_cash_only(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    fake_for(ids[0]).collateral = 5000.0  # pledge margin can't pay for delivery
    _add_etf(client, owner, "NIFTYBEES")
    _add_etf(client, owner, "BANKBEES")
    res = _confirm_section(client, owner, "holding")
    assert res["run_id"], "confirming during market hours buys straight away"
    run = _wait(res["run_id"])
    assert run.status == "done", run.summary
    a, b = fake_for(ids[0]), fake_for(ids[1])
    # ₹25,000 cash split in two, less 1.5% for charges: ₹12,312 each at ₹100 -> 123 units.
    assert sorted((p[2], p[4], p[5]) for p in b.placed) == [("BANKBEES-EQ", 123, "CNC"), ("NIFTYBEES-EQ", 123, "CNC")]
    # (25,000 - 5,000 pledge) x 98.5% / 2 = ₹9,850 -> 98 units.
    assert sorted((p[2], p[4]) for p in a.placed) == [("BANKBEES-EQ", 98), ("NIFTYBEES-EQ", 98)]
    with session_scope() as db:
        assert {l.section for l in db.query(OrderLog).all()} == {"holding"}


def test_intraday_square_off_never_sells_holding(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    _add_etf(client, owner)
    _wait(_confirm_section(client, owner, "holding")["run_id"])
    fake = fake_for(ids[0])
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 10)  # an intraday position
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    trading.run_exit(user, "manual")
    left = {p.trading_symbol: p.net_qty for p in fake.positions()}
    assert left["SBIN-EQ"] == 0 and left["NIFTYBEES-EQ"] == 246


def test_holding_sell_sells_only_what_tradelab_bought(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    _add_etf(client, owner)
    _wait(_confirm_section(client, owner, "holding")["run_id"])
    fake = fake_for(ids[0])
    assert fake.positions_map[("NIFTYBEES-EQ", "CNC")].net_qty == 246
    # A family member's own 100 units of the same ETF, bought long ago.
    fake.holdings_list = [Holding("NIFTYBEES-EQ", "NIFTYBEES", "nse_cm", 100, 100, 250.0)]
    pv, r = _command(client, owner, "holding", "sell")
    assert r.status_code == 200, r.text
    assert [(o["trading_symbol"], o["quantity"], o["skip"]) for o in pv["orders"]] == [("NIFTYBEES-EQ", 246, None)]
    _wait(r.json()["run_id"])
    sells = [p for p in fake.placed if p[3] == "S"]
    assert [(p[2], p[4], p[5]) for p in sells] == [("NIFTYBEES-EQ", 246, "CNC")]
    # Selling again finds nothing of TradeLab's left; the family member's units stay.
    pv, _ = _command(client, owner, "holding", "sell")
    assert pv["orders"] == []
    assert len([p for p in fake.placed if p[3] == "S"]) == 1


def test_holding_sell_never_exceeds_what_the_broker_shows(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    _add_etf(client, owner)
    _wait(_confirm_section(client, owner, "holding")["run_id"])
    fake = fake_for(ids[0])
    fake.positions_map[("NIFTYBEES-EQ", "CNC")].net_qty = 40  # someone sold most of them in the Kotak app
    pv, r = _command(client, owner, "holding", "sell")
    assert [(o["quantity"], o["note"]) for o in pv["orders"]] == [(40, "Sell 40 of 246 bought by TradeLab")]
    _wait(r.json()["run_id"])
    assert [p[4] for p in fake.placed if p[3] == "S"] == [40]


def test_fno_buys_lots_on_command_with_money_split_by_margin(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    fake_for(ids[1]).funds_val = 9000.0
    client.put("/api/sections/fno", json={"max_lots": 50}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    opt = _add_contract(client, owner, "nifty 25000 ce")
    assert fut["lot_size"] == 65 and opt["option_type"] == "CE" and opt["strike"] == 25000
    pv, r = _command(client, owner, "fno", "buy")
    assert r.status_code == 200, r.text
    run = _wait(r.json()["run_id"])
    assert run.status == "done", run.summary
    # One lot = 65 units x ₹100 x 15% = ₹975 of margin. ₹25,000 / 2 = ₹12,500 -> 12 lots each.
    a, b = fake_for(ids[0]), fake_for(ids[1])
    assert sorted((p[2], p[4], p[5]) for p in a.placed) == sorted(
        [(fut["trading_symbol"], 12 * 65, "NRML"), (opt["trading_symbol"], 12 * 65, "NRML")])
    # ₹9,000 / 2 = ₹4,500 -> 4 lots each.
    assert sorted(p[4] for p in b.placed) == [4 * 65, 4 * 65]
    # The margin was asked at the latest price, as a buy, for one lot.
    assert all(c[2] == "B" and c[4] == 65 and c[5] == 100.0 for c in a.margin_calls + b.margin_calls)
    assert {o["lots"] for o in pv["orders"]} == {12, 4}


def test_fno_custom_lots_are_capped_by_money_and_max(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    fake_for(ids[0]).funds_val = 3000.0
    client.put("/api/sections/fno", json={"sizing_mode": "custom", "max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "nifty fut")
    client.patch(f"/api/basket/{item['id']}", json={"sizing_value": 5}, headers=owner)
    pv, _ = _command(client, owner, "fno", "buy")
    # Fixed 5 lots, but ₹3,000 covers only 3 lots of ₹975 and the cap is 2.
    assert [(o["lots"], o["quantity"]) for o in pv["orders"]] == [(2, 130)]


def test_fno_sell_closes_only_tradelab_contracts(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake = fake_for(ids[0])
    ts = fut["trading_symbol"]
    # The account holder also bought 2 lots of the same contract themselves.
    fake.positions_map[(ts, "NRML")].net_qty += 130
    pv, r = _command(client, owner, "fno", "sell")
    assert [(o["quantity"], o["lots"]) for o in pv["orders"]] == [(65, 1)]
    _wait(r.json()["run_id"])
    assert fake.positions_map[(ts, "NRML")].net_qty == 130


def test_fno_buy_and_sell_follow_market_hours(client, owner, clock):
    _accounts(client, owner, 1)
    _add_contract(client, owner, "nifty fut")
    clock.set(datetime(2026, 10, 14, 15, 20, tzinfo=IST))
    pv = client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()
    assert "Too late to buy" in pv["blocked"]
    r = client.post("/api/sections/fno/buy", json={"fingerprint": pv["fingerprint"]}, headers=owner)
    assert r.status_code == 409 and "Too late to buy" in r.json()["detail"]
    assert client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner).json()["blocked"] is None
    clock.set(datetime(2026, 10, 17, 11, 0, tzinfo=IST))  # Saturday
    r = client.post("/api/sections/fno/sell", json={}, headers=owner)
    assert r.status_code == 409 and "weekend" in r.json()["detail"]
    clock.set(datetime(2026, 10, 14, 9, 5, tzinfo=IST))
    r = client.post("/api/sections/fno/buy", json={}, headers=owner)
    assert r.status_code == 409 and "opens at 09:15" in r.json()["detail"]


def test_commodity_trades_into_the_evening(client, owner, clock):
    ids = _accounts(client, owner, 1)
    client.put("/api/sections/commodity", json={"max_lots": 1}, headers=owner)
    gold = _add_contract(client, owner, "goldm", section="commodity")
    assert gold["exchange_segment"] == "mcx_fo" and gold["lot_size"] == 100
    clock.set(datetime(2026, 10, 14, 22, 30, tzinfo=IST))
    _, r = _command(client, owner, "commodity", "buy")
    assert r.status_code == 200, r.text
    _wait(r.json()["run_id"])
    assert [(p[1], p[4], p[5]) for p in fake_for(ids[0]).placed] == [("mcx_fo", 100, "NRML")]
    clock.set(datetime(2026, 10, 14, 23, 10, tzinfo=IST))
    pv = client.get("/api/sections/commodity/preview", params={"action": "buy"}, headers=owner).json()
    assert pv["blocked"] and "Too late" in pv["blocked"]


def test_changed_list_needs_a_fresh_confirmation(client, owner, clock):
    clock.set(IN_SESSION)
    _accounts(client, owner, 1)
    _add_contract(client, owner, "nifty fut")
    pv = client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()
    client.put("/api/sections/fno", json={"max_lots": 3}, headers=owner)
    r = client.post("/api/sections/fno/buy", json={"fingerprint": pv["fingerprint"]}, headers=owner)
    assert r.status_code == 409 and "changed" in r.json()["detail"]


def test_section_accounts_limit_where_it_trades(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    client.put("/api/sections/fno", json={"account_ids": [ids[1]], "max_lots": 1}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    assert fake_for(ids[0]).placed == [] and len(fake_for(ids[1]).placed) == 1
    # An empty choice pauses the section instead of meaning "everyone".
    client.put("/api/sections/fno", json={"account_ids": []}, headers=owner)
    r = client.post("/api/sections/fno/buy", json={}, headers=owner)
    assert r.status_code == 409 and "autopilot" in r.json()["detail"]
    # Sell still shows what TradeLab holds in the account it no longer trades in, without selling it.
    pv = client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner).json()
    assert [o["skip"].startswith("Not sold") for o in pv["orders"]] == [True]


def test_funds_alert_for_a_command_buy(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    fake_for(ids[1]).funds_val = 500.0  # less than the margin of one lot
    _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    run = _wait(r.json()["run_id"])
    assert run.funds_short == ["Acc 1"]
    alerts = client.get("/api/alerts", headers=owner).json()
    assert alerts[0]["section"] == "fno" and alerts[0]["accounts"] == ["Acc 1"]


def test_panic_closes_intraday_and_tradelab_contracts_but_never_delivery(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake = fake_for(ids[0])
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 10)  # MIS
    fake.place_market_order("nse_cm", "ITC-EQ", "B", 7, product="CNC")  # delivery
    fake.place_market_order("nse_fo", "BANKNIFTY26OCTFUT", "B", 30, product="NRML")  # the holder's own contract
    fake.place_market_order("nse_cm", "IRFC-EQ", "B", 5, product="MTF")
    fake.positions_map[(fut["trading_symbol"], "NRML")].net_qty += 65  # and one more lot of TradeLab's contract
    # A Let's Trade for later today is withdrawn too.
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    with session_scope() as db:
        db.add(TradeSession(user_id=user, section="intraday", trade_date=IN_SESSION.date(), status="armed",
                            items=[], sizing_mode="equal"))
    r = client.post("/api/automation/square-off-all", headers=owner)
    run = _wait(r.json()["run_id"])
    left = {k[0]: p.net_qty for k, p in fake.positions_map.items()}
    assert left["SBIN-EQ"] == 0 and left["ITC-EQ"] == 7 and left["IRFC-EQ"] == 5
    assert left["BANKNIFTY26OCTFUT"] == 30 and left[fut["trading_symbol"]] == 65
    assert "cancelled" in run.summary
    with session_scope() as db:
        assert db.query(TradeSession).one().status == "cancelled"


def test_account_square_off_is_a_one_account_panic(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    client.get("/api/accounts/live", headers=owner)  # log both in
    for i in ids:
        fake_for(i).place_market_order("nse_cm", "SBIN-EQ", "B", 10)
        fake_for(i).place_market_order("nse_cm", "ITC-EQ", "B", 7, product="CNC")
    r = client.post(f"/api/accounts/{ids[0]}/square-off", headers=owner)
    _wait(r.json()["run_id"])
    assert {k[0]: p.net_qty for k, p in fake_for(ids[0]).positions_map.items()} == {"SBIN-EQ": 0, "ITC-EQ": 7}
    assert fake_for(ids[1]).positions_map[("SBIN-EQ", "MIS")].net_qty == 10


def test_home_tiles_leave_out_delivery(client, owner):
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake = fake_for(ids[0])
    fake.positions_map[("ITC-EQ", "CNC")] = Position("nse_cm", "ITC-EQ", "CNC", 7, 7, 0, 100, 0, 110, 70)
    fake.positions_map[("SBIN-EQ", "MIS")] = Position("nse_cm", "SBIN-EQ", "MIS", 10, 10, 0, 100, 0, 105, 50)
    live = client.get("/api/accounts/live", headers=owner).json()[0]
    assert live["open_positions"] == 1 and live["pnl"] == 50


def test_positions_view_shows_what_each_section_holds(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    _add_etf(client, owner)
    _wait(_confirm_section(client, owner, "holding")["run_id"])
    FakeKotak.prices = {}
    data = client.get("/api/sections/holding/positions", headers=owner).json()
    assert [(r["symbol"], r["quantity"], r["account_name"]) for r in data["rows"]] == [("NIFTYBEES", 246, "Acc 0")]
    assert client.get("/api/sections/intraday/positions", headers=owner).status_code == 400
    assert client.get("/api/sections/fno/positions", headers=owner).json()["rows"] == []


def test_intraday_buys_stay_with_intraday(client, owner, clock):
    """An intraday entry and a Holding buy on the same day each log their own section."""
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045"}, headers=owner)
    _add_etf(client, owner)
    _wait(_confirm_section(client, owner, "intraday")["run_id"])
    _wait(_confirm_section(client, owner, "holding")["run_id"])
    fake = fake_for(ids[0])
    products = {p[2]: p[5] for p in fake.placed}
    assert products == {"SBIN-EQ": "MIS", "NIFTYBEES-EQ": "CNC"}
    # Each used the money free when it traded (the fake broker's balance doesn't go down).
    assert [(p[2], p[4]) for p in fake.placed] == [("SBIN-EQ", 250), ("NIFTYBEES-EQ", 246)]
    with session_scope() as db:
        assert {(l.symbol, l.section) for l in db.query(OrderLog).filter(OrderLog.status == "success")} == \
            {("SBIN-EQ", "intraday"), ("NIFTYBEES-EQ", "holding")}


def test_panic_close_of_intraday_contracts_counts_as_their_sell(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.put("/api/sections/fno", json={"max_lots": 1, "product": "MIS"}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    fake = fake_for(ids[0])
    assert fake.positions_map[(fut["trading_symbol"], "MIS")].net_qty == 0
    with session_scope() as db:
        sells = db.query(OrderLog).filter(OrderLog.action == "sell", OrderLog.status == "success").all()
        assert [(l.symbol, l.section) for l in sells] == [(fut["trading_symbol"], "fno")]
    # The holder now buys the same contract themselves: TradeLab's Sell leaves it alone.
    fake.positions_map[(fut["trading_symbol"], "MIS")].net_qty = 65
    pv = client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner).json()
    assert pv["orders"] == []
