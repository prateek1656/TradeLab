"""Every section, end to end, through the real Kotak adapter and Kotak's own reply formats.

The other trading tests use FakeKotak, which bypasses the code that reads Kotak's replies. Here the
real adapter talks to a simulated SDK (kotak_sim) that answers the way Kotak's servers do: rows when
there is something, Kotak's "No Data" report or "No holdings found for this client …" when an account
is empty, and refusals or gateway faults when asked to. The rule checked throughout: an empty account
trades like any other, and a reply TradeLab can't be sure of never turns into an order, a forgotten
position or an order taken as never placed.
"""

import copy
import threading
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
from dataclasses import replace as _replace

from app.services import calendar, contracts, trading
from kotak_sim import _Pos
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


def _add_contract(client, owner, section, query, side="B", lots=1):
    c = client.get("/api/stocks/contracts", params={"section": section, "q": query}, headers=owner).json()["results"][0]
    r = client.post("/api/basket", json={"section": section, "symbol": c["symbol"], "trading_symbol": c["trading_symbol"],
                                         "exchange_segment": c["exchange_segment"], "side": side, "sizing_mode": "lots",
                                         "sizing_value": lots}, headers=owner)
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
    assert kotak._report_rows(reply, "Fetching positions", "AB101") == []


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


@pytest.mark.parametrize("reply, ucc, empty", [
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client IP3K7"}, "IP3K7", True),
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client 12FA9."}, "12FA9", True),
    ({"stat": "Not_Ok", "emsg": "No positions found for client AUTH7"}, "AUTH7", True),
    ({"stat": "Not_Ok", "emsg": "No Data", "stCode": 5203}, "AB101", True),
    ({"stat": "Not_Ok", "emsg": "Session expired, no data"}, "AB101", False),
    ({"stat": "Not_Ok", "emsg": "No data due to an upstream failure"}, "AB101", False),
    ({"stat": "Not_Ok", "emsg": "Request failed: no data"}, "AB101", False),
    ({"stat": "Not_Ok", "emsg": "Invalid session for client 12FA9", "stCode": 5203}, "12FA9", False),
    # Only this account's own client code is taken off; any other last word is part of the message.
    ({"stat": "Not_Ok", "emsg": "No positions found for this account temporarily"}, "AB101", False),
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client XY999"}, "AB101", False),
    ({"stat": "Not_Ok", "emsg": "No holdings found for this client AB101"}, None, False),
])
def test_empty_answers_are_matched_whole_without_the_client_code(reply, ucc, empty):
    assert kotak._is_no_data(reply, ucc) is empty


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


def test_an_order_the_book_never_showed_is_never_closed_by_guesswork(client, owner, clock, sim):
    """Kotak took TradeLab's buy but its order book can't be read. Neither Exit nor the panic button closes it
    by what Kotak shows (that could be the holder's own contract); both say why. Once the book reads again,
    the panic button closes it."""
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
    assert acct.net(ts, "NRML") == item["lot_size"], run.summary  # nor did the panic button
    assert run.status != "done" and any("hasn't confirmed" in (r.message or "") for r in _logs(run_id=run.id))
    del acct.faults["order_report"]  # the book reads again
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary


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
    assert kotak._is_no_data(reply, "AB101") is empty


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


# ----------------------------------------------------------------------------- review 3


def _trading_days_back(n: int) -> timedelta:
    """How far back the n-th previous trading day is (record times use the real clock)."""
    today = datetime.now(IST).date()
    day = today
    for _ in range(n):
        day -= timedelta(days=1)
        while not trading.is_trading_day(day):
            day -= timedelta(days=1)
    return today - day


def _age_buys(account_id: int, by: timedelta) -> None:
    """Make an account's buys look `by` older (as if seen on a later day)."""
    with session_scope() as db:
        for row in db.find(OrderLog, {"account_id": account_id, "action": "buy"}):
            row.created_at = row.created_at - by


def test_the_panic_button_never_closes_the_holders_own_contract_for_an_order_it_cannot_confirm(client, owner, clock,
                                                                                                sim):
    """The holder carries a lot of their own; TradeLab's buy of the same contract is rejected where TradeLab can't
    see it (the order book is unreadable). Kotak's figure is the holder's lot: the panic button leaves it."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "B")  # the holder's own lot
    acct.reject_in_book = "RMS: margin not available"
    acct.faults["order_report"] = GATEWAY_PAGE
    _run(client, owner, "fno", "buy")
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == lot, run.summary
    assert not [o for o in acct.orders if o["trnsTp"] == "S"]


def test_the_panic_button_closes_the_confirmed_part_beside_an_unconfirmed_order(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")  # confirmed by the book
    assert acct.net(ts, "NRML") == lot
    acct.reject_in_book = "RMS: margin not available"
    acct.faults["order_report"] = GATEWAY_PAGE  # the second buy is rejected where TradeLab can't see it
    _run(client, owner, "fno", "buy")
    assert len(_logs(action="buy")) == 2
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary  # the confirmed lot is closed
    assert [o["qty"] for o in acct.orders if o["trnsTp"] == "S"] == [lot]  # and only that
    assert run.status != "done"  # the unconfirmed order is reported


def test_delivery_units_settling_are_kept_when_the_same_etf_is_bought_again_the_next_day(client, owner, clock, sim):
    """Monday's units are settling (in neither positions nor demat) when Tuesday buys the same ETF again:
    Tuesday's row in Kotak's positions must not get Monday's units written off."""
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    first = kanha.net("ITBEES-EQ", "CNC")
    assert first > 0
    _age_buys(small, _trading_days_back(1))  # bought on the previous trading day
    kanha.clear_day_positions()
    kanha.cash = 500.0
    _run(client, owner, "holding", "buy")
    second = kanha.net("ITBEES-EQ", "CNC")
    assert second > 0
    trading.true_up_all()
    assert _logs(status="adjusted") == []
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    assert [h.qty for k, h in trading.tradelab_book(user, "holding").items() if k.account_id == small] == [first + second]


