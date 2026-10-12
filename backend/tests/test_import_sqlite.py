"""Importing an older TradeLab's SQLite database into MongoDB (app/tools/import_sqlite.py)."""

import os
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["TRADELAB_DATA_DIR"] = tempfile.mkdtemp(prefix="tradelab-test-")
os.environ["TRADELAB_MONGO_URL"] = os.environ.get("TRADELAB_TEST_MONGO_URL", "mongodb://127.0.0.1:27017/?replicaSet=rs0")
os.environ["TRADELAB_MONGO_DB"] = f"tradelab_test_{os.getpid()}"  # never a real database

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Session, clear_all, database, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    AutomationSettings, BasketItem, BrokerAccount, ExecutionRun, Holiday, OrderLog, PnlRecord,
    SectionSettings, TradeSession, User,
)
from app.tools.import_sqlite import ImportRefused, import_sqlite, main  # noqa: E402

UTC = timezone.utc

# The last SQLite schema (as SQLAlchemy created it), with a bit of everything.
CURRENT = """
CREATE TABLE users (id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, email VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL, token_version INTEGER NOT NULL, created_at DATETIME NOT NULL,
  last_login_at DATETIME, PRIMARY KEY (id));
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE TABLE automation_settings (user_id INTEGER NOT NULL, entry_time VARCHAR(8) NOT NULL,
  exit_time VARCHAR(8) NOT NULL, auto_exit BOOLEAN NOT NULL, squareoff_scope VARCHAR(16) NOT NULL,
  sizing_mode VARCHAR(10) NOT NULL, reserve_pct FLOAT NOT NULL, PRIMARY KEY (user_id));
CREATE TABLE section_settings (user_id INTEGER NOT NULL, section VARCHAR(16) NOT NULL, entry_time VARCHAR(8),
  exit_time VARCHAR(8), auto_exit BOOLEAN NOT NULL, product VARCHAR(8) NOT NULL, sizing_mode VARCHAR(10) NOT NULL,
  reserve_pct FLOAT NOT NULL, max_lots INTEGER NOT NULL, account_ids JSON, PRIMARY KEY (user_id, section));
CREATE TABLE broker_accounts (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  name VARCHAR(80) NOT NULL, broker VARCHAR(16) NOT NULL, credentials TEXT, ucc_masked VARCHAR(32),
  client_name VARCHAR(120), automation_enabled BOOLEAN NOT NULL, status VARCHAR(16) NOT NULL, last_error TEXT,
  last_login_at DATETIME, paper_funds FLOAT NOT NULL, created_at DATETIME NOT NULL);
CREATE TABLE basket_items (id INTEGER NOT NULL, user_id INTEGER NOT NULL, section VARCHAR(16) NOT NULL,
  symbol VARCHAR(40) NOT NULL, trading_symbol VARCHAR(60) NOT NULL, token VARCHAR(20),
  exchange_segment VARCHAR(10) NOT NULL, name VARCHAR(160), side VARCHAR(1) NOT NULL, sizing_mode VARCHAR(10) NOT NULL,
  sizing_value FLOAT NOT NULL, active BOOLEAN NOT NULL, account_ids JSON, lot_size INTEGER NOT NULL, expiry DATE,
  instrument_type VARCHAR(12), option_type VARCHAR(4), strike FLOAT, created_at DATETIME NOT NULL, PRIMARY KEY (id),
  UNIQUE (user_id, section, exchange_segment, trading_symbol));
CREATE TABLE trade_sessions (id INTEGER NOT NULL, user_id INTEGER NOT NULL, section VARCHAR(16) NOT NULL,
  trade_date DATE NOT NULL, status VARCHAR(10) NOT NULL, items JSON NOT NULL, sizing_mode VARCHAR(10) NOT NULL,
  reserve_pct FLOAT NOT NULL, product VARCHAR(8) NOT NULL, account_ids JSON, confirmed_at DATETIME NOT NULL,
  note TEXT, PRIMARY KEY (id), UNIQUE (user_id, section, trade_date));
CREATE TABLE execution_runs (id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind VARCHAR(10) NOT NULL,
  section VARCHAR(16) NOT NULL, "trigger" VARCHAR(12) NOT NULL, trading_date DATE NOT NULL,
  status VARCHAR(12) NOT NULL, summary TEXT, started_at DATETIME NOT NULL, finished_at DATETIME, funds_short JSON,
  alert_seen BOOLEAN NOT NULL, PRIMARY KEY (id));
CREATE TABLE order_logs (id INTEGER NOT NULL, user_id INTEGER NOT NULL, run_id INTEGER, account_id INTEGER,
  account_name VARCHAR(80) NOT NULL, action VARCHAR(12) NOT NULL, symbol VARCHAR(60), quantity INTEGER,
  status VARCHAR(10) NOT NULL, order_id VARCHAR(40), message TEXT, exchange_segment VARCHAR(10), product VARCHAR(8),
  section VARCHAR(16), price FLOAT, created_at DATETIME NOT NULL, PRIMARY KEY (id));
CREATE TABLE pnl_records (id INTEGER NOT NULL, user_id INTEGER NOT NULL, trade_date DATE NOT NULL, account_id INTEGER,
  account_name VARCHAR(80) NOT NULL, symbol VARCHAR(60) NOT NULL, pnl FLOAT NOT NULL, buy_qty INTEGER NOT NULL,
  sell_qty INTEGER NOT NULL, buy_avg FLOAT NOT NULL, sell_avg FLOAT NOT NULL, pnl_complete BOOLEAN NOT NULL,
  captured_at DATETIME NOT NULL, PRIMARY KEY (id), UNIQUE (user_id, trade_date, account_id, symbol));
CREATE TABLE holidays (day DATE NOT NULL, description VARCHAR(120) NOT NULL, source VARCHAR(10) NOT NULL,
  PRIMARY KEY (day));
CREATE TABLE paper_positions (id INTEGER NOT NULL, account_id INTEGER NOT NULL, trading_date DATE NOT NULL,
  exchange_segment VARCHAR(10) NOT NULL, trading_symbol VARCHAR(60) NOT NULL, product VARCHAR(10) NOT NULL,
  buy_qty INTEGER NOT NULL, sell_qty INTEGER NOT NULL, buy_amt FLOAT NOT NULL, sell_amt FLOAT NOT NULL,
  PRIMARY KEY (id), UNIQUE (account_id, trading_date, exchange_segment, trading_symbol, product));

INSERT INTO users VALUES (1, 'Owner', 'o@x.com', 'h1', 2, '2026-10-01 09:15:00.123456', '2026-10-14 03:30:00');
INSERT INTO users VALUES (2, 'Second', 's@x.com', 'h2', 0, '2026-10-02 10:00:00', NULL);
INSERT INTO automation_settings VALUES (1, '09:20:00', '15:00:00', 0, 'intraday', 'custom', 2.5);
INSERT INTO section_settings VALUES (1, 'intraday', '09:15:00', '14:30:00', 1, 'MIS', 'equal', 0, 10, NULL);
INSERT INTO section_settings VALUES (1, 'fno', NULL, NULL, 0, 'NRML', 'custom', 5, 3, '[1, 3]');
INSERT INTO section_settings VALUES (2, 'holding', NULL, NULL, 1, 'CNC', 'equal', 0, 10, '[]');
INSERT INTO broker_accounts VALUES (1, 1, 'Main', 'kotak', NULL, 'AB•••', 'Test Client', 1, 'connected', NULL,
  '2026-10-14 03:00:00', 0, '2026-10-01 10:00:00');
INSERT INTO broker_accounts VALUES (3, 1, 'Practice', 'paper', NULL, NULL, NULL, 0, 'error', 'Login failed', NULL,
  100000, '2026-10-02 10:00:00');
INSERT INTO broker_accounts VALUES (4, 2, 'Deleted later', 'paper', NULL, NULL, NULL, 1, 'connected', NULL, NULL,
  0, '2026-10-03 10:00:00');
DELETE FROM broker_accounts WHERE id = 4;
INSERT INTO basket_items VALUES (1, 1, 'intraday', 'SBIN', 'SBIN-EQ', '3045', 'nse_cm', 'State Bank', 'B', 'qty', 5,
  1, NULL, 1, NULL, NULL, NULL, NULL, '2026-10-10 04:00:00');
INSERT INTO basket_items VALUES (2, 1, 'fno', 'NIFTY', 'NIFTY26OCT25000CE', '45678', 'nse_fo', 'NIFTY', 'S', 'lots',
  2, 0, '[1]', 75, '2026-10-27', 'OPTIDX', 'CE', 25000, '2026-10-11 05:30:00.500000');
INSERT INTO trade_sessions VALUES (1, 1, 'intraday', '2026-10-14', 'armed', '[{"symbol": "SBIN", "qty": 5}]',
  'equal', 1.5, 'MIS', '[1, 3]', '2026-10-13 12:00:00', NULL);
INSERT INTO trade_sessions VALUES (2, 1, 'fno', '2026-10-14', 'cancelled', '[]', 'custom', 0, 'NRML', NULL,
  '2026-10-13 12:05:00', 'Cancelled by you');
INSERT INTO execution_runs VALUES (1, 1, 'entry', 'intraday', 'scheduled', '2026-10-14', 'partial', '1 of 2 bought',
  '2026-10-14 03:45:00', '2026-10-14 03:46:00.250000', '["Practice"]', 1);
INSERT INTO execution_runs VALUES (2, 1, 'exit', 'all', 'manual', '2026-10-14', 'running', NULL,
  '2026-10-14 09:00:00', NULL, 'null', 0);
INSERT INTO order_logs VALUES (1, 1, 1, 1, 'Main', 'buy', 'SBIN-EQ', 5, 'success', 'ORD1', 'Order placed', 'nse_cm',
  'MIS', 'intraday', 801.25, '2026-10-14 03:45:01');
INSERT INTO order_logs VALUES (2, 1, NULL, NULL, 'Gone', 'login', NULL, NULL, 'failed', NULL, 'Invalid MPIN', NULL,
  NULL, NULL, NULL, '2026-10-14 03:40:00');
INSERT INTO pnl_records VALUES (1, 1, '2026-10-14', 1, 'Main', 'SBIN-EQ', 12.5, 5, 5, 801.25, 803.75, 1,
  '2026-10-14 10:00:00');
INSERT INTO pnl_records VALUES (2, 1, '2026-10-14', NULL, 'Gone', 'SBIN-EQ', -3, 1, 0, 100, 0, 0, '2026-10-14 10:00:00');
INSERT INTO pnl_records VALUES (3, 1, '2026-10-14', NULL, 'Gone too', 'SBIN-EQ', 1, 1, 1, 100, 101, 1,
  '2026-10-14 10:00:00');
INSERT INTO holidays VALUES ('2026-11-09', 'Diwali', 'nse');
INSERT INTO holidays VALUES ('2026-12-25', 'Christmas', 'manual');
INSERT INTO paper_positions VALUES (7, 3, '2026-10-14', 'nse_cm', 'SBIN-EQ', 'MIS', 5, 0, 4006.25, 0);
"""

