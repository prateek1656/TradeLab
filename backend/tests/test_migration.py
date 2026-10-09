"""Databases created by an earlier version upgrade cleanly."""

import sqlite3

from sqlalchemy import create_engine, event, inspect, text

from app.db import upgrade_legacy_schema

LEGACY = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(255) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL,
  token_version INTEGER NOT NULL, created_at DATETIME NOT NULL, last_login_at DATETIME);
CREATE TABLE automation_settings (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  entry_time VARCHAR(8) NOT NULL, exit_time VARCHAR(8) NOT NULL, auto_entry BOOLEAN NOT NULL,
  auto_exit BOOLEAN NOT NULL, squareoff_scope VARCHAR(16) NOT NULL);
CREATE TABLE basket_items (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol VARCHAR(40) NOT NULL, trading_symbol VARCHAR(60) NOT NULL, trade_date DATE);
CREATE INDEX ix_basket_items_trade_date ON basket_items (trade_date);
CREATE TABLE holidays (day DATE PRIMARY KEY, description VARCHAR(120) NOT NULL);
INSERT INTO holidays VALUES ('2026-11-09', 'Diwali');
INSERT INTO users VALUES (1,'Owner','o@x.com','h','admin','approved',0,'2026-01-01',NULL);
INSERT INTO users VALUES (2,'Waiting','w@x.com','h','member','pending',0,'2026-01-01',NULL);
INSERT INTO users VALUES (3,'Revoked','r@x.com','h','member','revoked',0,'2026-01-01',NULL);
INSERT INTO automation_settings VALUES (1,'09:15:00','14:30:00',1,1,'all');
INSERT INTO automation_settings VALUES (2,'09:15:00','14:30:00',1,1,'all');
INSERT INTO basket_items VALUES (1, 1, 'SBIN', 'SBIN-EQ', '2026-10-09');
"""


def test_legacy_database_upgrade(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY)
    con.close()
    engine = create_engine(f"sqlite:///{path}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))

    upgrade_legacy_schema(engine)
    upgrade_legacy_schema(engine)  # running twice is harmless

    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("users")} >= {"id", "email", "token_version"}
    assert not {"role", "status"} & {c["name"] for c in insp.get_columns("users")}
    with engine.connect() as c:
        # Everyone keeps their account; new settings get their defaults.
        assert [r[0] for r in c.exec_driver_sql("SELECT email FROM users ORDER BY id")] == ["o@x.com", "w@x.com", "r@x.com"]
        rows = c.exec_driver_sql("SELECT user_id, sizing_mode, reserve_pct FROM automation_settings ORDER BY user_id").all()
        assert rows == [(1, "equal", 0), (2, "equal", 0)]
        assert "trade_date" not in {r[1] for r in c.exec_driver_sql("PRAGMA table_info(basket_items)")}
        assert "auto_entry" not in {r[1] for r in c.exec_driver_sql("PRAGMA table_info(automation_settings)")}
        assert c.exec_driver_sql("SELECT day, source FROM holidays").all() == [("2026-11-09", "manual")]
        c.exec_driver_sql("INSERT INTO automation_settings (user_id, entry_time, exit_time, auto_exit, "
                          "squareoff_scope) VALUES (3, '09:15:00', '14:30:00', 1, 'all')")
        c.exec_driver_sql("INSERT INTO users (name, email, password_hash, token_version, created_at) "
                          "VALUES ('New', 'n@x.com', 'h', 0, '2026-01-02')")  # new sign-ups still work


def test_trade_sessions_gain_the_account_snapshot(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE trade_sessions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, trade_date DATE NOT NULL,"
            " status VARCHAR(10) NOT NULL, items JSON NOT NULL, sizing_mode VARCHAR(10) NOT NULL,"
            " reserve_pct FLOAT NOT NULL, confirmed_at DATETIME NOT NULL, note TEXT)"))
        conn.execute(text("INSERT INTO trade_sessions VALUES (1, 1, '2026-10-14', 'armed', '[]', 'equal', 0,"
                          " '2026-10-13 12:00:00', NULL)"))
    upgrade_legacy_schema(eng)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT account_ids FROM trade_sessions")).scalar() is None  # old: no limit


def test_pnl_records_gain_quantity_and_price_columns(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE pnl_records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, trade_date DATE NOT NULL,"
            " account_id INTEGER, account_name VARCHAR(80) NOT NULL, symbol VARCHAR(60) NOT NULL, pnl FLOAT NOT NULL,"
            " captured_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO pnl_records VALUES (1, 1, '2026-10-14', 1, 'Main', 'SBIN-EQ', 42.5,"
                          " '2026-10-14 09:00:00')"))
    upgrade_legacy_schema(eng)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT pnl, buy_qty, sell_qty, buy_avg, sell_avg, pnl_complete FROM pnl_records")).one()
    assert tuple(row) == (42.5, 0, 0, 0.0, 0.0, 1)


def test_failed_table_rebuild_changes_nothing(tmp_path, monkeypatch):
    """Upgrading a table is all-or-nothing: if copying the rows fails, the old table is untouched."""
    from app import db as db_module

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY)
    con.close()
    engine = create_engine(f"sqlite:///{path}")
    real = db_module._rebuild

    def broken(bind, name, defaults):
        # Leave out the default for a NOT NULL column, so the copy step fails half-way.
        real(bind, name, {k: v for k, v in defaults.items() if k != "lot_size"} | {"lot_size": "NULL"})

    monkeypatch.setattr(db_module, "_rebuild", broken)
    try:
        upgrade_legacy_schema(engine)
    except Exception:  # noqa: BLE001
        pass
    tables = set(inspect(engine).get_table_names())
    assert "basket_items" in tables and "basket_items__old" not in tables
    with engine.connect() as c:
        assert c.execute(text("SELECT symbol FROM basket_items")).scalars().all() == ["SBIN"]
        assert "section" not in [r[1] for r in c.execute(text("PRAGMA table_info(basket_items)"))]
    monkeypatch.setattr(db_module, "_rebuild", real)
    upgrade_legacy_schema(engine)  # and a later upgrade still works
    with engine.connect() as c:
        assert c.execute(text("SELECT section, symbol FROM basket_items")).all() == [("intraday", "SBIN")]
