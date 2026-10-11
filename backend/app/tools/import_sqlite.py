"""One-time import of an older TradeLab's SQLite database into MongoDB.

    python -m app.tools.import_sqlite /path/to/tradelab.db [--replace]

Before MongoDB, TradeLab kept everything in one SQLite file (`tradelab.db` in the data folder).
This copies every table of such a file, from any older version, into the MongoDB database in the
settings (TRADELAB_MONGO_URL, TRADELAB_MONGO_DB), stored exactly as the app stores records today:

* records keep their ids, and each collection's counter continues after the highest id ever used
  (including SQLite's own memory of deleted accounts), so ids are still never reused;
* columns an older version didn't have get today's defaults (stock lists, confirmations and runs
  from before sections are Intraday ones), and columns that are gone (user roles, per-day stock
  lists, "auto entry") are left out, as the old SQLite upgrade did;
* a table left behind by an interrupted older upgrade (`<table>__old`) is read in its place.

Stop TradeLab while importing. The SQLite file is only read. The import refuses to run when MongoDB
already holds records, unless --replace is given, which deletes them first. If copying fails
half-way, what was copied is removed again, so the import can simply be run again.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import types
import typing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pymongo.errors import BulkWriteError, PyMongoError

from .. import models  # noqa: F401  (register documents)
from ..config import get_settings
from ..db import REGISTRY, Document, clear_all, database, init_db

# Records written to MongoDB per request.
BATCH = 1000

# Values the old SQLite upgrade gave columns an older table lacked, where today's record has no
# default of its own (every other missing column gets the record's default).
OLD_DEFAULTS: dict[str, dict[str, Any]] = {"trade_sessions": {"sizing_mode": "equal"}}


class ImportRefused(Exception):
    """The import didn't happen; MongoDB is as it was (minus what --replace deleted)."""


@dataclass
class Imported:
    """What was copied into one collection."""

    collection: str
    count: int = 0
    table: str | None = None  # the SQLite table read (None: the file has no such table)
    dropped: list[str] = field(default_factory=list)  # old columns that were left out


@dataclass
class Report:
    collections: list[Imported]
    warnings: list[str]

    @property
    def total(self) -> int:
        return sum(c.count for c in self.collections)


# ───────────────────────────────────────────── reading SQLite

def _kind(hint: Any) -> str:
    """How a field's SQLite value is read: datetime, date, bool, int, float, json or str."""
    options = typing.get_args(hint) if typing.get_origin(hint) in (typing.Union, types.UnionType) else (hint,)
    opt = next(o for o in options if o is not type(None))
    for py, kind in ((datetime, "datetime"), (date, "date"), (bool, "bool"), (int, "int"), (float, "float")):
        if opt is py:
            return kind
    if opt in (list, dict) or typing.get_origin(opt) in (list, dict):
        return "json"
    return "str"


def _value(raw: Any, kind: str) -> Any:
    """A value as SQLite stored it -> the Python value today's record holds."""
    if raw is None:
        return None
    if isinstance(raw, bytes) and kind != "json":
        raw = raw.decode()
    if kind == "datetime":
        # Saved as naive UTC text ("2026-10-14 09:15:00.123456"); MongoDB keeps milliseconds.
        when = datetime.fromisoformat(str(raw).strip())
        return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
    if kind == "date":
        text = str(raw).strip()
        try:
            return date.fromisoformat(text)
        except ValueError:
            return datetime.fromisoformat(text).date()
    if kind == "json":
        return json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if kind == "bool":
        if isinstance(raw, str):
            flags = {"1": True, "0": False, "true": True, "false": False}
            if raw.strip().lower() not in flags:
                raise ValueError("not a yes/no value")
            return flags[raw.strip().lower()]
        return bool(raw)
    if kind == "int":
        number = int(raw)
        if number != float(raw):
            raise ValueError("not a whole number")
        return number
    if kind == "float":
        return float(raw)
    return raw if isinstance(raw, str) else str(raw)