# A database from the first versions: user roles, "auto entry", per-day stock lists, no sections, fewer columns.
LEGACY = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(255) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL,
  token_version INTEGER NOT NULL, created_at DATETIME NOT NULL, last_login_at DATETIME);
CREATE TABLE automation_settings (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  entry_time VARCHAR(8) NOT NULL, exit_time VARCHAR(8) NOT NULL, auto_entry BOOLEAN NOT NULL,
  auto_exit BOOLEAN NOT NULL, squareoff_scope VARCHAR(16) NOT NULL);
CREATE TABLE broker_accounts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name VARCHAR(80) NOT NULL,
  broker VARCHAR(16) NOT NULL, credentials TEXT, ucc_masked VARCHAR(32), client_name VARCHAR(120),
  automation_enabled BOOLEAN NOT NULL, status VARCHAR(16) NOT NULL, last_error TEXT, last_login_at DATETIME,
  paper_funds FLOAT NOT NULL, created_at DATETIME NOT NULL);
CREATE TABLE basket_items (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol VARCHAR(40) NOT NULL, trading_symbol VARCHAR(60) NOT NULL, trade_date DATE);
CREATE INDEX ix_basket_items_trade_date ON basket_items (trade_date);
CREATE TABLE trade_sessions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, trade_date DATE NOT NULL,
  status VARCHAR(10) NOT NULL, items JSON NOT NULL, sizing_mode VARCHAR(10) NOT NULL, reserve_pct FLOAT NOT NULL,
  confirmed_at DATETIME NOT NULL, note TEXT);