def test_delivery_units_sold_outside_tradelab_today_are_still_written_off(client, owner, clock, sim):
    """Kotak's positions show today's sale by the holder: that part of TradeLab's records goes at once."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    kanha.place_order("nse_cm", "CNC", "0", "MKT", str(bought - 1), "DAY", "ITBEES-EQ", "S")  # in the Kotak app
    pv = _preview(client, owner, "holding", "sell")
    assert [o["quantity"] for o in pv["orders"]] == [1]
    assert _logs(status="adjusted") == []  # just looking changes nothing
    _, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert [a.quantity for a in _logs(status="adjusted")] == [bought - 1]
    assert kanha.net("ITBEES-EQ", "CNC") == 0


def test_a_failed_cancel_on_another_product_does_not_hold_back_the_intraday_square_off(client, owner, clock, sim):
    """A delivery order of the same stock that couldn't be cancelled earlier today has nothing to do with the
    intraday position: once the order book reads, the square-off closes it (until then it closes nothing)."""
    user = _intraday_bought(client, owner, sim)
    mummy = sim.account("CD202")
    assert mummy.net("SBIN-EQ", "MIS") > 0
    clock.set(IN_SESSION)
    r = client.post("/api/basket", json={"section": "holding", "symbol": "SBIN", "trading_symbol": "SBIN-EQ",
                                         "token": "3045", "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text
    mummy.cash = 1000.0
    mummy.partial_next = 1  # Holding's buy of the same stock is still working...
    _run(client, owner, "holding", "buy")
    mummy.faults["cancel_order"] = GATEWAY_PAGE  # ...when Holding's Sell can't cancel it
    _run(client, owner, "holding", "sell")
    assert [(r.symbol, r.product) for r in _logs(action="cancel", status="failed")] == [("SBIN-EQ", "CNC")]
    del mummy.faults["cancel_order"]
    mummy.faults["order_report"] = GATEWAY_PAGE
    clock.set(SQUARE_OFF)
    held = mummy.net("SBIN-EQ", "MIS")
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    assert mummy.net("SBIN-EQ", "MIS") == held and run.status != "done"
    del mummy.faults["order_report"]
    clock.set(SQUARE_OFF + timedelta(minutes=1))
    run = _wait(trading.run_exit(user, "catchup", section="intraday"))
    assert mummy.net("SBIN-EQ", "MIS") == 0, run.summary


def test_an_order_reply_with_a_gateway_code_and_session_words_is_never_sent_twice(client, owner, clock, sim):
    """A 504 whose text mentions the session is a lost reply, not an expired login: renewing the login and
    sending again would buy twice."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    acct.faults["place_order"] = [(PLACED_BUT, {"stat": "Not_Ok", "emsg": "sessionTimeout: upstream timed out",
                                                "stCode": 504})]
    _run(client, owner, "fno", "buy")
    assert len([c for c in acct.calls if c[0] == "place_order"]) == 1
    assert acct.net(item["trading_symbol"], "NRML") == item["lot_size"]
    (row,) = _logs(action="buy")
    assert row.status == "success" and row.order_id


