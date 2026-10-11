"""Sections: Intraday, Holding, F&O and Commodity stay out of each other's way."""

import time
from datetime import date, datetime

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


def _buy_holding(client, owner):
    """Tap Buy on the Holding tab and answer "Yes"; returns the finished run."""
    _, r = _command(client, owner, "holding", "buy")
    assert r.status_code == 200, r.text
    return _wait(r.json()["run_id"])


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
    # Holding, F&O and Commodity never work by the clock: no buy time, no square-off, no MIS.
    for key in ("holding", "fno", "commodity"):
        assert not by[key]["scheduled"] and not by[key]["has_exit"] and by[key]["can_sell"]
        assert by[key]["entry_time"] is None and by[key]["exit_time"] is None
        assert "MIS" not in by[key]["products"]
    assert by["holding"]["product"] == "CNC" and by["fno"]["products"] == ["NRML"] and by["commodity"]["products"] == ["NRML"]
    assert by["fno"]["lots"] and by["commodity"]["buy_until"] == "23:25" and by["commodity"]["segments"] == ["mcx_fo"]
    assert all(s["account_ids"] is None and s["reserve_pct"] == 0 for s in data)


def test_section_settings_are_validated(client, owner):
    r = client.put("/api/sections/holding", json={"product": "MIS"}, headers=owner)
    assert r.status_code == 400
    # Intraday (MIS) contracts would be squared off by Kotak at the close: not allowed.
    for key in ("fno", "commodity"):
        r = client.put(f"/api/sections/{key}", json={"product": "MIS"}, headers=owner)
        assert r.status_code == 400 and "NRML" in r.json()["detail"]
    # A buy time can't be set on an on-command section (it is ignored).
    r = client.put("/api/sections/fno", json={"reserve_pct": 20, "max_lots": 4, "entry_time": "10:00"}, headers=owner)
    assert r.status_code == 200, r.text
    assert (r.json()["product"], r.json()["reserve_pct"], r.json()["max_lots"], r.json()["entry_time"]) == \
        ("NRML", 20, 4, None)
    r = client.put("/api/sections/holding", json={"entry_time": "10:00"}, headers=owner)
    assert r.status_code == 200 and r.json()["entry_time"] is None
    r = client.put("/api/sections/fno", json={"account_ids": [999]}, headers=owner)
    assert r.status_code == 400


def test_holding_takes_stocks_and_etfs_and_never_shorts(client, owner):
    _add_etf(client, owner)
    r = client.post("/api/basket", json={"section": "holding", "symbol": "SBIN", "trading_symbol": "SBIN-EQ"},
                    headers=owner)
    assert r.status_code == 201, r.text
    assert (r.json()["symbol"], r.json()["name"]) == ("SBIN", "State Bank of India")
    r = client.post("/api/basket", json={"section": "holding", "symbol": "NOPE", "trading_symbol": "NOPE-EQ"},
                    headers=owner)
    assert r.status_code == 400 and "stock or ETF list" in r.json()["detail"]
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
    run = _buy_holding(client, owner)
    assert run.status == "done", run.summary
    assert run.trigger == "command"
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
    _buy_holding(client, owner)
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
    _buy_holding(client, owner)
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
    _buy_holding(client, owner)
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
    client.put("/api/sections/fno", json={"max_lots": 25}, headers=owner)
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
    clock.set(datetime(2026, 10, 14, 15, 20, tzinfo=IST))  # still open for both
    assert client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()["blocked"] is None
    clock.set(datetime(2026, 10, 14, 15, 26, tzinfo=IST))
    pv = client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()
    assert "Too late to buy" in pv["blocked"]
    r = client.post("/api/sections/fno/buy", json={"fingerprint": pv["fingerprint"]}, headers=owner)
    assert r.status_code == 409 and "Too late to buy" in r.json()["detail"]
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
    clock.set(datetime(2026, 10, 14, 23, 20, tzinfo=IST))
    assert client.get("/api/sections/commodity/preview", params={"action": "buy"}, headers=owner).json()["blocked"] is None
    clock.set(datetime(2026, 10, 14, 23, 26, tzinfo=IST))
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
    assert [o["skip"].startswith("Not closed") for o in pv["orders"]] == [True]


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
    _buy_holding(client, owner)
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
    _buy_holding(client, owner)
    fake = fake_for(ids[0])
    products = {p[2]: p[5] for p in fake.placed}
    assert products == {"SBIN-EQ": "MIS", "NIFTYBEES-EQ": "CNC"}
    # Each used the money free when it traded (the fake broker's balance doesn't go down).
    assert [(p[2], p[4]) for p in fake.placed] == [("SBIN-EQ", 250), ("NIFTYBEES-EQ", 246)]
    with session_scope() as db:
        assert {(l.symbol, l.section) for l in db.query(OrderLog).filter(OrderLog.status == "success")} == \
            {("SBIN-EQ", "intraday"), ("NIFTYBEES-EQ", "holding")}