CREATE TABLE execution_runs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind VARCHAR(10) NOT NULL,
  "trigger" VARCHAR(12) NOT NULL, trading_date DATE NOT NULL, status VARCHAR(12) NOT NULL, summary TEXT,
  started_at DATETIME NOT NULL, finished_at DATETIME);
CREATE TABLE order_logs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, run_id INTEGER, account_id INTEGER,
  account_name VARCHAR(80) NOT NULL, action VARCHAR(12) NOT NULL, symbol VARCHAR(60), quantity INTEGER,
  status VARCHAR(10) NOT NULL, order_id VARCHAR(40), message TEXT, created_at DATETIME NOT NULL);
CREATE TABLE pnl_records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, trade_date DATE NOT NULL,
  account_id INTEGER, account_name VARCHAR(80) NOT NULL, symbol VARCHAR(60) NOT NULL, pnl FLOAT NOT NULL,
  captured_at DATETIME NOT NULL);
CREATE TABLE holidays (day DATE PRIMARY KEY, description VARCHAR(120) NOT NULL);

INSERT INTO users VALUES (1, 'Owner', 'o@x.com', 'h', 'admin', 'approved', 0, '2026-01-01', NULL);
INSERT INTO users VALUES (2, 'Waiting', 'w@x.com', 'h', 'member', 'pending', 0, '2026-01-01', NULL);
INSERT INTO users VALUES (3, 'Revoked', 'r@x.com', 'h', 'member', 'revoked', 1, '2026-01-01 08:00:00', NULL);
INSERT INTO automation_settings VALUES (1, '09:15:00', '14:30:00', 1, 1, 'all');
INSERT INTO automation_settings VALUES (2, '09:16:00', '14:31:00', 0, 0, 'intraday');
INSERT INTO broker_accounts VALUES (1, 1, 'Main', 'kotak', NULL, NULL, NULL, 1, 'connected', NULL, NULL, 0,
  '2026-01-02 10:00:00');