def test_an_expired_session_on_an_order_is_renewed_and_the_order_sent_once(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    acct.faults["place_order"] = [{"stat": "Not_Ok", "emsg": "Invalid session, please re-login to continue",
                                   "stCode": 1003}]
    _, run = _run(client, owner, "fno", "buy")
    assert run.status == "done", run.summary
    assert acct.net(item["trading_symbol"], "NRML") == item["lot_size"]
    assert len([o for o in acct.orders if o["trnsTp"] == "B"]) == 1


def test_an_order_rejected_after_a_part_fill_keeps_the_filled_units(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.partial_next, acct.reject_in_book = lot, "RMS: Margin Exceeds, Required Margin 120000"
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == lot
    assert run.funds_short == ["Acc A"]  # the rest was refused for money: the red pop-up says so
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    trading.reconcile_orders(user)
    (row,) = _logs(action="buy")
    assert (row.status, row.quantity, row.final) == ("success", lot, True)
    _, run = _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == 0, run.summary


def test_a_fill_between_the_screen_and_yes_does_not_stop_the_exit(client, owner, clock, sim):
    clock.set(IN_SESSION)
    ts, lot, acct = _part_filled_contract(client, owner, sim)
    pv = client.get("/api/sections/fno/preview", params={"action": "sell"}, headers=owner).json()
    working = next(o for o in acct.orders if o["ordSt"] == "open")
    with acct.lock:  # the rest fills while the confirmation screen is open
        working.update(ordSt="complete", fldQty=working["qty"])
        acct.open_positions[("nse_fo", ts, "NRML")].buy += working["qty"] - lot
    assert client.get("/api/sections/fno/positions", headers=owner).status_code == 200  # the Trade page refreshes
    r = client.post("/api/sections/fno/sell", json={"fingerprint": pv["fingerprint"], "confirm_id": pv["confirm_id"],
                                                    "symbols": None}, headers=owner)
    assert r.status_code == 200, r.text
    run = _wait(r.json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary


def test_records_are_not_written_down_while_kotaks_positions_lag_a_fill(client, owner, clock, sim):
    """Kotak's order book shows a fill its positions don't show yet: the filled units stay TradeLab's."""
    clock.set(IN_SESSION)
    ts, _, acct = _part_filled_contract(client, owner, sim)
    acct.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "SBIN-EQ", "B")  # another position, so rows > 0
    acct.hide_positions = {ts}
    trading.true_up_all()
    assert _logs(status="adjusted") == []
    acct.hide_positions = set()
    _, run = _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == 0, run.summary


# ----------------------------------------------------------------------------- review 4


@pytest.mark.parametrize("side", ["B", "S"])
def test_a_contract_is_never_written_off_while_kotaks_positions_lag_its_fill(client, owner, clock, sim, side):
    """The order completed at once, but Kotak's positions don't show the contract yet (another position does).
    The panic button says it couldn't close it (never "done"), TradeLab forgets nothing, and the contract is
    closed once Kotak shows it."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut" if side == "B" else "nifty 25000 pe", side)
    ts = item["trading_symbol"]
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "SBIN-EQ", "B")  # any other position
    acct.hide_positions = {ts}
    _run(client, owner, "fno", "buy")
    (row,) = _logs(action="buy" if side == "B" else "sell")
    assert row.status == "success" and row.final
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert run.status != "done", run.summary
    assert any("don't show" in (r.message or "") for r in _logs(run_id=run.id, status="failed"))
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Acc A"]]
    assert _logs(status="adjusted") == []
    acct.hide_positions = set()
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary


def test_the_exit_screen_while_positions_lag_shows_why_and_forgets_nothing(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty 25000 pe", "S")
    ts = item["trading_symbol"]
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "SBIN-EQ", "B")
    acct.hide_positions = {ts}
    _run(client, owner, "fno", "buy")
    pv = _preview(client, owner, "fno", "sell")
    assert any("don't show" in (o["skip"] or "") for o in pv["orders"]), pv["orders"]
    acct.hide_positions = set()
    pv, run = _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == 0, (run.status, run.summary, pv["orders"])


def test_looking_at_the_exit_screen_writes_nothing_even_when_kotak_shows_less(client, owner, clock, sim):
    """The holder closed TradeLab's contract in the Kotak Neo app: the screen shows nothing left to close, but
    only "Yes" records it."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "S")  # closed in the Kotak Neo app
    pv = _preview(client, owner, "fno", "sell")
    nothing = "Nothing left to close (already sold, bought back or expired)"
    assert [(o["quantity"], o["skip"]) for o in pv["orders"]] == [(0, nothing)]
    assert _logs(status="adjusted") == []
    _run(client, owner, "fno", "sell")
    assert [a.quantity for a in _logs(status="adjusted")] == [lot]
    assert acct.net(ts, "NRML") == 0 and len([o for o in acct.orders if o["trnsTp"] == "S"]) == 1


def test_delivery_units_the_holder_sold_the_next_day_are_written_off(client, owner, clock, sim):
    """Monday's units, sold by the holder on Tuesday: the true-up writes them off, so Wednesday's units the
    holder buys for themself are never sold by TradeLab."""
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    one = _trading_days_back(1)
    _age_buys(small, one)
    kanha.settle_overnight()
    kanha.place_order("nse_cm", "CNC", "0", "MKT", str(bought), "DAY", "ITBEES-EQ", "S")  # in the Kotak Neo app
    trading.true_up_all()
    assert [a.quantity for a in _logs(status="adjusted")] == [bought]
    _age_buys(small, _trading_days_back(2) - one)
    kanha.settle_overnight()
    kanha.place_order("nse_cm", "CNC", "0", "MKT", str(bought), "DAY", "ITBEES-EQ", "B")  # the holder's own
    pv = _preview(client, owner, "holding", "sell")
    assert [o for o in pv["orders"] if not o["skip"]] == [], pv["orders"]


def test_a_sale_while_delivery_units_are_still_settling_says_so(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    _age_buys(small, _trading_days_back(1))
    kanha.clear_day_positions()  # yesterday's units: in neither positions nor the demat yet
    pv, run = _run(client, owner, "holding", "sell")
    assert run.status != "done", run.summary
    assert any("still settling" in (o["skip"] or "") for o in pv["orders"]), pv["orders"]
    assert _logs(status="adjusted") == [] and not kanha.sold()


@pytest.mark.parametrize("how", ["exit", "panic"])
def test_a_close_cut_short_by_a_rejection_is_reported_with_the_rest_still_open(client, owner, clock, sim, how):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == 2 * lot
    acct.partial_next, acct.reject_in_book = lot, "RMS: price band exceeded"
    if how == "exit":
        _, run = _run(client, owner, "fno", "sell")
    else:
        run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == lot
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    assert list(trading.tradelab_open(user, "fno").values()) == [lot]
    assert run.status == "partial", run.summary
    assert any(f"{lot} still open" in (r.message or "") for r in _logs(run_id=run.id, status="failed"))
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Acc A"]]
    _, run = _run(client, owner, "fno", "sell")  # closing again closes the rest
    assert acct.net(ts, "NRML") == 0, run.summary


def test_a_close_still_working_when_tradelab_stops_following_it_is_reported(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "FINAL_CHECKS", (0.05,))
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")
    acct.partial_next = lot  # a thin market: the close fills one lot and keeps working
    _, run = _run(client, owner, "fno", "sell")
    assert run.status == "partial", run.summary
    assert any("Still working at Kotak" in (r.message or "") for r in _logs(run_id=run.id, status="failed"))
    assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Acc A"]]
    _, run = _run(client, owner, "fno", "sell")  # cancels the working close, then closes what is left
    assert acct.net(ts, "NRML") == 0, run.summary
    assert [o["ordSt"] for o in acct.orders if o["trnsTp"] == "S"] == ["cancelled", "complete"]


def test_delivery_units_kotak_shows_but_wont_let_be_sold_are_reported(client, owner, clock, sim):
    """Units in the demat that aren't free to sell yet (pledged, or not settled): Sell sells what it can and
    says what it couldn't, instead of passing over them."""
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    assert bought > 2
    _age_buys(small, _trading_days_back(1))
    kanha.settle_overnight()
    kanha.demat["ITBEES-EQ"][1] = 2  # only 2 free to sell
    pv, run = _run(client, owner, "holding", "sell")
    assert [o["quantity"] for o in pv["orders"] if not o["skip"]] == [2]
    assert any("can't be sold yet" in (o["skip"] or "") for o in pv["orders"]), pv["orders"]
    assert run.status == "partial", run.summary
    assert [o["qty"] for o in kanha.sold()] == [2]


# ----------------------------------------------------------------------------- review 5


def _close_left_working(client, owner, sim, monkeypatch):
    """2 lots bought; the Exit's close fills one lot and keeps working."""
    monkeypatch.setattr(trading, "FINAL_CHECKS", (0.05,))
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 2}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")
    acct.partial_next = item["lot_size"]
    _, run = _run(client, owner, "fno", "sell")
    assert run.status == "partial", run.summary
    return item["trading_symbol"], item["lot_size"], acct


def test_after_a_close_left_working_the_exit_screen_shows_what_is_left(client, owner, clock, sim, monkeypatch):
    clock.set(IN_SESSION)
    ts, lot, acct = _close_left_working(client, owner, sim, monkeypatch)
    pv = _preview(client, owner, "fno", "sell")
    assert [o["quantity"] for o in pv["orders"] if not o["skip"]] == [lot]
    rows = client.get("/api/sections/fno/positions", headers=owner).json()["rows"]
    assert [r["quantity"] for r in rows] == [lot]
    _, run = _run(client, owner, "fno", "sell")  # cancels the working close, then closes what is left
    assert acct.net(ts, "NRML") == 0 and run.status == "done", run.summary


@pytest.mark.parametrize("lag, closed", [(2, True), (10, False)])
def test_the_panic_button_waits_for_kotak_to_show_a_cancel(client, owner, clock, sim, monkeypatch, lag, closed):
    """Kotak shows a cancel it accepted a few looks late: the panic button waits for it and then closes what is
    left, or, if it never shows, says so (never "done") and sends nothing beside the order still working."""
    clock.set(IN_SESSION)
    ts, lot, acct = _close_left_working(client, owner, sim, monkeypatch)
    acct.cancel_lag = lag
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    if closed:
        assert acct.net(ts, "NRML") == 0, run.summary
    else:
        assert run.status != "done", run.summary
        assert any("doesn't show the order stopped yet" in (r.message or "")
                   for r in _logs(run_id=run.id, status="failed"))
        assert acct.net(ts, "NRML") == lot
        assert len([o for o in acct.orders if o["trnsTp"] == "S"]) == 1  # no second close beside it
        assert ["Acc A"] in [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"]


def test_the_panic_button_reports_a_buy_kotak_hasnt_shown_cancelled(client, owner, clock, sim, monkeypatch):
    """TradeLab's own part-filled buy is still working: the panic button closes the lot that filled and says the
    rest may still fill, since Kotak's book doesn't show the cancel yet."""
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "FINAL_CHECKS", (0.05,))
    ts, _, acct = _part_filled_contract(client, owner, sim)
    acct.cancel_lag = 10
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0
    assert run.status != "done", run.summary
    assert any("doesn't show the order stopped yet" in (r.message or "") for r in _logs(run_id=run.id, status="failed"))


def test_delivery_units_the_holder_sold_in_part_the_next_day_are_written_off(client, owner, clock, sim):
    """Monday's units are in the demat on Tuesday and the holder sells some: the rest shows there, so none of
    them is "still settling", and the holder's own later units are never sold by TradeLab."""
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    assert bought > 2
    one = _trading_days_back(1)
    _age_buys(small, one)
    kanha.settle_overnight()
    kanha.place_order("nse_cm", "CNC", "0", "MKT", "2", "DAY", "ITBEES-EQ", "S")  # the holder sells 2
    trading.true_up_all()
    assert [a.quantity for a in _logs(status="adjusted")] == [2]
    _age_buys(small, _trading_days_back(2) - one)
    kanha.settle_overnight()
    kanha.place_order("nse_cm", "CNC", "0", "MKT", "2", "DAY", "ITBEES-EQ", "B")  # the holder's own 2
    _, run = _run(client, owner, "holding", "sell")
    assert run.status == "done", run.summary
    assert kanha.net("ITBEES-EQ", "CNC") + kanha.demat["ITBEES-EQ"][0] == 2  # only the holder's own are left


# ----------------------------------------------------------------------------- review 6


def _bought_on_two_days(client, owner, sim):
    """The same ETF bought on two days; the next morning day 2's units show in neither positions nor demat."""
    small = _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    kanha = sim.account("AB101")
    _run(client, owner, "holding", "buy")  # day 1
    first = kanha.net("ITBEES-EQ", "CNC")
    kanha.settle_overnight()  # day 1's units reach the demat
    _age_buys(small, _trading_days_back(1))
    kanha.cash = 500.0
    _run(client, owner, "holding", "buy")  # day 2
    second = kanha.net("ITBEES-EQ", "CNC")
    kanha.clear_day_positions()  # day 3
    _age_buys(small, _trading_days_back(1))  # day 1 older still, day 2 the previous trading day
    return kanha, first, second


def test_units_still_settling_are_kept_beside_older_units_in_the_demat(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _bought_on_two_days(client, owner, sim)
    trading.true_up_all()
    assert _logs(status="adjusted") == []


def test_a_sale_beside_units_still_settling_sells_the_rest_once_they_arrive(client, owner, clock, sim):
    clock.set(IN_SESSION)
    kanha, first, second = _bought_on_two_days(client, owner, sim)
    _, run = _run(client, owner, "holding", "sell")
    assert [o["qty"] for o in kanha.sold()] == [first]
    assert run.status != "done", run.summary  # day 2's units couldn't be sold yet, and it says so
    kanha.demat["ITBEES-EQ"][0] += second  # day 2's units arrive
    kanha.demat["ITBEES-EQ"][1] += second
    pv = _preview(client, owner, "holding", "sell")
    assert [o["quantity"] for o in pv["orders"] if not o["skip"]] == [second]


# ----------------------------------------------------------------------------- review 7


def test_looking_again_after_an_outside_sale_writes_nothing_more_off(client, owner, clock, sim):
    """Day 2's units still settling beside day 1's in the demat; the holder sells one: exactly that one is
    written off, and the same Kotak figures read again change nothing."""
    clock.set(IN_SESSION)
    kanha, first, second = _bought_on_two_days(client, owner, sim)
    kanha.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "ITBEES-EQ", "S")  # the holder, in the Kotak Neo app
    held = []
    for _ in range(4):
        trading.true_up_all()
        held.append(first + second - sum(r.quantity for r in _logs(status="adjusted")))
    assert held == [first + second - 1] * 4


# ----------------------------------------------------------------------------- review 8


def test_nothing_is_written_off_between_midnight_and_the_morning(client, owner, clock, sim):
    """Until Kotak's start of day it may still show yesterday's trades while TradeLab's day has turned: a true-up
    that runs late, after midnight, writes nothing off; the next one in the day does."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Kanha", "AB101", cash=500.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    kanha.place_order("nse_cm", "CNC", "0", "MKT", str(bought), "DAY", "ITBEES-EQ", "S")  # in the Kotak Neo app
    clock.set(IN_SESSION.replace(hour=0, minute=5) + timedelta(days=1))
    trading.true_up_all()
    assert _logs(status="adjusted") == []
    clock.set(IN_SESSION.replace(hour=15, minute=40))
    trading.true_up_all()
    assert [a.quantity for a in _logs(status="adjusted")] == [bought]


def test_the_simulated_demat_keeps_a_sale_and_a_later_buy_of_the_same_day_apart(sim):
    acct = sim.account("AB101", demat={"ITBEES-EQ": [7, 7, 30.0]})
    acct.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "ITBEES-EQ", "S")
    acct.place_order("nse_cm", "CNC", "0", "MKT", "5", "DAY", "ITBEES-EQ", "B")
    acct.place_order("nse_cm", "CNC", "0", "MKT", "2", "DAY", "ITBEES-EQ", "S")
    acct.settle_overnight()
    assert acct.demat["ITBEES-EQ"][0] == 7 - 1 + 5 - 2


# ----------------------------------------------------------------------------- broad audit, round 1


def test_one_accounts_square_off_closes_contracts_only_in_that_account(client, owner, clock, sim):
    """The Accounts screen's "Square off" on one family member's account never closes TradeLab's contracts in
    another member's account."""
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
    assert acc_b.net(ts, "NRML") == lot, run.summary


def _intraday_stock(client, owner):
    r = client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045",
                                         "exchange_segment": "nse_cm"}, headers=owner)
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("lag, done", [(1, True), (50, False)])
def test_square_off_waits_for_kotak_to_show_a_cancelled_entry_stopped(client, owner, clock, sim, lag, done):
    """The 14:30 square-off cancels a buy still working with nothing filled. Kotak accepts the cancel, but its book
    shows the order working a while longer: the square-off waits for the book (the order could still fill and open
    a position) and, if it never shows it stopped, says so instead of "done", so it is retried and the pop-up shows."""
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _intraday_stock(client, owner)
    acct = sim.account("AB101")
    acct.partial_next = 0  # the buy is accepted and keeps working with nothing filled
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    buy(client, owner)
    (entry,) = [o for o in acct.orders if o["trnsTp"] == "B"]
    assert entry["ordSt"] == "open" and entry["fldQty"] == 0
    acct.cancel_lag = lag
    clock.set(SQUARE_OFF)
    run = _wait(trading.run_exit(user, "scheduled", section="intraday"))
    cancels = [o for o in acct.calls if o[0] == "cancel_order"]
    if done:
        assert run.status == "done" and entry["ordSt"] == "cancelled", run.summary
    else:
        assert run.status != "done", run.summary
        assert any("doesn't show the order stopped yet" in (r.message or "")
                   for r in _logs(run_id=run.id, status="failed"))
        assert [a["accounts"] for a in _alerts(client, owner) if a["kind"] == "exit"] == [["Acc A"]]
    assert len(cancels) <= 1  # an accepted cancel is never sent again


