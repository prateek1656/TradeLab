"""Databases created by an earlier version upgrade cleanly."""

import sqlite3

from sqlalchemy import create_engine, event, inspect

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
