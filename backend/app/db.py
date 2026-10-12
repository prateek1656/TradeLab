"""MongoDB storage: stored documents, sessions and indexes.

Every stored record is a `@document` dataclass in `models.py`. Records keep the integer ids the
app and the browser use everywhere (from a counter per collection, never reused); records with a
natural key (one per user, one per day…) use that key as their MongoDB `_id`.

A `Session` works like a small unit of work (and like the SQLAlchemy sessions it replaced):

* `get` / `find` / `find_one` load records; the same record is the same Python object for the
  whole session, so changes made to it are kept.
* `add` and `delete` queue new and removed records; changed attributes of loaded records are
  noticed automatically.
* From its first write until `commit()`, a session keeps ONE MongoDB transaction open (when the
  server is a replica set — the shipped setup). Pending changes are written into it before every
  query (autoflush), so queries see them, but nothing is saved for others until `commit()`; a
  failed request or a `session_scope` block that raises saves nothing. If MongoDB aborts the
  transaction (another writer touched the same records, or it ran over MongoDB's time limit), the
  session writes what it had written again in a new one, retrying for up to 30 seconds.
* `update_many` / `update_one` / `delete_many` first commit the session, then act on the database
  straight away and return how many records matched — atomic per record, which is what
  "claim this run" checks rely on.

Unique rules (one confirmation per day, one list entry per contract…) are unique indexes; breaking
one raises `IntegrityError`, and nothing of the session's transaction is saved.

Without a replica set (a plain local MongoDB) there are no transactions: every write is saved as it
happens. Fine for trying TradeLab out, not for real money.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import threading
import time
import types
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, ClassVar, TypeVar

from pymongo import MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure, PyMongoError

from .config import get_settings

log = logging.getLogger(__name__)


class IntegrityError(Exception):
    """A unique rule was broken (MongoDB duplicate key). Nothing of that write was saved."""


class SessionFailed(Exception):
    """A save in this session failed, so its transaction was thrown away: call rollback() (or end the
    session) before using it again — like SQLAlchemy's PendingRollbackError."""


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
    _meta: ClassVar[tuple]

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
        attrs = self.__dict__
        attrs["_tl_saved"] = True  # exists in the database
        for name, stored, default, factory, kind in type(self)._meta:
            value = doc.get(stored, _MISSING)
            if value is _MISSING:  # saved by an older version: the field's default
                value = factory() if factory is not None else default
            elif kind is not None or isinstance(value, (list, dict)):
                value = _decode(value, kind)
            attrs[name] = value


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
        # Per field, for loading quickly: (attribute, stored name, default, default factory, kind).
        cls._meta = tuple(
            (f.name, "_id" if f.name == "id" and key == ("id",) else f.name,
             None if f.default is dataclasses.MISSING else f.default,
             None if f.default_factory is dataclasses.MISSING else f.default_factory,  # type: ignore[misc]
             cls._kinds[f.name])
            for f in dataclasses.fields(cls))
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


def next_ids(name: str, n: int) -> range:
    """`n` new integer ids for a collection with one counter write (for many records at once)."""
    doc = database()["counters"].find_one_and_update(
        {"_id": name}, {"$inc": {"seq": n}}, upsert=True, return_document=ReturnDocument.AFTER)
    return range(int(doc["seq"]) - n + 1, int(doc["seq"]) + 1)


def next_id(name: str) -> int:
    """The next integer id for a collection (never reused, even after deletes)."""
    doc = database()["counters"].find_one_and_update(
        {"_id": name}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return int(doc["seq"])


# ───────────────────────────────────────────── session

# How long a save keeps retrying when another writer holds the same records (SQLite waited 30 s too).
RETRY_SECONDS = 30.0
_DUPLICATE = 11000
_TRANSIENT_CODES = {112, 244, 251}  # WriteConflict, TransactionAborted, NoSuchTransaction (e.g. expired)


def _is_duplicate(exc: Exception) -> bool:
    if isinstance(exc, DuplicateKeyError):
        return True
    if isinstance(exc, BulkWriteError):
        return any(e.get("code") == _DUPLICATE for e in exc.details.get("writeErrors", []))
    return isinstance(exc, OperationFailure) and exc.code == _DUPLICATE


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, PyMongoError) and exc.has_error_label("TransientTransactionError"):
        return True
    if isinstance(exc, BulkWriteError):
        return any(e.get("code") in _TRANSIENT_CODES for e in exc.details.get("writeErrors", []))
    return isinstance(exc, OperationFailure) and exc.code in _TRANSIENT_CODES