def test_a_sell_first_never_closes_the_holders_lot_beside_an_order_the_book_never_confirmed(client, owner, clock, sim):
    """TradeLab's buy was rejected where TradeLab can't see it (unreadable order book) next to the holder's own lot:
    a sell-first order sized against TradeLab's records would close the holder's lot, so nothing is sent."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "B")  # the holder's own lot
    acct.reject_in_book = "RMS: margin not available"
    acct.faults["order_report"] = GATEWAY_PAGE
    _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == lot  # only the holder's lot: TradeLab's buy was rejected
    r = client.patch(f"/api/basket/{item['id']}", json={"side": "S"}, headers=owner)
    assert r.status_code == 200, r.text
    pv, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == lot, (run.summary, pv["orders"])
    assert not [o for o in acct.orders if o["trnsTp"] == "S"]
    assert any("hasn't confirmed" in (o["skip"] or "") for o in pv["orders"])


@pytest.mark.parametrize("how", ["exit", "panic"])
def test_exit_never_says_done_beside_a_stop_loss_that_would_open_a_new_position(client, owner, clock, sim, how):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut", side="S")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == -lot, run.summary
    sl = acct.add_open_order(ts, "nse_fo", "NRML", "B", lot)  # a stop-loss placed in the Kotak Neo app
    if how == "exit":
        _, run = _run(client, owner, "fno", "sell")
    else:
        run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net(ts, "NRML") == 0, run.summary
    assert acct.order(sl)["ordSt"] == "trigger pending"  # never cancelled by TradeLab (it isn't TradeLab's)
    assert run.status != "done", run.summary
    assert any("would open a new position" in (r.message or "") for r in _logs(run_id=run.id, status="failed"))


def test_a_stop_loss_beside_the_holders_own_remaining_position_is_no_alarm(client, owner, clock, sim):
    """The holder's own short stays after TradeLab buys back its lot: their stop-loss would close their own short,
    not open anything new, so Exit is done."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut", side="S")
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _run(client, owner, "fno", "buy")
    acct.place_order("nse_fo", "NRML", "0", "MKT", str(lot), "DAY", ts, "S")  # the holder's own short
    acct.add_open_order(ts, "nse_fo", "NRML", "B", lot)  # and its stop-loss
    _, run = _run(client, owner, "fno", "sell")
    assert acct.net(ts, "NRML") == -lot and run.status == "done", run.summary