def _read(con: sqlite3.Connection, tables: set[str], cls: type[Document]) -> tuple[Imported, list[dict], int]:
    """One table's rows in stored form (ordered by key), and the highest id it ever used."""
    name = cls.__collection__
    item = Imported(name)
    table = name
    if f"{name}__old" in tables:
        if name in tables:
            raise ImportRefused(f"The SQLite file has both {name} and {name}__old (left by an interrupted "
                                "upgrade); keep one of them by hand, then import again")
        table = f"{name}__old"
    elif name not in tables:
        return item, [], 0
    item.table = table
    columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    item.dropped = [c for c in columns if c not in cls._fields]
    hints = typing.get_type_hints(cls)
    kinds = {f: _kind(hints[f]) for f in cls._fields if f in columns}
    defaults = {k: v for k, v in OLD_DEFAULTS.get(name, {}).items() if k not in columns}
    keys = [k for k in cls.__key__ if k in columns]
    order = ", ".join(f'"{k}"' for k in keys) if keys else "rowid"

    docs = []
    for row in con.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
        values: dict[str, Any] = dict(defaults)
        for f, kind in kinds.items():
            try:
                values[f] = _value(row[f], kind)
            except (ValueError, TypeError) as exc:
                where = ", ".join(f"{k}={row[k]!r}" for k in keys)
                raise ImportRefused(f"{table} ({where}): can't read {f}={row[f]!r} ({exc})") from exc
        if cls._uses_counter():
            values["_id"] = values.pop("id", None)
        # Missing columns get the record's defaults, the same way records saved by older versions load.
        docs.append(cls._from_doc(values)._to_doc())
    item.count = len(docs)

    top = 0
    if cls._uses_counter():
        top = max((d["_id"] for d in docs), default=0)
        if "sqlite_sequence" in tables:  # AUTOINCREMENT tables remember ids of deleted rows too
            hit = con.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
            if hit and hit[0]:
                top = max(top, int(hit[0]))
    return item, docs, top


def read_sqlite(path: str | Path) -> list[tuple[Imported, list[dict], int]]:
    """Every collection's records from the SQLite file, converted (nothing is written anywhere)."""
    path = Path(path)
    if not path.is_file():
        raise ImportRefused(f"{path}: no such file")
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ImportRefused(f"{path} can't be opened: {exc}") from exc
    try:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "users" not in tables:
            raise ImportRefused(f"{path} isn't a TradeLab database (it has no users table)")
        return [_read(con, tables, cls) for cls in REGISTRY.values()]
    except sqlite3.Error as exc:
        raise ImportRefused(f"{path} can't be read: {exc}") from exc
    finally:
        con.close()


# ───────────────────────────────────────────── writing MongoDB

def _reason(exc: PyMongoError) -> str:
    if isinstance(exc, BulkWriteError) and exc.details.get("writeErrors"):
        return exc.details["writeErrors"][0].get("errmsg", str(exc))
    return str(exc)


def _remove(copied: list[tuple[str, list]]) -> None:
    db = database()
    for name, ids in copied:
        for i in range(0, len(ids), BATCH):
            db[name].delete_many({"_id": {"$in": ids[i:i + BATCH]}})


def _key_warning(read: list[tuple[Imported, list[dict], int]]) -> str | None:
    """Broker logins are copied encrypted: they only work with the secret key they were saved with."""
    blobs = [d["credentials"] for item, docs, _ in read if item.collection == "broker_accounts"
             for d in docs if d.get("credentials")]
    if not blobs:
        return None
    s = get_settings()
    if not s.secret_key and not (s.data_dir / "secret.key").exists():
        return ("There is no secret key here yet: copy secret.key from the old data folder (or set "
                "TRADELAB_SECRET_KEY as before) before starting TradeLab, or every broker account must be added again.")
    try:
        Fernet(s.fernet_key()).decrypt(blobs[0].encode())
    except InvalidToken:
        return ("The broker logins were saved with a different secret key: use the old secret.key (or "
                "TRADELAB_SECRET_KEY), or every broker account must be added again.")
    except Exception:  # noqa: BLE001 - only a hint; the import itself is done
        return None
    return None


