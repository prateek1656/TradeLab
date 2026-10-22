"""Every section, end to end, through the real Kotak adapter and Kotak's own reply formats.

The other trading tests use FakeKotak, which bypasses the code that reads Kotak's replies. Here the
real adapter talks to a simulated SDK (kotak_sim) that answers the way Kotak's servers do: rows when
there is something, Kotak's "No Data" report or "No holdings found for this client …" when an account
is empty, and refusals or gateway faults when asked to. The rule checked throughout: an empty account
trades like any other, and a reply TradeLab can't be sure of never turns into an order, a forgotten
position or an order taken as never placed.
"""

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import buy, confirm, kotak_payload

import kotak_sim
from app.broker import kotak
from app.broker.manager import broker_manager
from app.config import IST
from app.db import session_scope
from app.models import ExecutionRun, OrderLog
from app.services import trading
from kotak_sim import NO_DATA, PLACED_BUT, KotakSim

IN_SESSION = datetime(2026, 10, 14, 10, 0, tzinfo=IST)  # a Wednesday
SQUARE_OFF = datetime(2026, 10, 14, 14, 30, tzinfo=IST)
EVENING = datetime(2026, 10, 14, 20, 0, tzinfo=IST)
GATEWAY_PAGE = {"message": "Service Unavailable"}  # an API gateway's own answer, not Kotak's


@pytest.fixture
def sim(monkeypatch):
    s = KotakSim()
    broker_manager.kotak_factory = s.factory
    # The adapter waits for a fresh TOTP code between logins; tests don't need to.
    monkeypatch.setattr(kotak, "time", SimpleNamespace(time=time.time, sleep=lambda _s: None))
    kotak_sim.PRICES.clear()
    kotak_sim.TOKEN_PRICES.clear()
    return s


def _add(client, owner, sim, name, ucc, **acct):
    sim.account(ucc, **acct)
    r = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _family(client, owner, sim, **kanha):
    """A small account with an empty demat, and a big one whose holder owns shares of their own."""
    small = _add(client, owner, sim, "Kanha", "AB101", **({"cash": 200.0} | kanha))
    big = _add(client, owner, sim, "Mummy Ji", "CD202", cash=168389.84,
               demat={"ITBEES-EQ": [1801, 1801, 30.0], "GOLDBEES-EQ": [901, 901, 60.0]})
    return small, big


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


def _command(client, owner, section, action, symbols=None):
    params = {"action": action} | ({"symbols": symbols} if symbols else {})
    pv = client.get(f"/api/sections/{section}/preview", params=params, headers=owner)
    assert pv.status_code == 200, pv.text
    body = {"fingerprint": pv.json()["fingerprint"], "confirm_id": pv.json()["confirm_id"], "symbols": symbols}
    r = client.post(f"/api/sections/{section}/{action}", json=body, headers=owner)
    return pv.json(), r


def _preview(client, owner, section, action):
    """What the confirmation screen shows, without answering "Yes"."""
    pv = client.get(f"/api/sections/{section}/preview", params={"action": action}, headers=owner)
    assert pv.status_code == 200, pv.text
    return pv.json()


def _run(client, owner, section, action, symbols=None):
    pv, r = _command(client, owner, section, action, symbols)
    assert r.status_code == 200, r.text
    return pv, _wait(r.json()["run_id"])


