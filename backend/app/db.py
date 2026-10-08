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


def upgrade_legacy_schema(bind: Engine | None = None) -> None:
    """Bring a database created by an older TradeLab version up to date."""
    bind = bind or engine
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    with bind.begin() as conn:
        if "automation_settings" in tables:
            cols = {c["name"] for c in insp.get_columns("automation_settings")}
            if "sizing_mode" not in cols:
                conn.execute(text(
                    "ALTER TABLE automation_settings ADD COLUMN sizing_mode VARCHAR(10) NOT NULL DEFAULT 'equal'"
                ))
            if "reserve_pct" not in cols:
                conn.execute(text("ALTER TABLE automation_settings ADD COLUMN reserve_pct FLOAT NOT NULL DEFAULT 0"))
        if "basket_items" in tables:
            cols = {c["name"] for c in insp.get_columns("basket_items")}
            if "trade_date" not in cols:
                # Older lists had no date: they stay visible but never trade by themselves.
                conn.execute(text("ALTER TABLE basket_items ADD COLUMN trade_date DATE"))
        if "users" in tables:
            # Columns used by older versions only; newer code doesn't write them.
            cols = {c["name"] for c in insp.get_columns("users")}
            for col in ("role", "status"):
                if col in cols:
                    conn.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    upgrade_legacy_schema()
    Base.metadata.create_all(engine)
