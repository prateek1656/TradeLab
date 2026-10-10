from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_is_sqlite = _settings.db_url.startswith("sqlite")
engine = create_engine(
    _settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _rebuild(bind: Engine, name: str, defaults: dict[str, str]) -> None:
    """Recreate table `name` with the model's current schema, keeping its rows.

    SQLite can't change a unique constraint in place. New columns missing from the old
    table get the SQL expression in `defaults` (every new NOT NULL column needs one).
    Runs as one transaction on its own connection (DDL included), so a failure leaves
    the old table exactly as it was.
    """
    from sqlalchemy.schema import CreateIndex, CreateTable

    from . import models  # noqa: F401  (register tables)

    table = Base.metadata.tables[name]
    old = f"{name}__old"
    raw = bind.raw_connection()
    try:
        conn = raw.driver_connection
        saved = conn.isolation_level
        conn.isolation_level = None  # manual transactions, so the DDL below is part of one
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            def exists(t: str) -> bool:
                return cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone() is not None

            if exists(old):
                if exists(name):
                    raise RuntimeError(f"Both {name} and {old} exist; restore one of them by hand before upgrading")
                cur.execute(f'ALTER TABLE "{old}" RENAME TO "{name}"')  # left by an interrupted older upgrade
            old_cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{name}")').fetchall()]
            before = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            indexes = cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (name,)
            ).fetchall()
            for (index,) in indexes:
                cur.execute(f'DROP INDEX "{index}"')
            cur.execute(f'ALTER TABLE "{name}" RENAME TO "{old}"')
            cur.execute(str(CreateTable(table).compile(dialect=bind.dialect)))
            for index in table.indexes:
                cur.execute(str(CreateIndex(index).compile(dialect=bind.dialect)))
            keep = [c.name for c in table.columns if c.name in old_cols]
            extra = {c: v for c, v in defaults.items() if c in table.columns and c not in old_cols}
            cols = ", ".join(f'"{c}"' for c in [*keep, *extra])
            exprs = ", ".join([*(f'"{c}"' for c in keep), *extra.values()])
            cur.execute(f'INSERT INTO "{name}" ({cols}) SELECT {exprs} FROM "{old}"')
            after = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if after != before:
                raise RuntimeError(f"Upgrading {name} kept {after} of {before} rows")
            cur.execute(f'DROP TABLE "{old}"')
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            conn.isolation_level = saved
    finally:
        raw.close()


def upgrade_legacy_schema(bind: Engine | None = None) -> None:
    """Bring a database created by an older TradeLab version up to date."""
    bind = bind or engine
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    with bind.begin() as conn:
        if "automation_settings" in tables:
            cols = {c["name"] for c in insp.get_columns("automation_settings")}
            if "auto_entry" in cols:
                # Buying is now switched on per day with "Let's Trade".
                conn.execute(text("ALTER TABLE automation_settings DROP COLUMN auto_entry"))
            if "sizing_mode" not in cols:
                conn.execute(text(
                    "ALTER TABLE automation_settings ADD COLUMN sizing_mode VARCHAR(10) NOT NULL DEFAULT 'equal'"
                ))
            if "reserve_pct" not in cols:
                conn.execute(text("ALTER TABLE automation_settings ADD COLUMN reserve_pct FLOAT NOT NULL DEFAULT 0"))
        if "basket_items" in tables:
            cols = {c["name"] for c in insp.get_columns("basket_items")}
            if "trade_date" in cols:
                # Per-day stock lists were replaced by "Let's Trade" confirmations.
                conn.execute(text("DROP INDEX IF EXISTS ix_basket_items_trade_date"))
                conn.execute(text("ALTER TABLE basket_items DROP COLUMN trade_date"))
        if "holidays" in tables:
            cols = {c["name"] for c in insp.get_columns("holidays")}
            if "source" not in cols:
                conn.execute(text("ALTER TABLE holidays ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'manual'"))
        if "order_logs" in tables:
            cols = {c["name"] for c in insp.get_columns("order_logs")}
            for col, ddl in (("exchange_segment", "VARCHAR(10)"), ("product", "VARCHAR(8)"), ("section", "VARCHAR(16)"),
                             ("price", "FLOAT")):
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE order_logs ADD COLUMN {col} {ddl}"))
        if "execution_runs" in tables:
            cols = {c["name"] for c in insp.get_columns("execution_runs")}
            if "funds_short" not in cols:
                conn.execute(text("ALTER TABLE execution_runs ADD COLUMN funds_short JSON"))
            if "alert_seen" not in cols:
                conn.execute(text("ALTER TABLE execution_runs ADD COLUMN alert_seen BOOLEAN NOT NULL DEFAULT 0"))
        if "trade_sessions" in tables:
            cols = {c["name"] for c in insp.get_columns("trade_sessions")}
            if "account_ids" not in cols:
                conn.execute(text("ALTER TABLE trade_sessions ADD COLUMN account_ids JSON"))
        if "pnl_records" in tables:
            cols = {c["name"] for c in insp.get_columns("pnl_records")}
            for col, ddl in (
                ("buy_qty", "INTEGER NOT NULL DEFAULT 0"),
                ("sell_qty", "INTEGER NOT NULL DEFAULT 0"),
                ("buy_avg", "FLOAT NOT NULL DEFAULT 0"),
                ("sell_avg", "FLOAT NOT NULL DEFAULT 0"),
                ("pnl_complete", "BOOLEAN NOT NULL DEFAULT 1"),
            ):
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE pnl_records ADD COLUMN {col} {ddl}"))
        if "execution_runs" in tables and "section" not in {c["name"] for c in insp.get_columns("execution_runs")}:
            conn.execute(text("ALTER TABLE execution_runs ADD COLUMN section VARCHAR(16) NOT NULL DEFAULT 'intraday'"))
        if "users" in tables:
            # Columns used by older versions only; newer code doesn't write them.
            cols = {c["name"] for c in insp.get_columns("users")}
            for col in ("role", "status"):
                if col in cols:
                    conn.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))
    # Sections: lists and confirmations are kept per section (all older ones were intraday).
    insp = inspect(bind)
    if "basket_items" in tables and "section" not in {c["name"] for c in insp.get_columns("basket_items")}:
        _rebuild(bind, "basket_items", {
            "section": "'intraday'", "lot_size": "1", "exchange_segment": "'nse_cm'", "side": "'B'",
            "sizing_mode": "'qty'", "sizing_value": "1", "active": "1", "created_at": "CURRENT_TIMESTAMP",
        })
    if "trade_sessions" in tables and "section" not in {c["name"] for c in insp.get_columns("trade_sessions")}:
        _rebuild(bind, "trade_sessions", {
            "section": "'intraday'", "product": "'MIS'", "reserve_pct": "0", "sizing_mode": "'equal'",
            "confirmed_at": "CURRENT_TIMESTAMP",
        })


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    upgrade_legacy_schema()
    Base.metadata.create_all(engine)