def _hold(acct):
    """Make an account's place_order wait until released (Kotak slow for that account)."""
    entered, release = threading.Event(), threading.Event()
    orig = acct.place_order

    def slow(*a, **kw):
        entered.set()
        assert release.wait(15), "never released"
        return orig(*a, **kw)

    acct.place_order = slow
    return entered, release, orig


def _until(cond, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def _lagging_book(acct, reads=1):
    """Kotak's order book shows an order placed from now on only after `reads` more reads."""
    hidden: dict[str, int] = {}
    orig_place, orig_report = acct.place_order, acct.order_report

    def place(*a, **kw):
        before = {o["nOrdNo"] for o in acct.orders}
        out = orig_place(*a, **kw)
        for o in acct.orders:
            if o["nOrdNo"] not in before:
                hidden[o["nOrdNo"]] = reads
        return out

    def report(**kw):
        out = orig_report(**kw)
        if isinstance(out, dict) and isinstance(out.get("data"), list):
            rows = [r for r in out["data"] if hidden.get(r["nOrdNo"], 0) <= 0]
            out = dict(out, data=rows) if rows else dict(kotak_sim.NO_DATA)
        for k in list(hidden):
            hidden[k] -= 1
        return out

    acct.place_order, acct.order_report = place, report


def test_a_rejection_seen_while_a_run_is_still_sending_is_never_turned_back_into_a_fill(client, owner, clock, sim):
    """One account's buy is rejected at once while another account's order is still on its way, and the Holding
    page's positions refresh runs meanwhile: the rejection stays a rejection, so Sell never sells the holder's
    own units in its place."""
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Kanha", "AB101", cash=200.0)
    _add(client, owner, sim, "Mummy Ji", "CD202", cash=168389.84, demat={"ITBEES-EQ": [1801, 1801, 30.0]})
    kotak_sim.PRICES["ITBEES-EQ"] = kotak_sim.TOKEN_PRICES["19084"] = 35.90
    _add_etf(client, owner, "ITBEES")
    kanha, mummy = sim.account("AB101"), sim.account("CD202")
    mummy.reject_in_book = "RMS:Margin Exceeds,Required:165000.00,Available:160000.00"
    entered, release, orig = _hold(kanha)
    pv, r = _command(client, owner, "holding", "buy")
    assert r.status_code == 200, r.text
    _until(lambda: entered.is_set() and any(o["ordSt"] == "rejected" for o in mummy.orders))
    assert client.get("/api/sections/holding/positions", headers=owner).status_code == 200  # the 30 s refresh
    release.set()
    _wait(r.json()["run_id"])
    kanha.place_order = orig
    (row,) = _logs(account_name="Mummy Ji", action="buy")
    assert row.status == "failed", (row.status, row.message)
    _run(client, owner, "holding", "sell")
    assert mummy.demat["ITBEES-EQ"][0] == 1801  # her own units are untouched


def test_a_lost_reply_late_in_a_long_batch_is_not_taken_as_never_placed(client, owner, clock, sim, monkeypatch):
    """The "not placed" wait runs from when an order was sent, not from when the batch started: an order sent late
    in a slow batch, whose reply was lost and which Kotak's book shows a moment later, is TradeLab's and Exit
    closes it."""
    from concurrent.futures import ThreadPoolExecutor
    clock.set(IN_SESSION)
    monkeypatch.setattr(trading, "MISSING_AFTER", timedelta(seconds=1))
    monkeypatch.setattr(trading, "FINAL_CHECKS", (0.05, 0.05, 0.05))
    monkeypatch.setattr(trading, "_pool", lambda n: ThreadPoolExecutor(max_workers=1))
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    _add(client, owner, sim, "Acc B", "CD202", cash=25000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "nifty fut")
    a, b = sim.account("AB101"), sim.account("CD202")
    orig_a = a.place_order

    def slow_a(*args, **kw):
        time.sleep(1.2)  # Kotak slow: the batch takes a while
        return orig_a(*args, **kw)

    a.place_order = slow_a
    b.faults["place_order"] = [(PLACED_BUT, {"message": "Endpoint request timed out"})]
    _lagging_book(b, reads=1)
    _run(client, owner, "fno", "buy")
    a.place_order = orig_a
    ts = item["trading_symbol"]
    _, run = _run(client, owner, "fno", "sell")
    assert a.net(ts, "NRML") == 0
    assert b.net(ts, "NRML") == 0, run.summary


def _lag_positions(acct, lag: float) -> None:
    """Kotak's positions show a fill only `lag` seconds after it happened; its order book shows it at once."""
    state = {"until": 0.0, "reply": None}
    real_place = acct.place_order

    def place_order(*a, **kw):
        before = kotak_sim.SimAccount.positions(acct)
        res = real_place(*a, **kw)
        if time.monotonic() >= state["until"]:
            state["until"], state["reply"] = time.monotonic() + lag, before
        return res

    def positions(**kw):
        if time.monotonic() < state["until"]:
            return copy.deepcopy(state["reply"])
        return kotak_sim.SimAccount.positions(acct, **kw)

    acct.place_order, acct.positions = place_order, positions


def test_the_panic_button_tapped_twice_never_closes_twice_while_positions_lag(client, owner, clock, sim, monkeypatch):
    monkeypatch.setattr(trading, "VERIFY_DELAY", 0.2)
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")  # an intraday position
    _lag_positions(acct, 0.5)
    first = client.post("/api/automation/square-off-all", headers=owner).json()["run_id"]
    time.sleep(0.05)
    second = client.post("/api/automation/square-off-all", headers=owner).json()["run_id"]
    runs = [_wait(first), _wait(second)]
    sells = [(o["trnsTp"], o["qty"]) for o in acct.orders if o["GuiOrdId"].startswith("TL")]
    assert acct.net("SBIN-EQ", "MIS") == 0, (sells, [(r.status, r.summary) for r in runs])
    assert sells == [("S", 10)], sells


def _age_rows(by: timedelta) -> None:
    """Make every record look `by` older (record times use the real clock)."""
    with session_scope() as db:
        for row in db.find(OrderLog, {}):
            row.created_at = row.created_at - by


def _bonus_adjust(acct, old_ts: str, new_contract, factor: int) -> None:
    """NSE's bonus / split adjustment: the open position's units are multiplied (options also move into a new
    contract with the adjusted strike, so a new trading symbol)."""
    seg = contracts.contract_master._segs["nse_fo"]
    rows = [c for c in seg.contracts if c.trading_symbol != old_ts] + [new_contract]
    contracts.contract_master._set("nse_fo", rows, "kotak", contracts._today().isoformat())
    with acct.lock:
        p = acct.open_positions.pop(("nse_fo", old_ts, "NRML"))
        acct.open_positions[("nse_fo", new_contract.trading_symbol, "NRML")] = _Pos(
            "nse_fo", new_contract.trading_symbol, "NRML", cf_buy=max(p.net, 0) * factor, cf_sell=max(-p.net, 0) * factor)


def test_a_bonus_on_a_stock_future_never_leaves_half_of_tradelabs_position_open_as_done(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 2, "sizing_mode": "custom"}, headers=owner)
    item = _add_contract(client, owner, "fno", "reliance fut", lots=2)
    ts, lot = item["trading_symbol"], item["lot_size"]
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == 2 * lot, run.summary
    trading.true_up_all()
    acct.settle_overnight()
    _age_rows(_trading_days_back(1))
    clock.set(IN_SESSION + timedelta(days=1))
    old = contracts.contract_master.get("nse_fo", ts)
    _bonus_adjust(acct, ts, _replace(old, lot_size=2 * lot), 2)  # 1:1 bonus: lot 500 -> 1000, units doubled
    assert acct.net(ts, "NRML") == 4 * lot
    pv, run = _run(client, owner, "fno", "sell")
    # All of it is TradeLab's: either it is all closed, or the run says something is left (never a plain "done").
    assert acct.net(ts, "NRML") == 0 or run.status != "done", (acct.net(ts, "NRML"), run.status, run.summary,
                                                                pv["orders"])


