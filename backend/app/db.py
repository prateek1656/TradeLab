"""MongoDB storage: stored documents, sessions and indexes.

Every stored record is a `@document` dataclass in `models.py`. Records keep the integer ids the
app and the browser use everywhere (from a counter per collection, never reused); records with a
natural key (one per user, one per day…) use that key as their MongoDB `_id`.

A `Session` works like a small unit of work:

* `get` / `find` / `find_one` load records; the same record is the same Python object for the
  whole session, so changes made to it are kept.
* `add` and `delete` queue new and removed records; changed attributes of loaded records are
  noticed automatically.
* `flush` / `commit` write everything queued in one go — in one MongoDB transaction when the
  server is a replica set (the shipped setup), so either all of it is saved or none of it.
  Pending changes are written before every query (like SQLAlchemy's autoflush), so a query
  always sees them.
* `update_many` / `update_one` / `delete_many` act on the database straight away and return how
  many records matched — atomic per record, which is what "claim this run" checks rely on.

Unique rules (one confirmation per day, one list entry per contract…) are unique indexes; breaking
one raises `IntegrityError`, and nothing of that write is saved.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import threading
import types
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, ClassVar, TypeVar

from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure

from .config import get_settings

log = logging.getLogger(__name__)


class IntegrityError(Exception):
    """A unique rule was broken (MongoDB duplicate key). Nothing of that write was saved."""


# ───────────────────────────────────────────── connection

_lock = threading.Lock()
_client: MongoClient | None = None
_transactions: bool | None = None


def client() -> MongoClient:
    global _client
    with _lock:
        if _client is None:
            s = get_settings()
            _client = MongoClient(s.mongo_url, tz_aware=True, tzinfo=timezone.utc,
                                  serverSelectionTimeoutMS=s.mongo_timeout_ms)
        return _client


def database() -> Database:
    return client()[get_settings().mongo_db]


def transactions_supported() -> bool:
    """Multi-record transactions need a replica set (the docker-compose setup runs one)."""
    global _transactions
    if _transactions is None:
        setting = get_settings().mongo_transactions
        if setting is not None:
            _transactions = setting
        else:
            try:
                hello = client().admin.command("hello")
                _transactions = bool(hello.get("setName") or hello.get("msg") == "isdbgrid")
            except Exception:  # noqa: BLE001 - decided again on the next write
                return False
            if not _transactions:
                log.warning("MongoDB isn't a replica set: each record is saved on its own, without transactions")
    return _transactions


# ───────────────────────────────────────────── documents

D = TypeVar("D", bound="Document")
REGISTRY: dict[str, type[Document]] = {}
_MISSING = object()


class Document:
    """Base class of stored records. Subclasses are made with `@document(...)`."""

    __collection__: ClassVar[str]
    __key__: ClassVar[tuple[str, ...]]
    __indexes__: ClassVar[list[dict]]
    _fields: ClassVar[tuple[str, ...]]
    _kinds: ClassVar[dict[str, str | None]]

    # Called inside the same write as the record's removal (e.g. what points at a deleted account).
    @classmethod
    def _on_delete(cls, db: Database, keys: list[Any], session) -> None:  # noqa: ANN001
        return None

    # ── keys
    @classmethod
    def _uses_counter(cls) -> bool:
        return cls.__key__ == ("id",)

    @classmethod
    def _id_for(cls, key: Any) -> Any:
        """MongoDB `_id` for a key value (an int id, a date, or a tuple for a compound key)."""
        if cls._uses_counter():
            return key
        values = key if isinstance(key, tuple) else (key,)
        if len(values) != len(cls.__key__):
            raise TypeError(f"{cls.__name__} key is {cls.__key__}, got {key!r}")
        encoded = [_encode(v) for v in values]
        return encoded[0] if len(encoded) == 1 else "|".join(str(v) for v in encoded)

    def _key_value(self) -> Any:
        cls = type(self)
        if cls._uses_counter():
            return self.id  # type: ignore[attr-defined]
        return cls._id_for(tuple(getattr(self, k) for k in cls.__key__))

    # ── conversion
    def _to_doc(self) -> dict:
        doc = {"_id": self._key_value()}
        for name in type(self)._fields:
            if name == "id" and type(self)._uses_counter():
                continue
            doc[name] = _encode(getattr(self, name))
        return doc

    @classmethod
    def _from_doc(cls: type[D], doc: dict) -> D:
        obj = object.__new__(cls)
        obj._apply_doc(doc)
        return obj

    def _apply_doc(self, doc: dict) -> None:
        cls = type(self)
        object.__setattr__(self, "_tl_saved", True)  # exists in the database
        for f in dataclasses.fields(cls):
            if f.name == "id" and cls._uses_counter():
                value = doc.get("_id")
            else:
                value = doc.get(f.name, _MISSING)
                if value is _MISSING:  # saved by an older version: the field's default
                    if f.default is not dataclasses.MISSING:
                        value = f.default
                    elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                        value = f.default_factory()  # type: ignore[misc]
                    else:
                        value = None
            object.__setattr__(self, f.name, _decode(value, cls._kinds.get(f.name)))


def _kind(hint: Any) -> str | None:
    """'datetime', 'date' or None, for a field's type hint (Optional[...] included)."""
    options = typing.get_args(hint) if typing.get_origin(hint) in (typing.Union, types.UnionType) else (hint,)
    for opt in options:
        if opt is datetime:
            return "datetime"
        if opt is date:
            return "date"
    return None


