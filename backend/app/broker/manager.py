"""Keeps one broker client per account and logs in again automatically when needed.

This is what makes "log in once" work: the encrypted credentials stored when an
account is added are used to start a fresh broker session every trading day
(and whenever Kotak reports that a session has expired).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from ..config import IST
from ..db import session_scope
from ..models import BrokerAccount, utcnow
from ..security import decrypt_json
from .base import Broker, BrokerError, LoginFailed, LoginInfo, SessionExpired

log = logging.getLogger(__name__)
T = TypeVar("T")

SESSION_MAX_AGE = timedelta(hours=10)
# After a failed login, requests that need a session fail straight away for this long instead of
# trying again (each try can wait up to 30 s for a fresh TOTP code, and repeated wrong MPIN or TOTP
# attempts can get the account locked). A forced login (warm-up, the Retry button) always tries.
LOGIN_RETRY_AFTER = timedelta(seconds=45)


def make_kotak(credentials: dict[str, str]):
    from .kotak import KotakNeoBroker

    return KotakNeoBroker(credentials)


class _Entry:
    def __init__(self, broker: Broker, fingerprint: str):
        self.broker = broker
        self.fingerprint = fingerprint
        self.logged_in_at: datetime | None = None
        self.lock = threading.Lock()
        # Goes up with every successful login, so threads that all saw the same expired session
        # log in once between them instead of once each.
        self.generation = 0
        self.failed_at: datetime | None = None
        self.failure: str | None = None

    def fresh(self) -> bool:
        if self.logged_in_at is None:
            return False
        now = datetime.now(IST)
        return self.logged_in_at.date() == now.date() and now - self.logged_in_at < SESSION_MAX_AGE


class BrokerManager:
    def __init__(self, kotak_factory: Callable[[dict[str, str]], Any] = make_kotak):
        self._entries: dict[int, _Entry] = {}
        self._lock = threading.Lock()
        self.kotak_factory = kotak_factory

    # -- construction -----------------------------------------------------------
    def _entry(self, account_id: int) -> _Entry:
        with session_scope() as db:
            acc = db.get(BrokerAccount, account_id)
            if acc is None:
                raise BrokerError("Account not found")
            if acc.broker != "kotak":
                raise BrokerError("Only Kotak Neo accounts can trade")
            blob = acc.credentials or ""
        fingerprint = hashlib.sha256(f"kotak:{blob}".encode()).hexdigest()
        with self._lock:
            entry = self._entries.get(account_id)
            if entry is None or entry.fingerprint != fingerprint:
                broker: Broker = self.kotak_factory(decrypt_json(blob))
                entry = _Entry(broker, fingerprint)
                self._entries[account_id] = entry
            return entry

    def forget(self, account_id: int) -> None:
        with self._lock:
            self._entries.pop(account_id, None)

    # -- sessions -------------------------------------------------------------------
    def _login(self, account_id: int, entry: _Entry) -> LoginInfo:
        try:
            info = entry.broker.login()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or exc.__class__.__name__
            # Whatever session there was can't be trusted any more (this login was needed for a reason).
            entry.logged_in_at = None
            entry.failed_at, entry.failure = datetime.now(IST), msg
            _mark(account_id, ok=False, error=msg)
            raise LoginFailed(msg) from exc
        entry.logged_in_at = datetime.now(IST)
        entry.generation += 1
        entry.failed_at = entry.failure = None
        _mark(account_id, ok=True, client_name=info.client_name)
        return info

    def ensure_session(self, account_id: int, force: bool = False, seen: int | None = None) -> Broker:
        """A logged-in broker for the account. `force` logs in again, unless another thread already did
        after generation `seen`. Raises LoginFailed if it can't log in."""
        return self._ensure(account_id, self._entry(account_id), force, seen)

    def _ensure(self, account_id: int, entry: _Entry, force: bool = False, seen: int | None = None) -> Broker:
        with entry.lock:
            if force and seen is not None and entry.generation != seen and entry.fresh():
                return entry.broker  # someone logged in again since this caller's session failed
            if force or not entry.fresh():
                if (not force and entry.failed_at is not None
                        and datetime.now(IST) - entry.failed_at < LOGIN_RETRY_AFTER):
                    raise LoginFailed(entry.failure or "Login failed")
                self._login(account_id, entry)
        return entry.broker

    def call(self, account_id: int, fn: Callable[[Broker], T]) -> T:
        """Run `fn` with a logged-in broker, logging in again once if the session expired.

        Raises LoginFailed when logging in fails (so `fn` never ran, or its retry never did)."""
        entry = self._entry(account_id)
        broker = self._ensure(account_id, entry)
        seen = entry.generation
        try:
            return fn(broker)
        except SessionExpired:
            log.info("Session expired for account %s, logging in again", account_id)
            broker = self._ensure(account_id, entry, force=True, seen=seen)
            return fn(broker)

    # -- market data helpers ----------------------------------------------------------
    def market_data_clients(self, user_id: int | None = None) -> list:
        """Kotak clients usable for quotes / scrip master (they need only the consumer key).

        The user's own accounts come first, healthy ones before ones with login errors.
        """
        with session_scope() as db:
            accounts = db.find(BrokerAccount, {"broker": "kotak"}, sort=[("status", 1), ("id", 1)])
            if user_id is not None:
                # Stable sort: the user's own accounts first, each group still by status, then id.
                accounts.sort(key=lambda a: a.user_id != user_id)
            ids = [a.id for a in accounts]
        clients = []
        for account_id in ids:
            try:
                clients.append(self._entry(account_id).broker)
            except Exception:  # noqa: BLE001 - skip accounts whose credentials can't be read
                continue
        return clients

    def market_data_client(self, user_id: int | None = None):
        clients = self.market_data_clients(user_id)
        return clients[0] if clients else None


def _mark(account_id: int, ok: bool, error: str | None = None, client_name: str | None = None) -> None:
    with session_scope() as db:
        acc = db.get(BrokerAccount, account_id)
        if acc is None:
            return
        if ok:
            acc.status = "connected"
            acc.last_error = None
            acc.last_login_at = utcnow()
            if client_name:
                acc.client_name = client_name
        else:
            acc.status = "error"
            acc.last_error = (error or "Login failed")[:500]


broker_manager = BrokerManager()