def test_a_bonus_on_a_sold_stock_option_is_never_forgotten(client, owner, clock, sim):
    clock.set(IN_SESSION)
    seg = contracts.contract_master._segs["nse_fo"]
    fut = next(c for c in seg.contracts if c.symbol == "RELIANCE")
    opt = _replace(fut, trading_symbol=f"RELIANCE{fut.expiry:%y%b}3000CE".upper(), token="S900001",
                   instrument_type="OPTSTK", option_type="CE", strike=3000.0)
    contracts.contract_master._set("nse_fo", seg.contracts + [opt], "kotak", contracts._today().isoformat())
    _add(client, owner, sim, "Acc A", "AB101", cash=500000.0)
    client.put("/api/sections/fno", json={"max_lots": 1}, headers=owner)
    item = _add_contract(client, owner, "fno", "reliance 3000 ce", side="S")
    ts, lot = item["trading_symbol"], item["lot_size"]
    assert ts == opt.trading_symbol
    acct = sim.account("AB101")
    _, run = _run(client, owner, "fno", "buy")
    assert acct.net(ts, "NRML") == -lot, run.summary
    trading.true_up_all()
    acct.settle_overnight()
    _age_rows(_trading_days_back(1))
    clock.set(IN_SESSION + timedelta(days=1))
    new = _replace(opt, trading_symbol=f"RELIANCE{fut.expiry:%y%b}1500CE".upper(), token="S900002", strike=1500.0,
                   lot_size=2 * lot)
    _bonus_adjust(acct, ts, new, 2)  # the sold call now sits in the adjusted 1500 CE, 2 x the units
    acct.place_order("nse_cm", "CNC", "0", "MKT", "1", "DAY", "SBIN-EQ", "B")  # any other position today
    trading.true_up_all()  # the 15:40 check
    pv, run = _run(client, owner, "fno", "sell")
    still = acct.net(new.trading_symbol, "NRML")
    adjusted = [r.message for r in _logs(status="adjusted")]
    # TradeLab's sold call is still open at Kotak: it must not be written off as closed while the run says "done".
    assert still == 0 or run.status != "done", (still, run.status, run.summary, adjusted, pv["orders"])