def test_panic_sells_tradelab_contracts_and_credits_intraday_closes(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake = fake_for(ids[0])
    fake.place_market_order("nse_fo", "BANKNIFTY26OCTFUT", "B", 30, product="MIS")  # the holder's own intraday trade
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert fake.positions_map[(fut["trading_symbol"], "NRML")].net_qty == 0
    assert fake.positions_map[("BANKNIFTY26OCTFUT", "MIS")].net_qty == 0
    with session_scope() as db:
        sells = db.query(OrderLog).filter(OrderLog.action == "sell", OrderLog.status == "success").all()
        # TradeLab's NRML contract is closed by its own records; the holder's intraday contract is
        # nobody's business of F&O, so its close isn't credited to any section.
        assert sorted((l.symbol, l.section or "") for l in sells) == sorted(
            [(fut["trading_symbol"], "fno"), ("BANKNIFTY26OCTFUT", "")])
    # The holder buys BANKNIFTY again themselves: TradeLab's Sell leaves it alone.
    fake.positions_map[("BANKNIFTY26OCTFUT", "MIS")].net_qty = 30
    pv = client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner).json()
    assert pv["orders"] == []


def test_panic_stops_a_buy_already_waiting_for_its_moment(client, owner, clock):
    clock.set(datetime(2026, 10, 14, 9, 0, tzinfo=IST))
    _accounts(client, owner, 1)
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045"}, headers=owner)
    _confirm_section(client, owner, "intraday")
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    # The scheduled job has claimed the confirmation and waits for 09:15.
    sess = trading._claim_session(user, IN_SESSION.date(), "intraday")
    assert sess is not None
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert trading._begin_firing(sess.id) is False  # so the buy never goes out
    with session_scope() as db:
        assert db.get(TradeSession, sess.id).status == "cancelled"