class Session:
    """Loads and saves records for one request or one piece of work (see the module docstring)."""

    def __init__(self) -> None:
        self._db = database()
        self._identity: dict[tuple[type, Any], Document] = {}
        self._snap: dict[int, dict] = {}  # id(obj) -> stored form when loaded or last saved
        self._new: list[Document] = []
        self._deleted: list[Document] = []
        self._closed = False
        # The open transaction (from the first save until commit/rollback), what it has written
        # (replayed if MongoDB aborts it), and each touched record's state from before it began.
        self._txn = None
        self._txn_ops: list[tuple] = []
        self._txn_base: dict[int, tuple[Document, dict | None]] = {}
        self._failed = False

    def _check(self) -> None:
        if self._failed:
            raise SessionFailed("An earlier save in this session failed and was undone; call rollback() first")

    def _read(self, fn):  # noqa: ANN001, ANN202
        """Run a read in the session's transaction; if MongoDB aborted it meanwhile, write what the
        session had written again in a new one and read there."""
        self._check()
        try:
            return fn(self._txn)
        except PyMongoError as exc:
            if self._txn is None or not _is_transient(exc) or _is_duplicate(exc):
                raise
            try:
                self._restart_and_replay([], time.monotonic() + RETRY_SECONDS)
            except BaseException:
                self._abort()
                self._failed = True
                raise
            return fn(self._txn)

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
        doc = self._read(lambda s: self._db[cls.__collection__].find_one({"_id": _id}, session=s))
        return self._register(cls, doc) if doc else None

    def find(self, cls: type[D], filter: dict | None = None, sort: Any = None, limit: int = 0,
             skip: int = 0, track: bool = True) -> list[D]:
        """Matching records. `track=False` returns read-only copies the session doesn't follow
        (much faster for long lists that are only read, e.g. the whole order history)."""
        self.flush()
        flt, order = _encode_filter(cls, filter or {}), _sort_spec(cls, sort)
        docs = self._read(lambda s: list(self._db[cls.__collection__].find(flt, sort=order, limit=limit, skip=skip,
                                                                           session=s)))
        if not track:
            return [cls._from_doc(d) for d in docs]
        return [self._register(cls, d) for d in docs]

    def find_one(self, cls: type[D], filter: dict | None = None, sort: Any = None) -> D | None:
        rows = self.find(cls, filter, sort, limit=1)
        return rows[0] if rows else None

    def count(self, cls: type[Document], filter: dict | None = None) -> int:
        self.flush()
        flt = _encode_filter(cls, filter or {})
        return self._read(lambda s: self._db[cls.__collection__].count_documents(flt, session=s))

    def distinct(self, cls: type[Document], field: str, filter: dict | None = None) -> list:
        self.flush()
        name = "_id" if field == "id" and cls._uses_counter() else field
        flt = _encode_filter(cls, filter or {})
        values = self._read(lambda s: self._db[cls.__collection__].distinct(name, flt, session=s))
        return [_decode(v, cls._kinds.get(field)) for v in values]

    def aggregate(self, cls: type[Document], pipeline: list[dict]) -> list[dict]:
        """Raw MongoDB aggregation over the collection (results are plain dicts, stored form)."""
        self.flush()
        stages = _encode_filter(cls, pipeline)
        return self._read(lambda s: list(self._db[cls.__collection__].aggregate(stages, session=s)))

    # ── changing
    def add(self, obj: Document) -> None:
        self._check()
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
            # Loaded by another session: what's stored now is its baseline (so only real changes are
            # saved, and an undo restores real values). Gone meanwhile: it is saved as new.
            doc = self._read(lambda s: self._db[cls.__collection__].find_one({"_id": key[1]}, session=s))
            if doc is not None:
                self._snap[id(obj)] = cls._from_doc(doc)._to_doc()
                return
            obj.__dict__["_tl_saved"] = False
        self._new.append(obj)

    def add_all(self, objs) -> None:  # noqa: ANN001
        objs = list(objs)
        # New records of the same kind get their ids with one counter write (orders go out sooner).
        need: dict[type, list[Document]] = {}
        for o in objs:
            if type(o)._uses_counter() and getattr(o, "id", None) is None and o not in self._deleted:
                need.setdefault(type(o), []).append(o)
        for cls, group in need.items():
            for o, new_id in zip(group, next_ids(cls.__collection__, len(group))):
                o.id = new_id  # type: ignore[attr-defined]
        for o in objs:
            self.add(o)

    def delete(self, obj: Document) -> None:
        self._check()
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

    def _write(self, ops: list[tuple], session) -> None:  # noqa: ANN001
        deleted: dict[type, list] = {}
        batch: list[dict] = []  # consecutive new records of one collection, written together
        for i, (kind, cls, payload, _obj) in enumerate(ops):
            coll = self._db[cls.__collection__]
            if kind == "insert":
                batch.append(payload)
                nxt = ops[i + 1] if i + 1 < len(ops) else None
                if nxt is None or nxt[0] != "insert" or nxt[1] is not cls:
                    coll.insert_many(batch, ordered=True, session=session)
                    batch = []
            elif kind == "update":
                coll.update_one({"_id": payload["_id"]}, {"$set": payload["$set"]}, session=session)
            else:
                coll.delete_one({"_id": payload}, session=session)
                deleted.setdefault(cls, []).append(payload)
        for cls, keys in deleted.items():
            cls._on_delete(self._db, keys, session)

    # ── the session's transaction
    def _begin(self) -> None:
        self._txn = client().start_session()
        self._txn.start_transaction()

    def _end(self) -> None:
        if self._txn is not None:
            try:
                if self._txn.in_transaction:
                    self._txn.abort_transaction()
            except PyMongoError:
                pass  # MongoDB drops it anyway
            self._txn.end_session()
        self._txn = None

    def _restart_and_replay(self, extra: list[tuple], deadline: float) -> None:
        """MongoDB aborted the transaction (another writer, or it ran too long): write everything
        this session has saved so far again, in a new one."""
        attempt = 0
        while True:
            if time.monotonic() > deadline:
                raise OperationFailure("Couldn't save: the same records kept changing (gave up after "
                                       f"{RETRY_SECONDS:.0f} s)", code=112)
            self._end()
            time.sleep(min(0.5, 0.01 * 2 ** attempt))
            attempt += 1
            self._begin()
            try:
                self._write(self._txn_ops + extra, self._txn)
                return
            except PyMongoError as exc:
                if _is_duplicate(exc):
                    raise
                if not _is_transient(exc):
                    raise

    def _abort(self) -> None:
        """Throw the transaction away and put every record it touched back as it was before it."""
        self._end()
        for obj, before in self._txn_base.values():
            key = (type(obj), obj._key_value())
            if before is None:  # created in the transaction: it never existed
                if self._identity.get(key) is obj:
                    self._identity.pop(key)
                self._snap.pop(id(obj), None)
                obj.__dict__["_tl_saved"] = False
            else:  # changed or deleted in the transaction: back to what's stored
                self._identity[key] = obj
                obj._apply_doc(before)
                self._snap[id(obj)] = before
        self._txn_ops = []
        self._txn_base = {}

    def flush(self) -> None:
        """Write everything pending (inside the session's transaction; see the module docstring)."""
        self._check()
        ops = self._changes()
        if not ops:
            return
        try:
            if transactions_supported():
                if self._txn is None:
                    self._begin()
                deadline = time.monotonic() + RETRY_SECONDS
                try:
                    self._write(ops, self._txn)
                except PyMongoError as exc:
                    if _is_duplicate(exc) or not _is_transient(exc):
                        raise
                    self._restart_and_replay(ops, deadline)
                for _kind, _cls, _payload, obj in ops:
                    if id(obj) not in self._txn_base:
                        self._txn_base[id(obj)] = (obj, self._snap.get(id(obj)) if _kind != "insert" else None)
                self._txn_ops.extend(ops)
            else:
                self._write(ops, None)
        except BaseException as exc:
            if self._txn is not None:
                # Nothing of the transaction stays, like a failed SQL transaction, and the session
                # must be rolled back before it's used again (so nothing is saved half-way).
                self._abort()
                self._failed = True
            if isinstance(exc, PyMongoError) and _is_duplicate(exc):
                raise IntegrityError(str(exc)) from exc
            raise
        # Written: what's stored (in the transaction) is the new baseline.
        for kind, cls, payload, obj in ops:
            if kind == "delete":
                self._identity.pop((cls, payload), None)
                self._snap.pop(id(obj), None)
                obj.__dict__["_tl_saved"] = False  # added again later, it is saved as new
            else:
                self._snap[id(obj)] = obj._to_doc()
                obj.__dict__["_tl_saved"] = True
        self._new.clear()
        self._deleted.clear()

    def commit(self) -> None:
        """Save everything: pending changes are written and the transaction is committed."""
        self.flush()
        if self._txn is None:
            return
        deadline = time.monotonic() + RETRY_SECONDS
        try:
            while True:
                try:
                    self._txn.commit_transaction()
                    break
                except PyMongoError as exc:
                    if exc.has_error_label("UnknownTransactionCommitResult") and time.monotonic() < deadline:
                        continue  # safe to retry the commit itself
                    if _is_transient(exc) and not _is_duplicate(exc):
                        self._restart_and_replay([], deadline)
                        continue
                    raise
        except BaseException as exc:
            self._abort()
            self._failed = True
            if isinstance(exc, PyMongoError) and _is_duplicate(exc):
                raise IntegrityError(str(exc)) from exc
            raise
        self._end()
        self._txn_ops = []
        self._txn_base = {}

    def rollback(self) -> None:
        """Forget unsaved changes, including what was written since the last commit."""
        self._failed = False
        if self._txn is not None:
            self._abort()
        for obj in self._new:
            self._identity.pop((type(obj), obj._key_value()), None)
        self._new.clear()
        self._deleted.clear()
        for (cls, _id), obj in list(self._identity.items()):
            snap = self._snap.get(id(obj))
            if snap is not None and obj._to_doc() != snap:
                obj._apply_doc(snap)

    def close(self) -> None:
        """End the session; anything not committed is not saved."""
        self.rollback()
        self._identity.clear()
        self._snap.clear()
        self._closed = True

    def refresh(self, obj: Document) -> None:
        """Reload a record from the database (unsaved changes to it are dropped)."""
        cls = type(obj)
        doc = self._read(lambda s: self._db[cls.__collection__].find_one({"_id": obj._key_value()}, session=s))
        if doc is None:
            raise LookupError(f"{cls.__name__} {obj._key_value()!r} no longer exists")
        obj._apply_doc(doc)
        self._snap[id(obj)] = obj._to_doc()

    def expunge(self, obj: Document) -> None:
        """Stop tracking a record: later changes to the object aren't saved."""
        self._identity.pop((type(obj), obj._key_value()), None)
        self._snap.pop(id(obj), None)
        self._txn_base.pop(id(obj), None)
        if obj in self._new:
            self._new.remove(obj)
        if obj in self._deleted:
            self._deleted.remove(obj)

    # ── direct writes: they first save the session (commit), then act on the database straight
    #    away, atomic per record, and return how many records matched.
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
        self.commit()
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
        self.commit()
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
        self.commit()
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
        if cls._uses_counter():
            # Never hand out an id that is already used (e.g. after restoring an older backup).
            top = db[name].find_one({}, {"_id": 1}, sort=[("_id", -1)])
            if top is not None:
                db["counters"].update_one({"_id": name}, {"$max": {"seq": int(top["_id"])}}, upsert=True)


def clear_all() -> None:
    """Delete every record but keep the indexes (tests)."""
    from . import models  # noqa: F401

    db = database()
    for name in [*REGISTRY, "counters"]:
        db[name].delete_many({})
