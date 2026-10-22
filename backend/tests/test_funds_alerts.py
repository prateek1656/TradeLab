"""Accounts that couldn't trade for lack of money are named in a pop-up until dismissed."""

from conftest import MARKET_MORNING, buy, fake_for, kotak_payload

from app.broker.base import Order
from app.db import session_scope
from app.models import ExecutionRun, OrderLog


def _account(client, owner, name, ucc, funds):
    acc = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner).json()["id"]
    fake_for(acc).funds_val = funds
    return acc


def _stock(client, owner, sym, token):
    client.post("/api/basket", json={"symbol": sym, "trading_symbol": f"{sym}-EQ", "token": token}, headers=owner)


def test_accounts_without_enough_money_are_named_once(client, owner):
    rich = _account(client, owner, "Main", "UCC1", 100000)
    _account(client, owner, "Wife", "UCC2", 150)   # ₹75 per stock < ₹100 a share
    _account(client, owner, "Empty", "UCC3", 0)
    _stock(client, owner, "AAA", "1")
    _stock(client, owner, "BBB", "2")
    buy(client, owner)
    assert len(fake_for(rich).placed) == 2

    alerts = client.get("/api/alerts", headers=owner).json()
    assert [a["accounts"] for a in alerts] == [["Wife", "Empty"]]
    assert alerts[0]["section"] == "intraday" and alerts[0]["trading_date"] == str(MARKET_MORNING.date())

    assert client.post(f"/api/alerts/{alerts[0]['run_id']}/seen", headers=owner).status_code == 204
    assert client.get("/api/alerts", headers=owner).json() == []


def test_a_broker_rejection_for_margin_counts_too(client, owner):
    acc = _account(client, owner, "Main", "UCC1", 100000)
    _stock(client, owner, "AAA", "1")
    fake = fake_for(acc)
    fake.record_fills = False  # this test writes Kotak's order book itself
    real_place = fake.place_market_order

    def place_then_reject(*args, **kw):
        res = real_place(*args, **kw)
        fake.order_book.append(Order(order_id=res.order_id, status="rejected", trading_symbol="AAA-EQ",
                                     exchange_segment="nse_cm", product="MIS", side="B", quantity=1,
                                     message="RMS:Margin Exceeds,Required:52000.00, Available:100.00"))
        return res

    fake.place_market_order = place_then_reject
    buy(client, owner)
    alerts = client.get("/api/alerts", headers=owner).json()
    assert [a["accounts"] for a in alerts] == [["Main"]]
    with session_scope() as db:
        (log,) = db.find(OrderLog, {"action": "buy"})
        assert log.status == "failed" and log.message.startswith("Rejected by the broker: RMS:Margin Exceeds")


def test_other_failures_are_not_called_lack_of_funds(client, owner):
    acc = _account(client, owner, "Main", "UCC1", 100000)
    _stock(client, owner, "AAA", "1")
    fake = fake_for(acc)
    fake.record_fills = False  # this test writes Kotak's order book itself
    real_place = fake.place_market_order

    def place_then_reject(*args, **kw):
        res = real_place(*args, **kw)
        fake.order_book.append(Order(order_id=res.order_id, status="rejected", trading_symbol="AAA-EQ",
                                     exchange_segment="nse_cm", product="MIS", side="B", quantity=1,
                                     message="Scrip is in ban period"))
        return res

    fake.place_market_order = place_then_reject
    buy(client, owner)
    assert client.get("/api/alerts", headers=owner).json() == []
    with session_scope() as db:
        assert [r.status for r in db.find(ExecutionRun)] == ["failed"]


def test_alerts_are_private(client, owner):
    from conftest import signup

    _account(client, owner, "Empty", "UCC3", 0)
    _stock(client, owner, "AAA", "1")
    buy(client, owner)
    (alert,) = client.get("/api/alerts", headers=owner).json()
    other = {"Authorization": f"Bearer {signup(client, 'x@example.com').json()['token']}"}
    assert client.get("/api/alerts", headers=other).json() == []
    assert client.post(f"/api/alerts/{alert['run_id']}/seen", headers=other).status_code == 404