def test_option_premium_is_sized_from_cash_not_pledge(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake = fake_for(ids[0])
    fake.funds_val, fake.collateral = 100000.0, 90000.0
    client.put("/api/sections/fno", json={"max_lots": 25}, headers=owner)
    _add_contract(client, owner, "nifty 25000 ce")
    pv = client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()
    # ₹10,000 cash x 98.5% ≈ ₹9,850 against ₹999 per lot (₹975 + 2.5%) -> 9 lots, not 100.
    assert [o["lots"] for o in pv["orders"]] == [9]


def test_fixed_lots_share_one_budget(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake_for(ids[0]).funds_val = 1500.0
    client.put("/api/sections/fno", json={"sizing_mode": "custom"}, headers=owner)
    _add_contract(client, owner, "nifty fut")
    _add_contract(client, owner, "banknifty fut")
    pv = client.get("/api/sections/fno/preview", params={"action": "buy"}, headers=owner).json()
    # Each lot needs ₹999 (BANKNIFTY ₹461): the first takes ₹999 of ₹1,500, the second still fits.
    lots = [(o["symbol"].split()[0], o["lots"], bool(o["skip"])) for o in pv["orders"]]
    assert lots[0][1] == 1
    total = sum(o["lots"] * (65 if o["symbol"].startswith("NIFTY") else 30) * 15 * 1.025 for o in pv["orders"] if o["lots"])
    assert total <= 1500


def test_partial_fill_is_what_sell_counts(client, owner, clock):
    from app.broker.base import Order

    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake = fake_for(ids[0])
    real = fake.place_market_order

    def thin_book(seg, ts, side, qty, product="MIS"):
        res = real(seg, ts, side, qty, product)
        fake.positions_map[(ts, product)].net_qty -= qty - 100  # only 100 fill
        fake.order_book.append(Order(res.order_id, "cancelled", ts, seg, product, side, qty, filled_qty=100))
        return res

    fake.place_market_order = thin_book
    _add_etf(client, owner)
    _buy_holding(client, owner)
    fake.holdings_list = [Holding("NIFTYBEES-EQ", "NIFTYBEES", "nse_cm", 500, 500, 250.0)]  # the holder's own
    pv = client.get("/api/sections/holding/preview", params={"action": "sell"}, headers=owner).json()
    assert [o["quantity"] for o in pv["orders"]] == [100]


def test_two_taps_start_one_buy(client, owner, clock):
    import threading as th

    clock.set(IN_SESSION)
    _accounts(client, owner, 1)
    _add_contract(client, owner, "nifty fut")
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    results = []
    gate = th.Barrier(4)

    def tap():
        gate.wait()
        try:
            results.append(trading.launch("entry", user, section="fno", trigger="command", job=lambda rid: time.sleep(0.5)))
        except trading.RunInProgress:
            results.append("busy")

    threads = [th.Thread(target=tap) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if r != "busy") == 1


def test_nothing_in_holding_fno_or_commodity_happens_by_time(client, owner, clock):
    """Only Intraday has a Let's Trade and timed jobs; the others buy and sell only on command."""
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.services import scheduler

    clock.set(datetime(2026, 10, 14, 9, 0, tzinfo=IST))
    ids = _accounts(client, owner, 1)
    _add_etf(client, owner)
    for key in ("holding", "fno", "commodity"):
        r = client.post("/api/automation/lets-trade", json={"trade_date": "2026-10-14", "section": key}, headers=owner)
        assert r.status_code == 409, (key, r.text)
    sched = BackgroundScheduler(timezone=IST)
    sched.start(paused=True)
    scheduler._scheduler = sched
    try:
        scheduler.sync_jobs()
        ids_ = sorted(j.id.rsplit(":", 1)[-1] for j in sched.get_jobs() if j.id.startswith(("entry:", "exit:")))
        assert ids_ == ["intraday", "intraday"]
    finally:
        sched.shutdown(wait=False)
        scheduler._scheduler = None
    # A Holding confirmation left by an older version never buys, and is withdrawn on restart.
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    with session_scope() as db:
        db.add(TradeSession(user_id=user, section="holding", trade_date=date(2026, 10, 14), status="armed",
                            items=[{"symbol": "NIFTYBEES", "trading_symbol": "NIFTYBEES-EQ", "exchange_segment": "nse_cm",
                                    "side": "B", "sizing_mode": "qty", "sizing_value": 1}], sizing_mode="equal"))
    trading.run_entry(user, "scheduled", fire_at=datetime(2026, 10, 14, 9, 20, tzinfo=IST), section="holding")
    trading.recover_after_restart()
    assert ids[0] not in trading.broker_manager._entries or fake_for(ids[0]).placed == []
    with session_scope() as db:
        assert db.query(TradeSession).one().status == "cancelled"


def test_positions_show_contract_expiry(client, owner, clock):
    clock.set(IN_SESSION)
    _accounts(client, owner, 1)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    fut = _add_contract(client, owner, "nifty fut")
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    client.delete(f"/api/basket/{fut['id']}", headers=owner)  # removed from the list, still held
    rows = client.get("/api/sections/fno/positions", headers=owner).json()["rows"]
    assert [(x["symbol"], x["lots"], x["expiry"]) for x in rows] == [(fut["symbol"], 1, fut["expiry"])]


def test_expiry_notes_follow_exchange_and_kotak_rules():
    from app.services.contracts import Contract

    def c(seg, sym, inst, opt, expiry):
        return Contract(seg, f"{sym}X", "1", sym, inst, opt, 100.0 if opt else None, expiry, 1)

    today = date(2026, 10, 14)
    gold = trading.expiry_note(c("mcx_fo", "GOLDM", "FUTCOM", None, date(2026, 10, 20)), today)
    assert gold["level"] == "warn" and "tender" in gold["text"]
    assert trading.expiry_note(c("mcx_fo", "CRUDEOIL", "FUTCOM", None, date(2026, 10, 20)), today) is None
    # Stock F&O: counted in trading days (Tue 27 Oct expiry -> warned from Wed 21 Oct, 4 trading days before).
    assert trading.expiry_note(c("nse_fo", "RELIANCE", "FUTSTK", None, date(2026, 10, 27)), date(2026, 10, 20)) is None
    assert "delivery of shares" in trading.expiry_note(c("nse_fo", "RELIANCE", "FUTSTK", None, date(2026, 10, 27)),
                                                       date(2026, 10, 21))["text"]
    # MCX index futures are settled in cash: no tender-period warning.
    assert trading.expiry_note(c("mcx_fo", "MCXBULLDEX", "FUTIDX", None, date(2026, 10, 20)), today) is None
    assert trading.expiry_note(c("nse_fo", "NIFTY", "OPTIDX", "CE", date(2026, 10, 27)), today) is None
    assert trading.expiry_note(c("nse_fo", "NIFTY", "OPTIDX", "CE", date(2026, 10, 14)), today)["level"] == "warn"


def test_a_delivery_sale_needing_approval_says_what_to_do():
    msg = trading.explain_rejection("sell", "CNC", "RMS:Rule: Check holdings, eDIS authorisation pending")
    assert "turn on DDPI" in msg
    assert trading.explain_rejection("buy", "CNC", "Insufficient balance.") == "Insufficient balance."
    assert trading.explain_rejection("sell", "NRML", "authorisation") == "authorisation"


def test_holding_takes_bond_gold_and_liquid_etfs(client, owner):
    for sym in ("GOLDBEES", "EBBETF0430", "LIQUIDBEES", "GILT5YBEES"):
        r = client.post("/api/basket", json={"section": "holding", "symbol": sym, "trading_symbol": f"{sym}-EQ"},
                        headers=owner)
        assert r.status_code == 201, (sym, r.text)
    groups = client.get("/api/stocks/etfs", params={"group": "Bond & gilt", "limit": 400}, headers=owner).json()
    assert groups["results"] and {e["group"] for e in groups["results"]} == {"Bond & gilt"}
    assert "Gold & silver" in groups["groups"] and groups["total"] > 300


# ───────────────────────────────────────────── selling calls and puts first


def _add_write(client, owner, query="nifty 25000 pe"):
    found = client.get("/api/stocks/contracts", params={"section": "fno", "q": query}, headers=owner).json()
    c = found["results"][0]
    r = client.post("/api/basket", json={"section": "fno", "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "side": "S", "sizing_mode": "lots",
                                         "sizing_value": 1}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _add_sided(client, owner, section, query, side):
    c = client.get("/api/stocks/contracts", params={"section": section, "q": query}, headers=owner).json()["results"][0]
    r = client.post("/api/basket", json={"section": section, "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "side": side, "sizing_mode": "lots",
                                         "sizing_value": 1}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def test_every_contract_can_be_sold_first_but_holding_only_buys(client, owner):
    fut = _add_sided(client, owner, "fno", "nifty fut", "S")
    assert fut["side"] == "S" and fut["option_type"] is None
    assert client.patch(f"/api/basket/{fut['id']}", json={"side": "B"}, headers=owner).json()["side"] == "B"
    gold = _add_sided(client, owner, "commodity", "goldm", "S")
    crude_put = _add_sided(client, owner, "commodity", "crudeoil pe", "S")
    assert gold["instrument_type"] == "FUTCOM" and crude_put["option_type"] == "PE"
    r = client.post("/api/basket", json={"section": "holding", "symbol": "NIFTYBEES", "trading_symbol": "NIFTYBEES-EQ",
                                         "side": "S"}, headers=owner)
    assert r.status_code == 400  # delivery can't be sold before it's bought
    item = client.post("/api/basket", json={"section": "holding", "symbol": "NIFTYBEES", "trading_symbol": "NIFTYBEES-EQ"},
                       headers=owner).json()
    assert client.patch(f"/api/basket/{item['id']}", json={"side": "S"}, headers=owner).status_code == 400


def test_selling_a_future_first_and_exit_buys_it_back(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    fut = _add_sided(client, owner, "fno", "nifty fut", "S")
    ts = fut["trading_symbol"]
    FakeKotak.margins[ts] = 15.0  # ₹975 per lot of 65
    try:
        pv, r = _command(client, owner, "fno", "buy")
        assert pv["warnings"] and "no limit" in pv["warnings"][0]
        assert [(o["side"], o["lots"]) for o in pv["orders"]] == [("S", 10)]  # the default cap of 10 lots
        _wait(r.json()["run_id"])
        fake = fake_for(ids[0])
        assert [(p[2], p[3], p[4], p[5]) for p in fake.placed] == [(ts, "S", 650, "NRML")]
        assert all(c[2] == "S" for c in fake.margin_calls if c[1] == ts)
        rows = client.get("/api/sections/fno/positions", headers=owner).json()["rows"]
        assert [(x["side"], x["quantity"], x["value"]) for x in rows] == [("S", 650, None)]
        pv, r = _command(client, owner, "fno", "sell")
        assert [(o["side"], o["quantity"], o["note"]) for o in pv["orders"]] == [("B", 650, "Buy back 650")]
        _wait(r.json()["run_id"])
        assert fake.positions_map[(ts, "NRML")].net_qty == 0
    finally:
        FakeKotak.margins.pop(ts, None)


def test_commodity_futures_and_options_can_be_sold_first(client, owner, clock):
    clock.set(datetime(2026, 10, 14, 19, 0, tzinfo=IST))  # MCX's evening session
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake = fake_for(ids[0])
    fake.funds_val = 100000.0
    gold = _add_sided(client, owner, "commodity", "goldm", "S")
    put = _add_sided(client, owner, "commodity", "crudeoil pe", "S")
    pv, r = _command(client, owner, "commodity", "buy")
    assert pv["warnings"] and not pv["blocked"]
    _wait(r.json()["run_id"])
    sold = {p[2]: (p[3], p[5]) for p in fake.placed}
    assert sold == {gold["trading_symbol"]: ("S", "NRML"), put["trading_symbol"]: ("S", "NRML")}
    pv, r = _command(client, owner, "commodity", "sell")
    assert {o["side"] for o in pv["orders"]} == {"B"} and len(pv["orders"]) == 2
    _wait(r.json()["run_id"])
    assert all(p.net_qty == 0 for p in fake.positions_map.values())


def test_writing_an_option_sells_lots_sized_by_writing_margin(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.put("/api/sections/fno", json={"max_lots": 25}, headers=owner)
    put = _add_write(client, owner)
    pv, r = _command(client, owner, "fno", "buy")
    assert pv["warnings"] and "no limit" in pv["warnings"][0]
    assert r.status_code == 200, r.text
    _wait(r.json()["run_id"])
    fake = fake_for(ids[0])
    # Writing margin: 65 x ₹100 x 2 = ₹13,000 (+2.5%) per lot; ₹25,000 covers 1 lot. Pledge money may be used.
    assert [(p[2], p[3], p[4], p[5]) for p in fake.placed] == [(put["trading_symbol"], "S", 65, "NRML")]
    assert all(c[2] == "S" for c in fake.margin_calls)
    rows = client.get("/api/sections/fno/positions", headers=owner).json()["rows"]
    assert [(x["side"], x["quantity"], x["lots"], x["value"]) for x in rows] == [("S", 65, 1, None)]


def test_exit_buys_back_written_options_only_up_to_what_tradelab_sold(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    put = _add_write(client, owner)
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    fake = fake_for(ids[0])
    ts = put["trading_symbol"]
    fake.positions_map[(ts, "NRML")].net_qty -= 130  # the holder also wrote 2 lots themselves
    pv, r = _command(client, owner, "fno", "sell")
    assert [(o["side"], o["quantity"], o["note"]) for o in pv["orders"]] == [("B", 65, "Buy back 65")]
    _wait(r.json()["run_id"])
    assert fake.positions_map[(ts, "NRML")].net_qty == -130  # theirs is untouched
    pv, _ = _command(client, owner, "fno", "sell")
    assert pv["orders"] == []


def test_panic_buys_back_written_options(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    put = _add_write(client, owner)
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert fake_for(ids[0]).positions_map[(put["trading_symbol"], "NRML")].net_qty == 0


def test_unclear_replies_never_lead_to_opening_or_closing_twice(client, owner, clock):
    """An opening order with no reply isn't counted as open; a closing one is counted as closed."""
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    put = _add_write(client, owner)
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    key = trading.Lot(ids[0], put["exchange_segment"], put["trading_symbol"], "NRML")
    assert trading.tradelab_open(user, "fno") == {key: -65}
    with session_scope() as db:
        run = ExecutionRun(user_id=user, kind="exit", section="fno", trigger="command", trading_date=IN_SESSION.date())
        db.add(run)
        db.flush()
        db.add(OrderLog(user_id=user, run_id=run.id, account_id=ids[0], account_name="Acc 0", action="buy",
                        symbol=put["trading_symbol"], quantity=65, status="unknown", exchange_segment="nse_fo",
                        product="NRML", section="fno"))
    assert trading.tradelab_open(user, "fno") == {}


def _add_option(client, owner, query, side):
    c = client.get("/api/stocks/contracts", params={"section": "fno", "q": query}, headers=owner).json()["results"][0]
    r = client.post("/api/basket", json={"section": "fno", "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "side": side, "sizing_mode": "lots",
                                         "sizing_value": 1}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def test_selling_first_never_sells_the_account_holders_own_contracts(client, owner, clock):
    """Kotak nets an order against an open position, so it isn't sent against the holder's own one."""
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    client.get("/api/accounts/live", headers=owner)
    put = _add_write(client, owner)
    ts = put["trading_symbol"]
    fake_for(ids[0]).positions_map[(ts, "NRML")] = Position(put["exchange_segment"], ts, "NRML", 130)  # theirs
    pv, r = _command(client, owner, "fno", "buy")
    orders = {o["account_id"]: o for o in pv["orders"]}
    assert orders[ids[0]]["quantity"] == 0 and "bought outside TradeLab" in orders[ids[0]]["skip"]
    assert orders[ids[1]]["quantity"] == 65 and not orders[ids[1]]["skip"]
    _wait(r.json()["run_id"])
    assert fake_for(ids[0]).placed == [] and fake_for(ids[0]).positions_map[(ts, "NRML")].net_qty == 130
    assert [(p[2], p[3]) for p in fake_for(ids[1]).placed] == [(ts, "S")]


def test_buying_never_buys_back_what_the_holder_sold_first(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    call = _add_option(client, owner, "nifty 25000 ce", "B")
    ts = call["trading_symbol"]
    fake = fake_for(ids[0])
    fake.positions_map[(ts, "NRML")] = Position(call["exchange_segment"], ts, "NRML", -65)  # the holder wrote it
    pv, _ = _command(client, owner, "fno", "buy")
    assert [(o["quantity"], "outside TradeLab" in (o["skip"] or "")) for o in pv["orders"]] == [(0, True)]
    assert fake.placed == []


def test_tradelabs_own_written_option_can_be_turned_into_a_bought_one(client, owner, clock):
    """Its own sold-first position isn't the holder's, so switching to Buy is allowed; the cost restarts."""
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake = fake_for(ids[0])
    fake.record_fills, fake.fill_price = True, 100.0
    put = _add_write(client, owner)
    _, r = _command(client, owner, "fno", "buy")
    _wait(r.json()["run_id"])
    assert client.patch(f"/api/basket/{put['id']}", json={"side": "B"}, headers=owner).status_code == 200
    fake.fill_price = 120.0
    pv, r = _command(client, owner, "fno", "buy")
    assert pv["orders"][0]["quantity"] > 65 and not pv["orders"][0]["skip"]
    _wait(r.json()["run_id"])
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    [(held)] = trading.tradelab_book(user, "fno").values()
    net = fake.positions_map[(put["trading_symbol"], "NRML")].net_qty
    assert (held.qty, held.cost) == (net, 120.0) and net > 0


def test_average_cost_follows_partial_closes_and_flips(client, owner):
    acc = _accounts(client, owner, 1)[0]
    user = client.get("/api/auth/me", headers=owner).json()["id"]

    def order(kind, action, qty, price):
        with session_scope() as db:
            run = ExecutionRun(user_id=user, kind=kind, section="fno", trigger="command", trading_date=IN_SESSION.date(),
                               status="done")
            db.add(run)
            db.flush()
            db.add(OrderLog(user_id=user, run_id=run.id, account_id=acc, account_name="Acc 0", action=action,
                            symbol="NIFTY26OCT2025000PE", quantity=qty, price=price, status="success",
                            exchange_segment="nse_fo", product="NRML", section="fno"))

    def held():
        [(h)] = trading.tradelab_book(user, "fno").values()
        return h.qty, h.cost

    order("entry", "buy", 100, 100.0)
    order("exit", "sell", 50, 110.0)  # half closed
    order("entry", "buy", 50, 200.0)
    assert held() == (100, 150.0)  # 50 left at ₹100 and 50 more at ₹200
    order("entry", "sell", 300, 80.0)  # through zero: now 200 sold first at ₹80
    assert held() == (-200, 80.0)
    order("exit", "buy", 100, 60.0)  # half bought back
    order("entry", "sell", 100, 90.0)
    assert held() == (-200, 85.0)


def test_exit_on_one_row_closes_only_that_side(client, owner, clock):
    """A contract bought in one account and sold first in another shows as two rows; each Exit is its own."""
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 2)
    client.get("/api/accounts/live", headers=owner)
    put = _add_write(client, owner)
    ts, seg = put["trading_symbol"], put["exchange_segment"]
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    with session_scope() as db:
        run = ExecutionRun(user_id=user, kind="entry", section="fno", trigger="command", trading_date=IN_SESSION.date(),
                           status="done")
        db.add(run)
        db.flush()
        for acc, action in ((ids[0], "buy"), (ids[1], "sell")):
            db.add(OrderLog(user_id=user, run_id=run.id, account_id=acc, account_name="Acc", action=action, symbol=ts,
                            quantity=65, price=100.0, status="success", exchange_segment=seg, product="NRML",
                            section="fno"))
    fake_for(ids[0]).positions_map[(ts, "NRML")] = Position(seg, ts, "NRML", 65)
    fake_for(ids[1]).positions_map[(ts, "NRML")] = Position(seg, ts, "NRML", -65)
    pv = client.get("/api/sections/fno/preview", params={"action": "sell", "symbols": [ts], "opened": "S"},
                    headers=owner).json()
    assert [(o["account_id"], o["side"], o["quantity"]) for o in pv["orders"]] == [(ids[1], "B", 65)]
    r = client.post("/api/sections/fno/sell", json={"fingerprint": pv["fingerprint"], "symbols": [ts], "opened": "B"},
                    headers=owner)
    assert r.status_code == 409 and "changed" in r.json()["detail"]  # what was shown was the other row
    r = client.post("/api/sections/fno/sell", json={"fingerprint": pv["fingerprint"], "symbols": [ts], "opened": "S"},
                    headers=owner)
    assert r.status_code == 200, r.text
    _wait(r.json()["run_id"])
    assert fake_for(ids[0]).placed == [] and fake_for(ids[0]).positions_map[(ts, "NRML")].net_qty == 65
    assert fake_for(ids[1]).positions_map[(ts, "NRML")].net_qty == 0


def test_holding_pnl_uses_tradelabs_own_fill_price(client, owner, clock):
    clock.set(IN_SESSION)
    ids = _accounts(client, owner, 1)
    client.get("/api/accounts/live", headers=owner)
    fake = fake_for(ids[0])
    fake.record_fills, fake.fill_price = True, 100.0
    _add_etf(client, owner, "NIFTYBEES")
    _buy_holding(client, owner)
    qty = fake.positions_map[("NIFTYBEES-EQ", "CNC")].net_qty
    # Next day: the units are in demat together with the holder's own 1,000 bought at ₹50.
    del fake.positions_map[("NIFTYBEES-EQ", "CNC")]
    fake.holdings_list = [Holding("NIFTYBEES-EQ", "NIFTYBEES", "nse_cm", 1000 + qty, 1000 + qty,
                                  (50 * 1000 + 100 * qty) / (1000 + qty), 100.0)]
    rows = client.get("/api/sections/holding/positions", headers=owner).json()["rows"]
    assert [(x["quantity"], x["avg_price"], x["pnl"]) for x in rows] == [(qty, 100.0, 0.0)]


def test_old_intraday_times_are_pulled_inside_the_limits(client, owner):
    from app.models import SectionSettings

    user = client.get("/api/auth/me", headers=owner).json()["id"]
    client.get("/api/sections", headers=owner)
    with session_scope() as db:
        s = db.get(SectionSettings, (user, "intraday"))
        s.entry_time, s.exit_time = "15:10:00", "15:20:00"  # allowed by older versions
    data = client.get("/api/automation/settings", headers=owner).json()
    assert (data["entry_time"], data["exit_time"]) == ("15:00:00", "15:05:00")
    assert client.put("/api/automation/settings", json={"auto_exit": False}, headers=owner).status_code == 200
    r = client.put("/api/automation/settings", json={"entry_time": "15:04"}, headers=owner)
    assert r.status_code == 400 and "15:00" in r.json()["detail"]