def test_the_holder_selling_her_own_units_while_tradelab_units_settle_forgets_nothing(client, owner, clock, sim):
    """She owns 10 ITBEES; TradeLab buys more for her account. Next day TradeLab's units are still settling and she
    sells 5 of her own: TradeLab writes nothing off, and once its units arrive Sell leaves her own 5."""
    from app.db import database
    clock.set(IN_SESSION)
    mummy_id = _add(client, owner, sim, "Mummy Ji", "CD202", cash=2000.0, demat={"ITBEES-EQ": [10, 10, 30.0]})
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    trading.true_up_all()  # day 1's 15:40 check sees her own 10
    mummy = sim.account("CD202")
    bought = mummy.net("ITBEES-EQ", "CNC")
    back = _trading_days_back(1)
    _age_buys(mummy_id, back)
    for d in database()["holder_units"].find({}):
        database()["holder_units"].update_one({"_id": d["_id"]}, {"$set": {"seen_at": d["seen_at"] - back}})
    mummy.clear_day_positions()
    mummy.place_order("nse_cm", "CNC", "0", "MKT", "5", "DAY", "ITBEES-EQ", "S")  # 5 of her own
    trading.true_up_all()
    assert _logs(status="adjusted") == []
    mummy.demat["ITBEES-EQ"][0] += bought  # TradeLab's units arrive
    mummy.demat["ITBEES-EQ"][1] += bought
    mummy.clear_day_positions()
    _run(client, owner, "holding", "sell")
    assert mummy.demat["ITBEES-EQ"][0] == 5


@pytest.fixture(autouse=True)
def _clearing_holidays_downloaded():
    """The clearing-holiday lists of the years around today are known, with no holidays unless a test adds one."""
    year = datetime.now(IST).year
    calendar.save_clearing_holidays(set(), {year - 1, year, year + 1})


