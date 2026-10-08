"""The day's P/L report: Date | Account | Stock | P/L, with the day's total at the end."""

import io
from datetime import timedelta

from conftest import MARKET_MORNING, buy, fake_for, kotak_payload, signup
from openpyxl import load_workbook
from sqlalchemy import select

from app.broker.base import BrokerError, Position
from app.db import session_scope
from app.models import PnlRecord
from app.services import reports, trading

TODAY = MARKET_MORNING.date()


def _account(client, owner, name="Main", ucc="UCC1"):
    return client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner).json()["id"]


def _me(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["id"]


def _closed(sym, pnl, qty=10):
    return Position("nse_cm", f"{sym}-EQ", "MIS", 0, buy_qty=qty, sell_qty=qty, pnl=pnl)


def _sheet(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    return load_workbook(io.BytesIO(resp.content)).active


def _table(ws):
    return [tuple(c.value for c in row) for row in ws.iter_rows()]


def test_workbook_layout_and_total_formula():
    rows = [
        reports.Row(2, "Wife", "TCS-EQ", -120.5),
        reports.Row(1, "Main", "SBIN-EQ", 250.0),
        reports.Row(1, "Main", "INFY-EQ", 80.25),
    ]
    ws = load_workbook(io.BytesIO(reports.build_xlsx(rows, TODAY, True, []))).active
    t = _table(ws)
    assert t[0] == ("Date", "Account", "Stock", "P/L (₹)")
    body = [r[1:] for r in t[1:4]]
    assert body == [("Main", "INFY", 80.25), ("Main", "SBIN", 250.0), ("Wife", "TCS", -120.5)]
    assert all(r[0].date() == TODAY for r in t[1:4])
    assert t[5][2:] == ("Today's P/L", "=SUM(D2:D4)")
    assert ws["D2"].number_format == ws["D6"].number_format
    assert ws["A1"].font.name == "Arial" and ws["C6"].font.bold
    assert "before brokerage" in t[7][0]


def test_empty_day_still_has_a_total():
    ws = load_workbook(io.BytesIO(reports.build_xlsx([], TODAY - timedelta(days=1), False, []))).active
    t = _table(ws)
    assert t[1][1:] == ("No trades", None, 0)
    assert t[3][2:] == ("P/L for 13 Oct 2026", "=SUM(D2:D2)")


def test_todays_report_reads_the_broker_and_is_saved(client, owner):
    main, wife = _account(client, owner), _account(client, owner, "Wife", "UCC2")
    fake_for(main).positions_map = {
        ("SBIN-EQ", "MIS"): _closed("SBIN", 250.0),
        ("INFY-EQ", "MIS"): _closed("INFY", -40.0),
        ("UNTOUCHED-EQ", "MIS"): Position("nse_cm", "UNTOUCHED-EQ", "MIS", 0),  # no trades: left out
    }
    fake_for(wife).positions_map = {("TCS-EQ", "MIS"): _closed("TCS", 99.5)}

    ws = _sheet(client.get(f"/api/reports/{TODAY}.xlsx", headers=owner))
    t = _table(ws)
    assert [r[1:] for r in t[1:4]] == [("Main", "INFY", -40.0), ("Main", "SBIN", 250.0), ("Wife", "TCS", 99.5)]
    assert t[5][2:] == ("Today's P/L", "=SUM(D2:D4)")

    days = client.get("/api/reports", headers=owner).json()
    assert days["today"] == TODAY.isoformat()
    assert days["days"] == [{"day": TODAY.isoformat(), "pnl": 309.5, "rows": 3}]


def test_earlier_days_come_from_what_was_saved(client, owner, clock):
    acc = _account(client, owner)
    fake_for(acc).positions_map = {("SBIN-EQ", "MIS"): _closed("SBIN", 500.0)}
    assert client.get(f"/api/reports/{TODAY}.xlsx", headers=owner).status_code == 200

    clock.set(MARKET_MORNING + timedelta(days=1))
    fake_for(acc).positions_map = {}  # the broker forgets yesterday overnight
    t = _table(_sheet(client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)))
    assert t[1][1:] == ("Main", "SBIN", 500.0)
    assert t[3][2] == "P/L for 14 Oct 2026"


def test_a_broker_error_keeps_the_last_saved_numbers(client, owner):
    acc = _account(client, owner)
    fake_for(acc).positions_map = {("SBIN-EQ", "MIS"): _closed("SBIN", 75.0)}
    client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)

    def down():
        raise BrokerError("Kotak is down")

    fake_for(acc).positions = down
    t = _table(_sheet(client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)))
    assert t[1][1:] == ("Main", "SBIN", 75.0)
    assert any("Main: couldn't read positions" in str(r[0]) for r in t)