INSERT INTO broker_accounts VALUES (2, 1, 'Second', 'paper', NULL, NULL, NULL, 1, 'connected', NULL, NULL, 50000,
  '2026-01-02 10:05:00');
INSERT INTO basket_items VALUES (1, 1, 'SBIN', 'SBIN-EQ', '2026-10-09');
INSERT INTO trade_sessions VALUES (1, 1, '2026-10-14', 'armed', '[]', 'equal', 0, '2026-10-13 12:00:00', NULL);
INSERT INTO execution_runs VALUES (1, 1, 'entry', 'scheduled', '2026-10-14', 'done', 'All bought',
  '2026-10-14 03:45:00', '2026-10-14 03:45:30');
INSERT INTO order_logs VALUES (1, 1, 1, 1, 'Main', 'buy', 'SBIN-EQ', 5, 'success', 'ORD1', 'Order placed',
  '2026-10-14 03:45:01');
INSERT INTO pnl_records VALUES (1, 1, '2026-10-14', 1, 'Main', 'SBIN-EQ', 42.5, '2026-10-14 09:00:00');
INSERT INTO holidays VALUES ('2026-11-09', 'Diwali');
"""


@pytest.fixture(autouse=True)
def _empty_database():
    # Also runs on its own (`pytest --noconftest tests/test_import_sqlite.py`).
    init_db()
    clear_all()
    yield


def _sqlite(tmp_path, script, name="tradelab.db"):
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.executescript(script)
    con.close()
    return path


def _raw(collection):
    return list(database()[collection].find({}, sort=[("_id", 1)]))


def _counters():
    return {d["_id"]: d["seq"] for d in database()["counters"].find()}


def test_current_schema_is_copied_with_ids_values_and_counters(tmp_path):
    report = import_sqlite(_sqlite(tmp_path, CURRENT))
    counts = {c.collection: c.count for c in report.collections}
    assert counts == {"users": 2, "automation_settings": 1, "section_settings": 3, "broker_accounts": 2,
                      "basket_items": 2, "trade_sessions": 2, "execution_runs": 2, "order_logs": 2,
                      "pnl_records": 3, "holidays": 2}
    # Practice (paper) accounts are gone: their funds column is left out and the account removed after copying.
    assert {c.collection: c.dropped for c in report.collections if c.dropped} == {"broker_accounts": ["paper_funds"]}
    assert report.practice_removed == 1

    with session_scope() as db:
        owner = db.get(User, 1)
        assert (owner.name, owner.email, owner.password_hash, owner.token_version) == ("Owner", "o@x.com", "h1", 2)
        # Naive UTC text -> timezone-aware UTC (MongoDB keeps milliseconds).
        assert owner.created_at == datetime(2026, 10, 1, 9, 15, 0, 123000, tzinfo=UTC)
        assert owner.last_login_at == datetime(2026, 10, 14, 3, 30, tzinfo=UTC)
        assert owner.is_owner and not db.get(User, 2).is_owner
        assert db.get(User, 2).last_login_at is None

        auto = db.get(AutomationSettings, 1)
        assert (auto.entry_time, auto.exit_time, auto.auto_exit, auto.squareoff_scope, auto.sizing_mode,
                auto.reserve_pct) == ("09:20:00", "15:00:00", False, "intraday", "custom", 2.5)
        assert db.get(AutomationSettings, 2) is None

        fno = db.get(SectionSettings, (1, "fno"))
        assert (fno.entry_time, fno.auto_exit, fno.product, fno.max_lots, fno.reserve_pct, fno.account_ids) == (
            None, False, "NRML", 3, 5.0, [1])  # without the practice account 3
        assert db.get(SectionSettings, (1, "intraday")).account_ids is None  # every account
        assert db.get(SectionSettings, (2, "holding")).account_ids == []  # no account
        assert [s.section for s in db.find(SectionSettings, {"user_id": 1}, sort=[("section", 1)])] == ["fno", "intraday"]

        main_acc = db.get(BrokerAccount, 1)
        assert (main_acc.broker, main_acc.automation_enabled, main_acc.ucc_masked, main_acc.client_name) == (
            "kotak", True, "AB•••", "Test Client")
        assert db.get(BrokerAccount, 3) is None and db.count(BrokerAccount) == 1  # the practice account

        sbin, nifty = db.get(BasketItem, 1), db.get(BasketItem, 2)
        assert (sbin.section, sbin.token, sbin.active, sbin.account_ids, sbin.expiry, sbin.strike) == (
            "intraday", "3045", True, None, None, None)
        assert (nifty.section, nifty.side, nifty.sizing_mode, nifty.sizing_value, nifty.active, nifty.account_ids,
                nifty.lot_size, nifty.expiry, nifty.option_type, nifty.strike) == (
            "fno", "S", "lots", 2.0, False, [1], 75, date(2026, 10, 27), "CE", 25000.0)
        assert nifty.created_at == datetime(2026, 10, 11, 5, 30, 0, 500000, tzinfo=UTC)

        armed = db.find_one(TradeSession, {"trade_date": date(2026, 10, 14), "section": "intraday"})
        assert armed.id == 1 and armed.items == [{"symbol": "SBIN", "qty": 5}] and armed.account_ids == [1]
        assert (armed.status, armed.reserve_pct, armed.product) == ("armed", 1.5, "MIS")
        assert db.get(TradeSession, 2).note == "Cancelled by you" and db.get(TradeSession, 2).account_ids is None

        run = db.get(ExecutionRun, 1)
        assert (run.trigger, run.trading_date, run.status, run.funds_short, run.alert_seen) == (
            "scheduled", date(2026, 10, 14), "partial", ["Practice"], True)
        assert run.finished_at == datetime(2026, 10, 14, 3, 46, 0, 250000, tzinfo=UTC)
        running = db.get(ExecutionRun, 2)
        assert (running.section, running.finished_at, running.funds_short, running.alert_seen) == ("all", None, None, False)

        buy = db.get(OrderLog, 1)
        assert (buy.run_id, buy.account_id, buy.quantity, buy.section, buy.price) == (1, 1, 5, "intraday", 801.25)
        login = db.get(OrderLog, 2)
        assert (login.run_id, login.account_id, login.symbol, login.section, login.price) == (None, None, None, None, None)
        assert db.count(OrderLog, {"account_id": None}) == 1

        assert db.get(PnlRecord, 2).pnl_complete is False and db.get(PnlRecord, 1).sell_avg == 803.75
        # Two records of deleted accounts for the same stock and day are fine, as in SQL.
        assert db.count(PnlRecord, {"trade_date": date(2026, 10, 14), "account_id": None}) == 2

        diwali = db.get(Holiday, date(2026, 11, 9))
        assert (diwali.description, diwali.source) == ("Diwali", "nse")

    # Stored form: natural keys as _id, dates as text, no stray "id" field.
    assert [d["_id"] for d in _raw("holidays")] == ["2026-11-09", "2026-12-25"]
    assert [d["_id"] for d in _raw("automation_settings")] == [1]
    assert [d["_id"] for d in _raw("section_settings")] == ["1|fno", "1|intraday", "2|holding"]
    assert _raw("trade_sessions")[0]["trade_date"] == "2026-10-14" and "id" not in _raw("trade_sessions")[0]

    # Counters: the highest id ever used (account 4 was deleted, so 4 is never handed out again).
    assert _counters() == {"users": 2, "broker_accounts": 4, "basket_items": 2, "trade_sessions": 2,
                           "execution_runs": 2, "order_logs": 2, "pnl_records": 3}
    assert "paper_positions" not in database().list_collection_names()
    with session_scope() as db:
        user = User(name="New", email="n@x.com", password_hash="h")
        acc = BrokerAccount(user_id=1, name="New account")
        item = BasketItem(user_id=1, symbol="TCS", trading_symbol="TCS-EQ")
        db.add_all([user, acc, item])
    assert (user.id, acc.id, item.id) == (3, 5, 3)


def test_old_schema_gets_defaults_and_loses_old_columns(tmp_path):
    report = import_sqlite(_sqlite(tmp_path, LEGACY))
    by_name = {c.collection: c for c in report.collections}
    assert by_name["users"].dropped == ["role", "status"]
    assert by_name["automation_settings"].dropped == ["auto_entry"]
    assert by_name["basket_items"].dropped == ["trade_date"]
    assert by_name["section_settings"].table is None and by_name["section_settings"].count == 0
    assert by_name["broker_accounts"].dropped == ["paper_funds"]
    assert "paper_positions" not in by_name and report.practice_removed == 1

    raw_users = _raw("users")
    # Everyone keeps their account (pending or revoked included); roles are gone.
    assert [u["email"] for u in raw_users] == ["o@x.com", "w@x.com", "r@x.com"]
    assert not {"role", "status"} & set(raw_users[0])
    assert "auto_entry" not in _raw("automation_settings")[0]
    assert "trade_date" not in _raw("basket_items")[0]
    now = datetime.now(UTC)

    with session_scope() as db:
        assert db.get(User, 1).created_at == datetime(2026, 1, 1, tzinfo=UTC)  # date-only text
        assert db.get(User, 3).created_at == datetime(2026, 1, 1, 8, tzinfo=UTC) and db.get(User, 3).token_version == 1

        a1, a2 = db.get(AutomationSettings, 1), db.get(AutomationSettings, 2)
        assert (a1.sizing_mode, a1.reserve_pct, a1.auto_exit) == ("equal", 0.0, True)
        assert (a2.entry_time, a2.exit_time, a2.auto_exit, a2.squareoff_scope) == ("09:16:00", "14:31:00", False, "intraday")

        item = db.get(BasketItem, 1)
        assert (item.section, item.exchange_segment, item.side, item.sizing_mode, item.sizing_value, item.active,
                item.lot_size, item.token, item.account_ids, item.expiry) == (
            "intraday", "nse_cm", "B", "qty", 1, True, 1, None, None, None)
        assert now - timedelta(minutes=5) < item.created_at <= now + timedelta(seconds=1)  # wasn't kept back then

        sess = db.get(TradeSession, 1)
        assert (sess.section, sess.product, sess.account_ids, sess.items, sess.sizing_mode) == (
            "intraday", "MIS", None, [], "equal")
        assert sess.confirmed_at == datetime(2026, 10, 13, 12, tzinfo=UTC)

        run = db.get(ExecutionRun, 1)
        assert (run.section, run.funds_short, run.alert_seen, run.summary) == ("intraday", None, False, "All bought")

        log = db.get(OrderLog, 1)
        # Older order rows have no section (they're all Intraday ones, as before) and no price yet.
        assert (log.section, log.exchange_segment, log.product, log.price) == (None, None, None, None)

        pnl = db.get(PnlRecord, 1)
        assert (pnl.pnl, pnl.buy_qty, pnl.sell_qty, pnl.buy_avg, pnl.sell_avg, pnl.pnl_complete) == (
            42.5, 0, 0, 0.0, 0.0, True)

        assert db.get(Holiday, date(2026, 11, 9)).source == "manual"
        assert [a.id for a in db.find(BrokerAccount, {})] == [1]  # "Second" was a practice account

    assert _counters() == {"users": 3, "broker_accounts": 2, "basket_items": 1, "trade_sessions": 1,
                           "execution_runs": 1, "order_logs": 1, "pnl_records": 1}
    with session_scope() as db:
        item = BasketItem(user_id=1, symbol="TCS", trading_symbol="TCS-EQ")
        settings = SectionSettings(user_id=1, section="fno", product="NRML")
        db.add_all([item, settings])
    assert item.id == 2 and Session().get(SectionSettings, (1, "fno")).product == "NRML"


def test_refuses_to_mix_with_existing_records_unless_replace(tmp_path):
    path = _sqlite(tmp_path, CURRENT)
    import_sqlite(path)
    with session_scope() as db:
        db.add(User(name="Made in MongoDB", email="m@x.com", password_hash="h"))
    with pytest.raises(ImportRefused, match="already holds records.*users: 3"):
        import_sqlite(path)
    assert database()["users"].count_documents({}) == 3  # nothing changed

    report = import_sqlite(path, replace=True)
    assert report.total == 21
    assert [u["email"] for u in _raw("users")] == ["o@x.com", "s@x.com"]
    assert _counters()["users"] == 2  # the counter starts again from the file
    with session_scope() as db:
        user = User(name="Next", email="n@x.com", password_hash="h")
        db.add(user)
    assert user.id == 3


def test_a_failed_copy_leaves_nothing_behind(tmp_path):
    # Two users with the same email can't be stored (MongoDB's unique rule), so the copy stops half-way.
    path = _sqlite(tmp_path, CURRENT.replace("CREATE UNIQUE INDEX ix_users_email ON users (email);", "")
                   + "INSERT INTO users VALUES (9, 'Twin', 'o@x.com', 'h', 0, '2026-10-03 10:00:00', NULL);")
    with pytest.raises(ImportRefused, match="copying users, so nothing was imported.*duplicate key"):
        import_sqlite(path)
    assert all(database()[name].count_documents({}) == 0 for name in ("users", "counters"))


def test_unreadable_values_and_files_stop_before_mongodb_is_touched(tmp_path):
    bad = _sqlite(tmp_path, CURRENT + "UPDATE trade_sessions SET items = 'not json' WHERE id = 2;")
    with pytest.raises(ImportRefused, match=r"trade_sessions \(id=2\): can't read items"):
        import_sqlite(bad)
    assert database()["users"].count_documents({}) == 0
    with pytest.raises(ImportRefused, match="no such file"):
        import_sqlite(tmp_path / "missing.db")
    with pytest.raises(ImportRefused, match="isn't a TradeLab database"):
        import_sqlite(_sqlite(tmp_path, "CREATE TABLE notes (id INTEGER PRIMARY KEY);", "other.db"))
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database at all " * 50)
    with pytest.raises(ImportRefused, match="can't be read"):
        import_sqlite(junk)


def test_table_left_by_an_interrupted_upgrade_is_read(tmp_path):
    path = _sqlite(tmp_path, LEGACY + "ALTER TABLE basket_items RENAME TO basket_items__old;")
    report = import_sqlite(path)
    basket = next(c for c in report.collections if c.collection == "basket_items")
    assert (basket.table, basket.count) == ("basket_items__old", 1)
    assert Session().get(BasketItem, 1).symbol == "SBIN"

    clear_all()
    both = _sqlite(tmp_path, LEGACY + "CREATE TABLE basket_items__old (id INTEGER PRIMARY KEY);", "both.db")
    with pytest.raises(ImportRefused, match="both basket_items and basket_items__old"):
        import_sqlite(both)


def test_command_line(tmp_path, capsys):
    path = _sqlite(tmp_path, LEGACY)
    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert f'database "{get_settings().mongo_db}"' in out
    assert re.search(r"users +3   \(left out old columns: role, status\)", out)
    assert re.search(r"broker_accounts +2   \(left out old columns: paper_funds\)", out)
    assert re.search(r"section_settings +0   \(no such table in the SQLite file\)", out)
    assert "13 records in all" in out
    assert "Removed 1 practice (paper) account(s): TradeLab trades only Kotak Neo accounts." in out

    assert main([str(path)]) == 1
    assert "Not imported: MongoDB" in capsys.readouterr().err
    assert main([str(path), "--replace"]) == 0


def test_warns_when_broker_logins_need_another_secret_key(tmp_path, capsys):
    current = Fernet(get_settings().fernet_key()).encrypt(b'{"mpin": "123456"}').decode()
    path = _sqlite(tmp_path, CURRENT + f"UPDATE broker_accounts SET credentials = '{current}';")
    assert import_sqlite(path).warnings == []
    assert Session().get(BrokerAccount, 1).credentials == current  # copied as stored

    other = Fernet(Fernet.generate_key()).encrypt(b'{"mpin": "123456"}').decode()
    path = _sqlite(tmp_path, CURRENT + f"UPDATE broker_accounts SET credentials = '{other}';", "other.db")
    assert main([str(path), "--replace"]) == 0
    assert "Warning: The broker logins were saved with a different secret key" in capsys.readouterr().out


def test_holidays_the_new_app_downloaded_itself_dont_block_the_import(tmp_path):
    """The new version starts first (and downloads NSE's holidays); the import must still go ahead."""
    with session_scope() as db:
        db.add(Holiday(day=date(2026, 11, 9), description="Diwali", source="nse"))
        db.add(Holiday(day=date(2026, 12, 31), description="Detected closed", source="detected"))
    report = import_sqlite(_sqlite(tmp_path, CURRENT))
    assert report.total == 21
    days = [h["_id"] for h in _raw("holidays")]
    assert days == ["2026-11-09", "2026-12-25"]  # exactly the file's own holidays
    with session_scope() as db:
        db.add(Holiday(day=date(2027, 1, 26), description="Mine"))  # a holiday added by hand does count
    with pytest.raises(ImportRefused, match="already holds records"):
        import_sqlite(_sqlite(tmp_path, CURRENT, name="again.db"))