def _clearing_holiday(days_back: int) -> None:
    """Make the trading day this many trading days back a clearing holiday (record times use the real clock)."""
    day = datetime.now(IST).date() - _trading_days_back(days_back)
    calendar.save_clearing_holidays({day}, {day.year})


def _held_by_tradelab(client, owner, account_id):
    user = client.get("/api/auth/me", headers=owner).json()["id"]
    return [h.qty for k, h in trading.tradelab_book(user, "holding").items() if k.account_id == account_id]


def test_b2_units_settling_after_a_clearing_holiday_are_not_written_off_by_a_morning_sell(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small = _add(client, owner, sim, "Kanha", "AB101", cash=2000.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")  # day D
    kanha = sim.account("AB101")
    bought = kanha.net("ITBEES-EQ", "CNC")
    assert bought > 0
    # D+1 was a clearing holiday (NSE trades, banks and the clearing corporation don't), so D's trades settle
    # on D+2, the pay-out arriving in the afternoon. D+2, 10:00: in neither positions nor the demat yet.
    _clearing_holiday(1)
    _age_buys(small, _trading_days_back(2))
    kanha.clear_day_positions()
    pv, run = _run(client, owner, "holding", "sell")
    written = [a.quantity for a in _logs(status="adjusted")]
    # The afternoon: the units arrive; TradeLab must still know they are its own.
    kanha.demat["ITBEES-EQ"] = [bought, bought, 100.0]
    assert (written, run.status, _held_by_tradelab(client, owner, small)) == ([], "failed", [bought]), (
        f"written off {written}, run {run.status} ({run.summary}), TradeLab now counts "
        f"{_held_by_tradelab(client, owner, small)} of {bought}; rows: "
        f"{[(o['quantity'], o['skip']) for o in pv['orders']]}")


def _settling_setup(client, owner, sim):
    clock_account = _add(client, owner, sim, "Kanha", "AB101", cash=2000.0)
    _add_etf(client, owner, "ITBEES")
    _run(client, owner, "holding", "buy")
    kanha = sim.account("AB101")
    return clock_account, kanha, kanha.net("ITBEES-EQ", "CNC")


def test_units_whose_settlement_day_is_over_are_written_off_even_after_a_clearing_holiday(client, owner, clock, sim):
    clock.set(IN_SESSION)
    small, kanha, bought = _settling_setup(client, owner, sim)
    _clearing_holiday(2)  # bought on D, D+1 a clearing holiday, D+2 settled; now D+3 and the units are gone
    _age_buys(small, _trading_days_back(3))
    kanha.clear_day_positions()
    _, run = _run(client, owner, "holding", "sell")
    assert [a.quantity for a in _logs(status="adjusted")] == [bought]
    assert _held_by_tradelab(client, owner, small) == []


def test_without_the_clearing_holiday_list_units_of_the_last_two_trading_days_are_kept(client, owner, clock, sim):
    from app.models import ClearingYear
    clock.set(IN_SESSION)
    small, kanha, bought = _settling_setup(client, owner, sim)
    with session_scope() as db:
        for y in db.find(ClearingYear):
            db.delete(y)
    _age_buys(small, _trading_days_back(2))
    kanha.clear_day_positions()
    pv, run = _run(client, owner, "holding", "sell")
    assert _logs(status="adjusted") == [] and run.status != "done", run.summary
    assert any("still settling" in (o["skip"] or "") for o in pv["orders"]), pv["orders"]
    assert _held_by_tradelab(client, owner, small) == [bought]


def test_nse_clearing_holidays_are_downloaded_kept_and_never_raise(client):
    from datetime import date
    sample = {"CM": [{"tradingDate": "01-Apr-2027", "description": "Annual bank closing"},
                     {"tradingDate": "03-Apr-2027", "description": "a Saturday"}]}
    assert calendar.clearing_holidays(2031) is None
    assert calendar.sync_clearing_holidays(lambda: {"CM": [{"tradingDate": "01-Apr-2031"}]}) is True
    assert calendar.clearing_holidays(2031) == {date(2031, 4, 1)}
    assert calendar.sync_clearing_holidays(lambda: sample) is True
    assert calendar.clearing_holidays(2027) == {date(2027, 4, 1)}

    def boom():
        raise ConnectionError("blocked")

    assert calendar.sync_clearing_holidays(boom) is False and calendar.sync_clearing_holidays(lambda: {}) is False
    assert calendar.clearing_holidays(2027) == {date(2027, 4, 1)} and calendar.clearing_holidays(2031) is not None


def test_F3_panic_never_closes_next_to_a_working_order_it_could_not_check(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")  # an intraday position
    sl = acct.add_open_order("SBIN-EQ", "nse_cm", "MIS", "S", 10)  # the holder's stop-loss on it
    acct.faults["order_report"] = GATEWAY_PAGE  # the order book can't be read during the run
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    closes = [o for o in acct.orders if o["GuiOrdId"].startswith("TL")]
    assert acct.order(sl)["ordSt"] == "trigger pending"
    assert closes == [], ([(o["trnsTp"], o["qty"]) for o in closes], run.status, run.summary)
    assert run.status != "done"
    msgs = [x.message for x in _logs(status="failed", action="sell")]
    assert any("order book couldn't be read" in (m or "") for m in msgs), msgs


def test_a_square_off_whose_order_book_reads_on_a_later_attempt_closes_normally(client, owner, clock, sim):
    clock.set(IN_SESSION)
    _add(client, owner, sim, "Acc A", "AB101", cash=25000.0)
    acct = sim.account("AB101")
    acct.place_order("nse_cm", "MIS", "0", "MKT", "10", "DAY", "SBIN-EQ", "B")
    acct.faults["order_report"] = GATEWAY_PAGE
    _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net("SBIN-EQ", "MIS") == 10
    del acct.faults["order_report"]  # the next minute's retry can read the book
    run = _wait(client.post("/api/automation/square-off-all", headers=owner).json()["run_id"])
    assert acct.net("SBIN-EQ", "MIS") == 0, (run.status, run.summary)