def mongo_address() -> str:
    """The MongoDB address from the settings, without a password."""
    s = get_settings()
    url = re.sub(r"(//[^/@:]+):[^/@]*@", r"\1:***@", s.mongo_url)
    return f'database "{s.mongo_db}" at {url}'


def import_sqlite(path: str | Path, replace: bool = False) -> Report:
    """Copy the SQLite file at `path` into MongoDB (see the module docstring)."""
    read = read_sqlite(path)  # a file that can't be read stops here, before MongoDB is touched
    db = database()
    copied: list[tuple[str, list]] = []
    cleared = False
    step = f"connecting to MongoDB ({mongo_address()})"
    try:
        init_db()  # the unique rules apply while copying, as they did in SQLite
        held = {name: n for name in REGISTRY if (n := db[name].count_documents({}))}
        if held and not replace:
            listed = ", ".join(f"{name}: {n}" for name, n in held.items())
            raise ImportRefused(f"MongoDB ({mongo_address()}) already holds records ({listed}). Run again with "
                                "--replace to delete them and import the SQLite file instead")
        if replace:
            step = "deleting the records MongoDB held"
            clear_all()
            cleared = True
        for item, docs, _top in read:
            step = f"copying {item.collection}"
            copied.append((item.collection, [d["_id"] for d in docs]))
            for i in range(0, len(docs), BATCH):
                db[item.collection].insert_many(docs[i:i + BATCH], ordered=True)
        # New records get the next id after the highest one used ($max: an existing counter never goes back).
        step = "setting the id counters"
        for item, _docs, top in read:
            if top:
                db["counters"].update_one({"_id": item.collection}, {"$max": {"seq": top}}, upsert=True)
    except ImportRefused:
        raise
    except BaseException as exc:
        reason = _reason(exc) if isinstance(exc, PyMongoError) else f"{type(exc).__name__}: {exc}"
        try:
            _remove(copied)
        except PyMongoError:
            raise ImportRefused(f"Failed while {step}: {reason}. Some records may have stayed in MongoDB; "
                                "run again with --replace") from exc
        if not isinstance(exc, Exception):  # Ctrl+C: stopped, and what was copied is gone again
            raise
        before = " (the records it held before were deleted, as --replace asked)" if cleared else ""
        raise ImportRefused(f"Failed while {step}, so nothing was imported{before}: {reason}") from exc
    warnings = [w for w in [_key_warning(read)] if w]
    return Report([item for item, _docs, _top in read], warnings)


# ───────────────────────────────────────────── command line

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.import_sqlite",
        description="Copy an older TradeLab's SQLite database (tradelab.db) into MongoDB, once. Stop TradeLab first.")
    parser.add_argument("sqlite_file", help="the old tradelab.db (only read, never changed)")
    parser.add_argument("--replace", action="store_true",
                        help="delete the records MongoDB already holds before importing")
    args = parser.parse_args(argv)
    try:
        report = import_sqlite(args.sqlite_file, replace=args.replace)
    except ImportRefused as exc:
        print(f"Not imported: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {args.sqlite_file} into MongoDB ({mongo_address()}):")
    for item in report.collections:
        notes = []
        if item.table is None:
            notes.append("no such table in the SQLite file")
        elif item.table != item.collection:
            notes.append(f"read from {item.table}")
        if item.dropped:
            notes.append("left out old columns: " + ", ".join(item.dropped))
        print(f"  {item.collection:<20} {item.count:>7}" + (f"   ({'; '.join(notes)})" if notes else ""))
    print(f"  {report.total} records in all; new ids continue after the imported ones.")
    for warning in report.warnings:
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