def document(collection: str, key: tuple[str, ...] = ("id",), indexes: list[dict] | None = None):
    """Make a class a stored record: a keyword-only dataclass saved in `collection`.

    `key`: ("id",) for an integer id from the collection's counter, else the natural key fields.
    `indexes`: index specs — {"keys": [("field", 1), ...], "unique": bool, "partialFilterExpression": {...}}.
    """
    def wrap(cls):
        cls = dataclasses.dataclass(eq=False, kw_only=True)(cls)
        cls.__collection__ = collection
        cls.__key__ = key
        cls.__indexes__ = list(indexes or [])
        hints = typing.get_type_hints(cls)
        cls._fields = tuple(f.name for f in dataclasses.fields(cls))
        cls._kinds = {name: _kind(hints[name]) for name in cls._fields}
        REGISTRY[collection] = cls
        return cls
    return wrap


def _encode(value: Any) -> Any:
    """Python value -> what is stored: dates as 'YYYY-MM-DD' (sortable), datetimes in UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _decode(value: Any, kind: str | None) -> Any:
    if value is None or kind is None:
        return copy.deepcopy(value) if isinstance(value, (list, dict)) else value
    if kind == "date":
        if isinstance(value, datetime):
            return value.date()
        return date.fromisoformat(value) if isinstance(value, str) else value
    if kind == "datetime" and isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _encode_filter(cls: type[Document], flt: Any) -> Any:
    """A query filter in the app's terms -> MongoDB's: `id` means `_id`, dates become strings."""
    if isinstance(flt, dict):
        out = {}
        for k, v in flt.items():
            name = "_id" if k == "id" and cls._uses_counter() else k
            out[name] = _encode_filter(cls, v)
        return out
    if isinstance(flt, (list, tuple)):
        return [_encode_filter(cls, v) for v in flt]
    return _encode(flt)


def _sort_spec(cls: type[Document], sort: Any) -> list[tuple[str, int]] | None:
    if not sort:
        return None
    if isinstance(sort, tuple) and len(sort) == 2 and isinstance(sort[1], int):
        sort = [sort]
    if isinstance(sort, str):
        sort = [(sort, 1)]
    return [("_id" if k == "id" and cls._uses_counter() else k, d) for k, d in sort]


def collection(cls: type[Document]) -> Collection:
    return database()[cls.__collection__]


def next_id(name: str) -> int:
    """The next integer id for a collection (never reused, even after deletes)."""
    doc = database()["counters"].find_one_and_update(
        {"_id": name}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return int(doc["seq"])


# ───────────────────────────────────────────── session

class Session:
    """Loads and saves records for one request or one piece of work (see the module docstring)."""

    def __init__(self) -> None:
        self._db = database()
        self._identity: dict[tuple[type, Any], Document] = {}
        self._snap: dict[int, dict] = {}  # id(obj) -> stored form when loaded or last saved
        self._new: list[Document] = []
        self._deleted: list[Document] = []
        self._closed = False

    # ── reading
    def _register(self, cls: type[D], doc: dict) -> D:
        key = (cls, doc["_id"])
        obj = self._identity.get(key)
        if obj is not None:
            return obj  # type: ignore[return-value]  # the session's copy, with its unsaved changes
        obj = cls._from_doc(doc)
        self._identity[key] = obj
        self._snap[id(obj)] = obj._to_doc()
        return obj

    def get(self, cls: type[D], key: Any) -> D | None:
        """The record with this key (int id, date, or tuple for a compound key), or None."""
        if key is None:
            return None
        _id = cls._id_for(key)
        obj = self._identity.get((cls, _id))
        if obj is not None:
            return None if obj in self._deleted else obj  # type: ignore[return-value]
        self.flush()
        doc = self._db[cls.__collection__].find_one({"_id": _id})
        return self._register(cls, doc) if doc else None

    def find(self, cls: type[D], filter: dict | None = None, sort: Any = None, limit: int = 0,
             skip: int = 0) -> list[D]:
        self.flush()
        cur = self._db[cls.__collection__].find(_encode_filter(cls, filter or {}), sort=_sort_spec(cls, sort),
                                                limit=limit, skip=skip)
        return [self._register(cls, d) for d in cur]

    def find_one(self, cls: type[D], filter: dict | None = None, sort: Any = None) -> D | None:
        rows = self.find(cls, filter, sort, limit=1)
        return rows[0] if rows else None

    def count(self, cls: type[Document], filter: dict | None = None) -> int:
        self.flush()
        return self._db[cls.__collection__].count_documents(_encode_filter(cls, filter or {}))

    def distinct(self, cls: type[Document], field: str, filter: dict | None = None) -> list:
        self.flush()
        name = "_id" if field == "id" and cls._uses_counter() else field
        values = self._db[cls.__collection__].distinct(name, _encode_filter(cls, filter or {}))
        return [_decode(v, cls._kinds.get(field)) for v in values]

    def aggregate(self, cls: type[Document], pipeline: list[dict]) -> list[dict]:
        """Raw MongoDB aggregation over the collection (results are plain dicts, stored form)."""
        self.flush()
        return list(self._db[cls.__collection__].aggregate(_encode_filter(cls, pipeline)))

    # ── changing
    def add(self, obj: Document) -> None:
        if obj in self._deleted:
            self._deleted.remove(obj)
            return
        cls = type(obj)
        if cls._uses_counter() and getattr(obj, "id", None) is None:
            obj.id = next_id(cls.__collection__)  # type: ignore[attr-defined]
        key = (cls, obj._key_value())
        if self._identity.get(key) is obj:
            return
        self._identity[key] = obj
        if getattr(obj, "_tl_saved", False):
            # Loaded by another session: save all of its fields over the stored record.
            self._snap[id(obj)] = {"_id": obj._key_value()}
            return
        self._new.append(obj)

    def add_all(self, objs) -> None:  # noqa: ANN001
        for o in objs:
            self.add(o)

    def delete(self, obj: Document) -> None:
        if obj in self._new:
            self._new.remove(obj)
            self._identity.pop((type(obj), obj._key_value()), None)
            return
        if obj not in self._deleted:
            self._deleted.append(obj)

    def _changes(self) -> list[tuple]:
        ops: list[tuple] = []
        for obj in self._new:
            ops.append(("insert", type(obj), obj._to_doc(), obj))
        for (cls, _id), obj in list(self._identity.items()):
            if obj in self._new or obj in self._deleted or id(obj) not in self._snap:
                continue
            now, before = obj._to_doc(), self._snap[id(obj)]
            if now["_id"] != before["_id"]:
                raise ValueError(f"{cls.__name__}: a record's key can't be changed")
            changed = {k: v for k, v in now.items() if k != "_id" and before.get(k, _MISSING) != v}
            if changed:
                ops.append(("update", cls, {"_id": _id, "$set": changed}, obj))
        for obj in self._deleted:
            ops.append(("delete", type(obj), obj._key_value(), obj))
        return ops

    def flush(self) -> None:
        """Write everything pending, all of it or none of it (see the module docstring)."""
        ops = self._changes()
        if not ops:
            return

        def write(session=None) -> None:  # noqa: ANN001
            deleted: dict[type, list] = {}
            for kind, cls, payload, _obj in ops:
                coll = self._db[cls.__collection__]
                if kind == "insert":
                    coll.insert_one(payload, session=session)
                elif kind == "update":
                    coll.update_one({"_id": payload["_id"]}, {"$set": payload["$set"]}, session=session)
                else:
                    coll.delete_one({"_id": payload}, session=session)
                    deleted.setdefault(cls, []).append(payload)
            for cls, keys in deleted.items():
                cls._on_delete(self._db, keys, session)

        try:
            if transactions_supported():
                with client().start_session() as s:
                    s.with_transaction(lambda sess: write(sess))
            else:
                write()
        except (DuplicateKeyError, BulkWriteError) as exc:
            raise IntegrityError(str(exc)) from exc
        except OperationFailure as exc:
            if exc.code == 11000:
                raise IntegrityError(str(exc)) from exc
            raise
        # Saved: what's stored now is the new baseline.
        for kind, cls, payload, obj in ops:
            if kind == "delete":
                self._identity.pop((cls, payload), None)
                self._snap.pop(id(obj), None)
            else:
                self._snap[id(obj)] = obj._to_doc()
                object.__setattr__(obj, "_tl_saved", True)
        self._new.clear()
        self._deleted.clear()

    def commit(self) -> None:
        self.flush()

    def rollback(self) -> None:
        """Forget unsaved changes: new records are dropped, changed ones go back to what's stored."""
        for obj in self._new:
            self._identity.pop((type(obj), obj._key_value()), None)
        self._new.clear()
        self._deleted.clear()
        for (cls, _id), obj in list(self._identity.items()):
            snap = self._snap.get(id(obj))
            if snap is not None and obj._to_doc() != snap:
                obj._apply_doc(snap)

    def close(self) -> None:
        self.rollback()
        self._identity.clear()
        self._snap.clear()
        self._closed = True

    def refresh(self, obj: Document) -> None:
        """Reload a record from the database (unsaved changes to it are dropped)."""
        cls = type(obj)
        doc = self._db[cls.__collection__].find_one({"_id": obj._key_value()})
        if doc is None:
            raise LookupError(f"{cls.__name__} {obj._key_value()!r} no longer exists")
        obj._apply_doc(doc)
        self._snap[id(obj)] = obj._to_doc()

    def expunge(self, obj: Document) -> None:
        """Stop tracking a record: later changes to the object aren't saved."""
        self._identity.pop((type(obj), obj._key_value()), None)
        self._snap.pop(id(obj), None)
        if obj in self._new:
            self._new.remove(obj)
        if obj in self._deleted:
            self._deleted.remove(obj)

    # ── direct writes (not queued; atomic per record; return how many matched)
    def _resync(self, cls: type[Document], ids: list) -> None:
        for _id in ids:
            obj = self._identity.get((cls, _id))
            if obj is None or obj in self._new:
                continue
            doc = self._db[cls.__collection__].find_one({"_id": _id})
            if doc is None:
                self.expunge(obj)
            else:
                obj._apply_doc(doc)
                self._snap[id(obj)] = obj._to_doc()

    def update_many(self, cls: type[Document], filter: dict, set: dict | None = None, *,  # noqa: A002
                    inc: dict | None = None) -> int:
        """`$set` (and `$inc`) on every matching record right now; returns how many matched.

        Records this session has loaded are updated to match afterwards.
        """
        self.flush()
        flt = _encode_filter(cls, filter)
        coll = self._db[cls.__collection__]
        ids = [d["_id"] for d in coll.find(flt, {"_id": 1})]
        if not ids:
            return 0
        update: dict = {}
        if set:
            update["$set"] = _encode_filter(cls, set)
        if inc:
            update["$inc"] = inc
        res = coll.update_many({"$and": [flt, {"_id": {"$in": ids}}]}, update)
        self._resync(cls, ids)
        return res.matched_count

    def update_one(self, cls: type[Document], filter: dict, set: dict | None = None, *,  # noqa: A002
                   inc: dict | None = None, upsert: bool = False, set_on_insert: dict | None = None) -> int:
        """Like `update_many` for at most one record; with `upsert`, creates it if none matches."""
        self.flush()
        update: dict = {}
        if set:
            update["$set"] = _encode_filter(cls, set)
        if inc:
            update["$inc"] = inc
        if set_on_insert:
            update["$setOnInsert"] = _encode_filter(cls, set_on_insert)
        flt = _encode_filter(cls, filter)
        coll = self._db[cls.__collection__]
        found = coll.find_one(flt, {"_id": 1})
        try:
            if found is not None:
                # Only that record, and only if it still matches (another writer may have got there first).
                res = coll.update_one({"$and": [flt, {"_id": found["_id"]}]}, update)
                self._resync(cls, [found["_id"]])
                return res.matched_count
            if not upsert:
                return 0
            res = coll.update_one(flt, update, upsert=True)
        except DuplicateKeyError as exc:
            raise IntegrityError(str(exc)) from exc
        _id = res.upserted_id
        if _id is None:  # created by someone else in the meantime
            hit = coll.find_one(flt, {"_id": 1})
            _id = hit["_id"] if hit else None
        if _id is not None:
            self._resync(cls, [_id])
        return 1 if (res.upserted_id is not None or res.matched_count) else 0

    def delete_many(self, cls: type[Document], filter: dict) -> int:
        """Delete every matching record right now (with what depends on them); returns how many."""
        self.flush()
        flt = _encode_filter(cls, filter)
        coll = self._db[cls.__collection__]
        ids = [d["_id"] for d in coll.find(flt, {"_id": 1})]
        if not ids:
            return 0

        def write(session=None) -> int:  # noqa: ANN001
            n = coll.delete_many({"$and": [flt, {"_id": {"$in": ids}}]}, session=session).deleted_count
            cls._on_delete(self._db, ids, session)
            return n

        if transactions_supported():
            with client().start_session() as s:
                n = s.with_transaction(lambda sess: write(sess))
        else:
            n = write()
        for _id in ids:
            obj = self._identity.get((cls, _id))
            if obj is not None:
                self.expunge(obj)
        return n


def get_db() -> Iterator[Session]:
    """FastAPI dependency: a session for the request (routers call `db.commit()` to save)."""
    db = Session()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that saves everything at the end, or nothing if the block raises."""
    db = Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ───────────────────────────────────────────── setup

def init_db() -> None:
    """Create the indexes every collection needs (safe to run at every start)."""
    from . import models  # noqa: F401  (register documents)

    db = database()
    for name, cls in REGISTRY.items():
        for spec in cls.__indexes__:
            opts = {k: v for k, v in spec.items() if k != "keys"}
            db[name].create_index(spec["keys"], **opts)


def clear_all() -> None:
    """Delete every record but keep the indexes (tests)."""
    from . import models  # noqa: F401

    db = database()
    for name in [*REGISTRY, "counters"]:
        db[name].delete_many({})