def test_square_off_saves_the_days_pnl(client, owner, custom_sizing):
    acc = _account(client, owner)
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045", "sizing_value": 5},
                headers=owner)
    buy(client, owner)
    fake = fake_for(acc)
    fake.positions_map[("SBIN-EQ", "MIS")].pnl = 42.0
    fake.positions_map[("SBIN-EQ", "MIS")].buy_qty = 5
    trading.run_exit(_me(client, owner), "manual")

    with session_scope() as db:
        recs = db.scalars(select(PnlRecord)).all()
        assert [(r.account_name, r.symbol, r.pnl, r.trade_date) for r in recs] == [("Main", "SBIN-EQ", 42.0, TODAY)]


def test_evening_job_saves_only_accounts_tradelab_traded(client, owner, custom_sizing):
    traded, idle = _account(client, owner), _account(client, owner, "Idle", "UCC2")
    client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "token": "3045", "sizing_value": 5},
                headers=owner)
    client.patch(f"/api/accounts/{idle}", json={"automation_enabled": False}, headers=owner)
    buy(client, owner)
    fake_for(traded).positions_map[("SBIN-EQ", "MIS")].pnl = 10.0
    fake_for(traded).positions_map[("SBIN-EQ", "MIS")].buy_qty = 5
    fake_for(idle).positions_map = {("TCS-EQ", "MIS"): _closed("TCS", 1.0)}

    reports.capture_traded_accounts()
    with session_scope() as db:
        assert [(r.account_name, r.symbol) for r in db.scalars(select(PnlRecord))] == [("Main", "SBIN-EQ")]


def test_reports_are_private(client, owner):
    acc = _account(client, owner)
    fake_for(acc).positions_map = {("SBIN-EQ", "MIS"): _closed("SBIN", 500.0)}
    client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)

    other = {"Authorization": f"Bearer {signup(client, 'friend@example.com').json()['token']}"}
    assert client.get("/api/reports", headers=other).json()["days"] == []
    t = _table(_sheet(client.get(f"/api/reports/{TODAY}.xlsx", headers=other)))
    assert t[1][1] == "No trades"
    assert client.get(f"/api/reports/{TODAY}.xlsx").status_code == 401


def test_bad_date_is_rejected(client, owner):
    assert client.get("/api/reports/yesterday.xlsx", headers=owner).status_code == 422


def test_saved_pnl_survives_kotak_clearing_the_day(client, owner):
    acc = _account(client, owner)
    fake_for(acc).positions_map = {("SBIN-EQ", "MIS"): _closed("SBIN", 75.0)}
    client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)
    fake_for(acc).positions_map = {("SBIN-EQ", "MIS"): _closed("SBIN", 80.0)}
    client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)  # later numbers replace earlier ones

    fake_for(acc).positions_map = {}  # evening: Kotak no longer lists the day's positions
    t = _table(_sheet(client.get(f"/api/reports/{TODAY}.xlsx", headers=owner)))
    assert t[1][1:] == ("Main", "SBIN", 80.0)
    assert client.get("/api/reports", headers=owner).json()["days"][0]["rows"] == 1