def _add_etf(client, owner, symbol):
    r = client.post("/api/basket", json={"section": "holding", "symbol": symbol, "trading_symbol": f"{symbol}-EQ",
                                         "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text


def _add_contract(client, owner, section, query, side="B"):
    c = client.get("/api/stocks/contracts", params={"section": section, "q": query}, headers=owner).json()["results"][0]
    r = client.post("/api/basket", json={"section": section, "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "side": side, "sizing_mode": "lots",
                                         "sizing_value": 1}, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()


def _alerts(client, owner):
    return client.get("/api/alerts", headers=owner).json()


def _logs(**flt):
    with session_scope() as db:
        return db.find(OrderLog, flt, sort=[("id", 1)], track=False)


# ----------------------------------------------------------------------------- the adapter on its own


@pytest.mark.parametrize("reply", [
    NO_DATA,
    {"stCode": "5203", "errMsg": "No Data", "stat": "Not_Ok"},
    {"stat": "Not_Ok", "emsg": "No Data"},
    kotak_sim.no_holdings("AB101", "error"),
    kotak_sim.no_holdings("AB101", "emsg"),
    kotak_sim.no_holdings("AB101", "errMsg"),
    {"stat": "Not_Ok", "emsg": "No positions found for this client AB101"},
    {"stat": "Not_Ok", "emsg": "No orders found"},
    {"stat": "Ok", "stCode": 200, "data": []},
    {"stat": "Ok", "stCode": 200},
    [],
])
def test_these_replies_mean_nothing_is_there(reply):
    assert kotak._report_rows(reply, "Fetching positions") == []


@pytest.mark.parametrize("reply", [
    GATEWAY_PAGE,
    {"Message": "Endpoint request timed out"},
    {},
    None,
    "<html>502 Bad Gateway</html>",
    {"stat": "Not_Ok", "emsg": "Session not found"},
    {"stat": "Not_Ok", "emsg": "No valid session found, please login"},
    {"stat": "Not_Ok", "emsg": "User not found"},
    {"stat": "Not_Ok", "emsg": "Client not found"},
    {"stat": "Not_Ok", "emsg": "No access token found"},
    {"stat": "Not_Ok", "emsg": "unauthorized", "stCode": 100008},
    {"stat": "Not_Ok", "emsg": "Invalid session, please re-login to continue", "stCode": 1003},
    {"stat": "Not_Ok", "emsg": "Too many requests to the API", "stCode": 429},
    {"stat": "Not_Ok", "emsg": "Trade API service is unavailable", "stCode": 503},
    {"stat": "Not_Ok", "emsg": "Not able to communicate with OMS", "stCode": 502},
    {"stCode": 5203, "errMsg": "No Data", "stat": "Not_Ok", "data": [{"trdSym": "SBIN-EQ"}]},
    {"error": [{"code": "401", "message": "Unauthorized"}], "errMsg": "No Data", "stCode": 5203, "stat": "Not_Ok"},
    {"Error": "Unexpected response format", "StatusCode": 503, "ResponseText": "no data"},
    {"stat": "Ok", "stCode": 200, "data": {"rows": []}},
])
def test_these_replies_are_never_read_as_empty(reply):
    with pytest.raises(Exception):  # noqa: B017 - a BrokerError or a SessionExpired, never []
        kotak._report_rows(reply, "Fetching positions")


def test_a_position_reply_in_an_unknown_layout_is_an_error_not_nothing_open(sim):
    acct = sim.account("AB101")
    b = sim.factory({"ucc": "AB101"})
    acct.faults["positions"] = {"stat": "ok", "stCode": 200, "data": [{"trdSym": "SBIN-EQ", "prod": "MIS",
                                                                       "netQty": "10"}]}
    with pytest.raises(Exception, match="unknown format"):
        b.positions()
    acct.faults["order_report"] = {"stat": "Ok", "stCode": 200, "data": [{"nOrdNo": "1", "ordSt": "complete"}]}
    with pytest.raises(Exception, match="unknown format"):
        b.orders()
    acct.faults["holdings"] = {"data": [{"symbol": "ITBEES", "series": "EQ", "units": 5}]}
    with pytest.raises(Exception, match="unknown format"):
        b.holdings()
    acct.faults["limits"] = {"stat": "Ok", "stCode": 200}
    with pytest.raises(Exception, match="no balance"):
        b.funds()


@pytest.mark.parametrize("reply", [
    GATEWAY_PAGE,
    {"stat": "Ok", "stCode": 200},
    {"stat": "Not_Ok", "emsg": "Gateway timeout, trade API is unreachable", "stCode": 504},
    {"stat": "Not_Ok", "emsg": "Unexpected error", "stCode": 500},
    None,
])
def test_an_order_reply_without_an_order_number_or_a_refusal_is_unknown_not_refused(sim, reply):
    acct = sim.account("AB101")
    b = sim.factory({"ucc": "AB101"})
    acct.faults["place_order"] = reply
    with pytest.raises(kotak.OrderOutcomeUnknown):
        b.place_market_order("nse_cm", "SBIN-EQ", "B", 1)


def test_kotak_refusals_are_still_refusals(sim):
    acct = sim.account("AB101")
    b = sim.factory({"ucc": "AB101"})
    acct.faults["place_order"] = {"stat": "Not_Ok", "emsg": "RMS:Margin Exceeds,Required:500.00", "stCode": 1009}
    res = b.place_market_order("nse_cm", "SBIN-EQ", "B", 1)
    assert not res.ok and "Margin Exceeds" in res.message
    acct.faults["place_order"] = {"stat": "Not_Ok", "emsg": "unauthorized", "stCode": 100008}
    res = b.place_market_order("nse_cm", "SBIN-EQ", "B", 1)
    assert not res.ok and "isn't whitelisted" in res.message


def test_a_cancel_counts_only_when_kotak_confirms_it(sim):
    acct = sim.account("AB101")
    b = sim.factory({"ucc": "AB101"})
    assert b.cancel_order("123").ok
    acct.faults["cancel_order"] = GATEWAY_PAGE
    assert not b.cancel_order("123").ok


# ----------------------------------------------------------------------------- Holding


@pytest.mark.parametrize("shape", ["error", "emsg", "errMsg"])
def test_holding_bought_and_sold_the_same_day_in_an_empty_demat(client, owner, clock, sim, shape):
    """The reported case: Kanha's demat held nothing, so Kotak answered "No holdings found …"."""
    clock.set(IN_SESSION)
    small, big = _family(client, owner, sim, empty_holdings_shape=shape)
    kotak_sim.PRICES["ITBEES-EQ"] = kotak_sim.TOKEN_PRICES["19084"] = 35.90
    _add_etf(client, owner, "ITBEES")
    _, run = _run(client, owner, "holding", "buy")
    assert run.status == "done", run.summary
    kanha, mummy = sim.account("AB101"), sim.account("CD202")
    bought_small, bought_big = kanha.net("ITBEES-EQ", "CNC"), mummy.net("ITBEES-EQ", "CNC")
    assert bought_small == 5 and bought_big > 0

    pv, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert sorted((o["account_name"], o["quantity"], o["skip"]) for o in pv["orders"]) == [
        ("Kanha", 5, None), ("Mummy Ji", bought_big, None)]
    assert kanha.net("ITBEES-EQ", "CNC") == 0 and mummy.net("ITBEES-EQ", "CNC") == 0
    # The holder's own units are never sold.
    assert mummy.demat["ITBEES-EQ"][0] == 1801 and mummy.demat["GOLDBEES-EQ"][0] == 901
    assert _alerts(client, owner) == []


def test_holding_sold_on_a_later_day_from_the_demat(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small, big = _family(client, owner, sim)
    kotak_sim.PRICES["ITBEES-EQ"] = kotak_sim.TOKEN_PRICES["19084"] = 35.90
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha, mummy = sim.account("AB101"), sim.account("CD202")
    bought_big = mummy.net("ITBEES-EQ", "CNC")
    trading.true_up_all()  # the evening check must not forget anything
    for a in (kanha, mummy):
        a.settle_overnight()  # next day: the units are in the demat, positions and order books are empty
    clock.set(IN_SESSION + timedelta(days=1))
    trading.true_up_all()
    pv, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert sorted((o["account_name"], o["quantity"]) for o in pv["orders"]) == [("Kanha", 5), ("Mummy Ji", bought_big)]
    assert kanha.demat["ITBEES-EQ"][0] == 0 and mummy.demat["ITBEES-EQ"][0] == 1801


def test_holding_with_nothing_bought_sells_nothing_and_raises_no_error(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _family(client, owner, sim)
    _add_etf(client, owner, "ITBEES")
    pv = _preview(client, owner, "holding", "sell")
    assert pv["orders"] == []
    positions = client.get("/api/sections/holding/positions", headers=owner).json()
    assert positions["rows"] == [] and positions["errors"] == []


def test_holding_sale_waits_when_kotak_cannot_be_read(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small, _ = _family(client, owner, sim)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    kanha.faults["positions"] = GATEWAY_PAGE
    pv = _preview(client, owner, "holding", "sell")
    (row,) = [o for o in pv["orders"] if o["account_name"] == "Kanha"]
    assert row["skip"] and "Could not read" in row["skip"] and row["quantity"] > 0
    assert not kanha.sold()
    # TradeLab still remembers the units: once Kotak answers again, they are sold.
    kanha.faults.clear()
    _run(client, owner, "holding", "sell")
    assert kanha.net("ITBEES-EQ", "CNC") == 0


# ----------------------------------------------------------------------------- F&O and Commodity


@pytest.mark.parametrize("side", ["B", "S"])
def test_fno_opens_and_exits_in_accounts_with_empty_books(client, owner, clock, sim, side):
    clock.set(IN_SESSION)
    a = _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    b = _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut" if side == "B" else "nifty 25000 pe", side)
    ts = item["trading_symbol"]
    pv, run = _run(client, owner, "fno", "buy")
    assert run.status == "done", run.summary
    assert all(o["skip"] is None for o in pv["orders"]) and len(pv["orders"]) == 2
    lot = item["lot_size"]
    sign = 1 if side == "B" else -1
    assert sim.account("AB101").net(ts, "NRML") == sign * lot and sim.account("CD202").net(ts, "NRML") == sign * lot

    pv, run = _run(client, owner, "fno", "sell")
    assert run.status == "done", run.summary
    assert sim.account("AB101").net(ts, "NRML") == 0 and sim.account("CD202").net(ts, "NRML") == 0
    assert _alerts(client, owner) == []
    assert a != b


def test_fno_exit_on_a_later_day_closes_the_carried_position(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    _run(client, owner, "fno", "buy")
    acct = sim.account("AB101")
    trading.true_up_all()
    acct.settle_overnight()
    clock.set(IN_SESSION + timedelta(days=1))
    trading.true_up_all()
    pv, run = _run(client, owner, "fno", "sell")
    assert run.status == "done", run.summary
    assert [o["quantity"] for o in pv["orders"]] == [item["lot_size"]]
    assert acct.net(item["trading_symbol"], "NRML") == 0


def test_commodity_opens_and_exits_in_an_empty_account(client, owner, clock, sim):
    _add(client, owner, sim, "Acc A", "AB101", cash=50000.0)
    client.put("/api/sections/commodity", json={"max_lots": 1}, headers=owner)
    gold = _add_contract(client, owner, "commodity", "goldm")
    clock.set(EVENING)
    pv, run = _run(client, owner, "commodity", "buy")
    assert run.status == "done", run.summary
    assert pv["orders"][0]["skip"] is None
    acct = sim.account("AB101")
    assert acct.net(gold["trading_symbol"], "NRML") == gold["lot_size"]
    _, run = _run(client, owner, "commodity", "sell")
    assert run.status == "done", run.summary
    assert acct.net(gold["trading_symbol"], "NRML") == 0


def test_contract_exit_sends_nothing_while_positions_cannot_be_read(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    _run(client, owner, "fno", "buy")
    acct = sim.account("AB101")
    for fault in (GATEWAY_PAGE, {"stat": "Not_Ok", "emsg": "Session not found"}, {}):
        acct.faults["positions"] = fault
        pv = _preview(client, owner, "fno", "sell")
        (row,) = pv["orders"]
        assert row["skip"] and "Could not read" in row["skip"], fault
    assert [c for c in acct.calls if c[0] == "place_order"][1:] == []  # only the buy was ever sent
    acct.faults.clear()
    _run(client, owner, "fno", "sell")
    assert acct.net(item["trading_symbol"], "NRML") == 0


def test_a_buy_skips_an_account_whose_positions_cannot_be_checked(client, owner, clock, sim):
    """Selling first next to the holder's own contract would close it, so an unreadable account is skipped."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "fno", "nifty 25000 pe", "S")
    sim.account("AB101").faults["positions"] = GATEWAY_PAGE
    pv, run = _run(client, owner, "fno", "buy")
    rows = {o["account_name"]: o for o in pv["orders"]}
    assert rows["Acc A"]["skip"] and "open positions" in rows["Acc A"]["skip"]
    assert rows["Acc B"]["skip"] is None
    assert not [c for c in sim.account("AB101").calls if c[0] == "place_order"]


# ----------------------------------------------------------------------------- Intraday


def test_intraday_buy_and_square_off_with_an_account_that_never_got_shares(client, owner, clock, sim):
    """A rejected buy leaves an account with empty books; its square-off must not raise a false alarm."""
    small, big = _family(client, owner, sim)
    r = client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                         "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text
    kanha, mummy = sim.account("AB101"), sim.account("CD202")
    kanha.reject_in_book = "RMS:Margin Exceeds,Required:100.00,Available:0.00"
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    buy(client, owner)
    assert kanha.net("SBIN-EQ", "MIS") == 0 and mummy.net("SBIN-EQ", "MIS") > 0
    alerts = _alerts(client, owner)
    assert [a["accounts"] for a in alerts] == [["Kanha"]]  # the late margin rejection is reported

    clock.set(SQUARE_OFF)
    run_id = trading.run_exit(user, "scheduled", section="intraday")
    run = _wait(run_id)
    assert run.status == "done", run.summary
    assert mummy.net("SBIN-EQ", "MIS") == 0
    assert not [a for a in _alerts(client, owner) if a["kind"] == "exit"]


def test_square_off_with_unreadable_positions_reports_it_and_closes_nothing_blindly(client, owner, clock, sim):
    small, big = _family(client, owner, sim)
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                     "exchange_segment": "nse_cm"}, headers=owner)
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    buy(client, owner)
    mummy = sim.account("CD202")
    held = mummy.net("SBIN-EQ", "MIS")
    mummy.faults["positions"] = GATEWAY_PAGE
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert run.status != "done"
    assert mummy.net("SBIN-EQ", "MIS") == held  # nothing was sent on a guess
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Mummy Ji"]]


def test_panic_button_on_empty_accounts_raises_no_false_alarm(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _family(client, owner, sim)
    r = client.post("/api/automation/square-off-all", headers=owner)
    run = _wait(r.json()["run_id"])
    assert run.status == "done", run.summary
    assert _alerts(client, owner) == []


# ----------------------------------------------------------------------------- lost and unclear replies


def test_an_order_whose_reply_was_lost_is_found_and_counted(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    acct.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]
    _, run = _run(client, owner, "fno", "buy")
    (row,) = _logs(action="buy")
    assert row.status == "success" and row.order_id  # found in the order book by its tag
    assert acct.net(item["trading_symbol"], "NRML") == item["lot_size"]
    # TradeLab counts it as its own, so Exit closes it.
    _run(client, owner, "fno", "sell")
    assert acct.net(item["trading_symbol"], "NRML") == 0


def test_a_gateway_timeout_that_did_place_the_order_is_not_taken_as_refused(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add_etf(client, owner, "ITBEES")
    acct = sim.account("AB101")
    acct.faults["place_order"] = [(PLACED_BUT, {"stat": "Not_Ok", "emsg": "Gateway timeout", "stCode": 504})]
    _run(client, owner, "holding", "buy")
    (row,) = _logs(action="buy")
    assert row.status == "success"
    held = acct.net("ITBEES-EQ", "CNC")
    assert held > 0
    pv = _preview(client, owner, "holding", "sell")
    assert [o["quantity"] for o in pv["orders"]] == [held]


def test_an_unclear_order_is_marked_not_placed_only_from_a_readable_order_book(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(0))
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    acct.faults["place_order"] = [{"message": "Endpoint request timed out"}]  # and it never reached Kotak
    acct.faults["order_report"] = GATEWAY_PAGE  # the order book can't be read either
    _run(client, owner, "fno", "buy")
    (row,) = _logs(action="buy")
    assert row.status == "unknown"  # never "not placed" on a guess
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    trading.reconcile_orders(user)
    assert _logs(action="buy")[0].status == "unknown"
    acct.faults.clear()  # the book answers: empty ("No Data"), so the order really wasn't placed
    trading.reconcile_orders(user)
    row = _logs(action="buy")[0]
    assert row.status == "failed" and "Not placed" in row.message


def test_an_expired_session_is_renewed_and_the_read_retried(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    _run(client, owner, "fno", "buy")
    acct = sim.account("AB101")
    logins = len([c for c in acct.calls if c[0] == "totp_login"])
    acct.faults["positions"] = [{"stat": "Not_Ok", "emsg": "Invalid session, please re-login to continue",
                                 "stCode": 1003}]
    pv, run = _run(client, owner, "fno", "sell")
    assert run.status == "done", run.summary
    assert len([c for c in acct.calls if c[0] == "totp_login"]) == logins + 1
    assert acct.net(item["trading_symbol"], "NRML") == 0


def test_a_balance_kotak_did_not_send_is_never_taken_for_zero(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add_etf(client, owner, "ITBEES")
    sim.account("AB101").faults["limits"] = {"stat": "Ok", "stCode": 200}
    pv = _preview(client, owner, "holding", "buy")
    (row,) = pv["orders"]
    assert row["skip"] == trading.NO_FUNDS and not row["funds_short"]


def test_a_confirmed_intraday_buy_survives_empty_books_everywhere(client, owner, clock, sim):
    """Before the first trade of the day every account's books are empty; the 09:15 buy must still go out."""
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045", "exchange_segment": "nse_cm"},
                headers=owner)
    confirm(client, owner)
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    trading.run_entry(user, "manual")
    assert sim.account("AB101").net("SBIN-EQ", "MIS") > 0 and sim.account("CD202").net("SBIN-EQ", "MIS") > 0
    with session_scope() as db:
        (run,) = db.find(ExecutionRun, {"kind": "entry"})
        assert run.status == "done", run.summary


# ----------------------------------------------------------------------------- client codes in messages


@pytest.mark.parametrize("ucc", ["IP3K7", "12FA9", "AUTH7", "TOKEN1", "ERR0R", "LOGIN5", "LIMIT9"])
def test_a_client_code_that_looks_like_a_word_changes_nothing(client, owner, clock, sim, ucc):
    """Kotak quotes the client code in its empty-demat answer; IP3K7 or 12FA9 must not read as "IP" or "2FA"."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Kanha", ucc, cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    acct = sim.account(ucc)
    held = acct.net("ITBEES-EQ", "CNC")
    assert held > 0
    pv, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert [(o["quantity"], o["skip"]) for o in pv["orders"]] == [(held, None)]
    assert acct.net("ITBEES-EQ", "CNC") == 0
    assert len([c for c in acct.calls if c[0] == "totp_login"]) == 1  # no needless re-login either


@pytest.mark.parametrize("reply, empty", [
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client IP3K7"}, True),
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client 12FA9."}, True),
    ({"stat": "Not_Ok", "emsg": "No positions found for client AUTH7"}, True),
    ({"stat": "Not_Ok", "emsg": "No Data", "stCode": 5203}, True),
    ({"stat": "Not_Ok", "emsg": "Session expired, no data"}, False),
    ({"stat": "Not_Ok", "emsg": "No data due to an upstream failure"}, False),
    ({"stat": "Not_Ok", "emsg": "Request failed: no data"}, False),
    ({"stat": "Not_Ok", "emsg": "Invalid session for client 12FA9", "stCode": 5203}, False),
])
def test_empty_answers_are_matched_whole_without_the_client_code(reply, empty):
    assert kotak._is_no_data(reply) is empty


def test_a_session_error_quoting_a_client_code_is_still_a_session_error(sim):
    acct = sim.account("12FA9")
    b = sim.factory({"ucc": "12FA9"})
    acct.faults["positions"] = {"stat": "Not_Ok", "emsg": "Invalid session for client 12FA9", "stCode": 1003}
    with pytest.raises(kotak.SessionExpired):
        b.positions()
    acct.faults["positions"] = {"stat": "Not_Ok", "emsg": "Not able to communicate with OMS for client 12FA9"}
    with pytest.raises(kotak.BrokerError) as exc:
        b.positions()
    assert not isinstance(exc.value, kotak.SessionExpired)


# ----------------------------------------------------------------------------- orders still working


def test_a_part_filled_order_is_never_counted_twice(client, owner, clock, sim):
    """2 lots asked, 1 filled, the rest still working: looking at Sell must not write anything off, and Exit
    cancels the rest and closes exactly the lot that filled."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.partial_next = lot
    _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == lot
    (buy_row,) = _logs(action="buy")
    assert buy_row.quantity == 2 * lot and not buy_row.final  # still working at Kotak

    pv = _preview(client, owner, "fno", "sell")
    assert [(o["quantity"], o["skip"]) for o in pv["orders"]] == [(lot, None)]  # what filled, not what was asked
    trading.true_up_all()
    assert _logs(status="adjusted") == []  # nothing written off while the order works

    pv, run = _run(client, owner, "fno", "sell")
    assert run.status == "done", run.summary
    working = [o for o in acct.orders if o["trnsTp"] == "B"]
    assert [o["ordSt"] for o in working] == ["cancelled"]  # the unfilled lot was stopped first
    assert acct.net(ts, "NRML") == 0
    assert [(o["trnsTp"], o["qty"]) for o in acct.orders if o["trnsTp"] == "S"] == [("S", lot)]
    assert _logs(status="adjusted") == []
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    assert trading.tradelab_open(user, "fno") == {}


def test_exit_never_takes_the_holders_lot_while_an_order_cannot_be_stopped(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.partial_next = lot
    _run(client, owner, "fno", "buy")
    # The holder already had a lot of the same contract of their own, carried from yesterday.
    acct.open_positions[("nse_fo", ts, "NRML")].cf_buy += lot
    assert acct.net(ts, "NRML") == 2 * lot
    acct.faults["cancel_order"] = GATEWAY_PAGE  # TradeLab's working order can't be cancelled
    pv, run = _run(client, owner, "fno", "sell")
    # Exactly the lot TradeLab got filled is closed; the holder's own lot stays, whatever Kotak's total says.
    assert [(o["trnsTp"], o["qty"]) for o in acct.orders if o["trnsTp"] == "S"] == [("S", lot)]
    assert acct.net(ts, "NRML") == lot
    assert run.status != "done"  # the cancel that failed is reported
    # If the rest of TradeLab's order fills later, TradeLab still counts it as its own.
    working = next(o for o in acct.orders if o["trnsTp"] == "B" and o["ordSt"] == "open")
    working.update(ordSt="complete", fldQty=2 * lot)
    acct.open_positions[("nse_fo", ts, "NRML")].buy += lot
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    trading.reconcile_orders(user)
    assert list(trading.tradelab_open(user, "fno").values()) == [lot]


def test_a_part_filled_delivery_buy_sells_only_what_filled(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _family(client, owner, sim)
    kotak_sim.PRICES["ITBEES-EQ"] = kotak_sim.TOKEN_PRICES["19084"] = 35.90
    _add_etf(client, owner, "ITBEES")
    kanha, mummy = sim.account("AB101"), sim.account("CD202")
    kanha.partial_next = 3  # 5 asked, 3 filled
    _run(client, owner, "holding", "buy")
    assert kanha.net("ITBEES-EQ", "CNC") == 3
    _preview(client, owner, "holding", "sell")
    trading.true_up_all()
    assert _logs(status="adjusted") == []
    pv, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert kanha.net("ITBEES-EQ", "CNC") == 0 and mummy.net("ITBEES-EQ", "CNC") == 0
    assert mummy.demat["ITBEES-EQ"][0] == 1801


# ----------------------------------------------------------------------------- square-off and other orders


def _intraday_bought(client, owner, sim):
    _family(client, owner, sim)
    r = client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                         "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text
    buy(client, owner)
    return client.get("/api/auth/me", headers=owner).json()["id"]


def test_square_off_waits_for_a_stop_loss_it_cannot_cancel(client, owner, clock, sim):
    """Closing next to the holder's working stop-loss would flip the position when the stop-loss fires."""
    user = _intraday_bought(client, owner, sim)
    mummy = sim.account("CD202")
    held = mummy.net("SBIN-EQ", "MIS")
    sl = mummy.add_open_order("SBIN-EQ", "nse_cm", "MIS", "S", held)
    mummy.faults["cancel_order"] = GATEWAY_PAGE
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert run.status != "done"
    assert mummy.net("SBIN-EQ", "MIS") == held and mummy.order(sl)["ordSt"] == "trigger pending"
    assert not [o for o in mummy.orders if o["trnsTp"] == "S" and o["nOrdNo"] != sl]  # no close sent
    with session_scope() as db:
        msgs = [r.message for r in db.find(OrderLog, {"action": "sell", "status": "failed"})]
    assert any("couldn't be cancelled" in (m or "") for m in msgs)
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Mummy Ji"]]

    # Once the stop-loss can be cancelled, the next square-off cancels it and only then closes.
    mummy.faults.clear()
    run = _wait(trading.run_exit(user, "catchup", section="intraday"))
    assert run.status == "done", run.summary
    assert mummy.order(sl)["ordSt"] == "cancelled" and mummy.net("SBIN-EQ", "MIS") == 0


def test_square_off_cancels_a_working_order_then_closes(client, owner, clock, sim):
    user = _intraday_bought(client, owner, sim)
    mummy = sim.account("CD202")
    sl = mummy.add_open_order("SBIN-EQ", "nse_cm", "MIS", "S", mummy.net("SBIN-EQ", "MIS"))
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert run.status == "done", run.summary
    assert mummy.order(sl)["ordSt"] == "cancelled" and mummy.net("SBIN-EQ", "MIS") == 0


# ----------------------------------------------------------------------------- more unclear order replies


@pytest.mark.parametrize("reply", [
    {"Error": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")},
    {"error": [{"code": "504", "message": "Gateway Time-out"}], "stat": "Not_Ok"},
    {"error": [{"code": "502", "message": "Not able to communicate with OMS"}], "stat": "Not_Ok"},
])
def test_unreadable_or_gateway_order_replies_are_unknown(sim, reply):
    acct = sim.account("AB101")
    b = sim.factory({"ucc": "AB101"})
    acct.faults["place_order"] = reply
    with pytest.raises(kotak.OrderOutcomeUnknown):
        b.place_market_order("nse_cm", "SBIN-EQ", "B", 1)


# ----------------------------------------------------------------------------- the T+1 gap


def test_delivery_units_are_not_written_off_in_the_settlement_gap(client, owner, clock, sim):
    """Kotak may clear the day's positions before delivery buys show in the demat."""
    clock.set(IN_SESSION)
    small, _ = _family(client, owner, sim)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    kanha.clear_day_positions()  # in neither positions nor demat for a while
    trading.true_up_all()
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    assert _logs(status="adjusted") == []
    assert [h.qty for k, h in trading.tradelab_book(user, "holding").items() if k.account_id == small] == [bought]
    kanha.demat["ITBEES-EQ"] = [bought, bought, 100.0]  # settled
    clock.set(IN_SESSION + timedelta(days=1))
    pv, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert ("Kanha", bought) in [(o["account_name"], o["quantity"]) for o in pv["orders"]]


# ----------------------------------------------------------------------------- review 2: closing must still work


def _part_filled_contract(client, owner, sim, **acct):
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0, **acct)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    sim.account("AB101").partial_next = item["lot_size"]
    _run(client, owner, "fno", "buy")
    return item["trading_symbol"], item["lot_size"], sim.account("AB101")


def test_exit_closes_what_filled_even_while_kotak_is_slow_to_cancel(client, owner, clock, sim):
    clock.set(IN_SESSION)
    ts, lot, acct = _part_filled_contract(client, owner, sim)
    acct.cancel_lag = 5  # Kotak accepts the cancel but its book shows the order working a while longer
    _, run = _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == 0
    assert [(o["trnsTp"], o["qty"]) for o in acct.orders if o["trnsTp"] == "S"] == [("S", lot)]


def test_panic_closes_what_filled_even_while_kotak_is_slow_to_cancel(client, owner, clock, sim):
    clock.set(IN_SESSION)
    ts, lot, acct = _part_filled_contract(client, owner, sim)
    acct.cancel_lag = 5
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary


def test_an_order_the_book_never_showed_blocks_exit_but_not_the_panic_button(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    acct.faults["order_report"] = GATEWAY_PAGE  # the order book can't be read at all
    _run(client, owner, "fno", "buy")
    (row,) = _logs(action="buy")
    assert row.status == "success" and not row.final and row.filled is None
    ts = item["trading_symbol"]
    pv, _ = _command(client, owner, "fno", "sell")
    _wait_idle()
    assert pv["orders"][0]["note"] and "hasn't confirmed" in pv["orders"][0]["note"]
    assert acct.net(ts, "NRML") == item["lot_size"]  # Exit sent nothing it couldn't size safely
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary  # the panic button closes by what Kotak shows


def _wait_idle(timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_scope() as db:
            if not db.count(ExecutionRun, {"status": "running"}):
                return
        time.sleep(0.02)
    raise AssertionError("a run did not finish")


@pytest.mark.parametrize("reply, empty", [
    ({"stat": "Not_Ok", "emsg": "No positions found for this account due to a technical error"}, False),
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client AB101, service unavailable"}, False),
    ({"stat": "Not_Ok", "emsg": "No orders found for this client AB101 due to session timeout"}, False),
    ({"stat": "Not_Ok", "emsg": "No holdings available in your account"}, True),
    ({"stat": "Not_Ok", "emsg": "No data found for the given request"}, True),
    ({"stat": "Not_Ok", "emsg": "No orders found for today"}, True),
    ({"stat": "Not_Ok", "emsg": "No holdings found for this account"}, True),
    ({"stat": "Not_Ok", "emsg": "No holdings found for client: AB101."}, True),
    ({"stCode": 5203, "errMsg": "InvalidSession", "stat": "Not_Ok"}, False),
])
def test_words_after_the_client_code_are_still_read(reply, empty):
    assert kotak._is_no_data(reply) is empty


@pytest.mark.parametrize("msg", ["Access denied for user AB123: session expired", "Session_Expired", "sessionExpired",
                                 "InvalidSession", "Sessions expired, please login"])
def test_session_errors_in_any_spelling_renew_the_login(msg):
    with pytest.raises(kotak.SessionExpired):
        kotak._raise_for({"stat": "Not_Ok", "emsg": msg}, "Fetching positions")


def test_a_delivery_record_older_than_settlement_is_written_off_before_the_holder_buys_again(client, owner, clock, sim):
    """Bought Monday, sold by the holder Tuesday, nothing read that day: by Wednesday Kotak shows none, so the
    record goes; Thursday's shares bought by the holder are then never sold by TradeLab."""
    clock.set(IN_SESSION)
    small, _ = _family(client, owner, sim)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    assert bought > 0
    with session_scope() as db:  # Monday's buy, from TradeLab's point of view three days ago
        for row in db.find(OrderLog, {"account_id": small, "action": "buy"}):
            row.created_at = row.created_at - timedelta(days=3)
    kanha.clear_day_positions()  # sold in the Kotak app on an earlier day: in neither positions nor demat
    trading.true_up_all()
    assert [a.quantity for a in _logs(status="adjusted")] == [bought]
    kanha.place_order("nse_cm", "CNC", "0", "MKT", "5", "DAY", "ITBEES-EQ", "B")  # the holder's own five
    pv = _preview(client, owner, "holding", "sell")
    assert [o for o in pv["orders"] if o["account_name"] == "Kanha"] == []
    assert kanha.net("ITBEES-EQ", "CNC") == 5  # only the holder's own five, untouched


def test_settlement_gap_with_some_of_the_same_etf_already_in_the_demat(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0, demat={"ITBEES-EQ": [2, 2, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    kanha.clear_day_positions()  # the demat still shows only the holder's 2
    trading.true_up_all()
    assert _logs(status="adjusted") == []
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    assert [h.qty for k, h in trading.tradelab_book(user, "holding").items() if k.account_id == small] == [bought]


def test_a_retry_square_off_remembers_a_stop_loss_it_could_not_cancel(client, owner, clock, sim):
    user = _intraday_bought(client, owner, sim)
    mummy = sim.account("CD202")
    held = mummy.net("SBIN-EQ", "MIS")
    sl = mummy.add_open_order("SBIN-EQ", "nse_cm", "MIS", "S", held)
    mummy.faults["cancel_order"] = GATEWAY_PAGE
    clock.set(SQUARE_OFF)
    _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert mummy.net("SBIN-EQ", "MIS") == held
    # The retry a minute later can't read the order book at all: it must still not close next to the stop-loss.
    mummy.faults["order_report"] = GATEWAY_PAGE
    clock.set(SQUARE_OFF + timedelta(minutes=1))
    run = _wait(trading.run_exit(user, "catchup", section="intraday"))
    assert run.status != "done"
    assert mummy.net("SBIN-EQ", "MIS") == held and mummy.order(sl)["ordSt"] == "trigger pending"
