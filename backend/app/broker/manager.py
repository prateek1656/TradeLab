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

from sqlalchemy import select

from ..config import IST
from ..db import session_scope
from ..models import BrokerAccount, utcnow
from ..security import decrypt_json
from .base import Broker, BrokerError, LoginInfo, SessionExpired
from .paper import PaperBroker

log = logging.getLogger(__name__)
T = TypeVar("T")

SESSION_MAX_AGE = timedelta(hours=10)


def make_kotak(credentials: dict[str, str]):
    from .kotak import KotakNeoBroker

    return KotakNeoBroker(credentials)


class _Entry:
    def __init__(self, broker: Broker, fingerprint: str):
        self.broker = broker
        self.fingerprint = fingerprint
        self.logged_in_at: datetime | None = None
        self.lock = threading.Lock()

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
            broker_kind, blob = acc.broker, acc.credentials or ""
        fingerprint = hashlib.sha256(f"{broker_kind}:{blob}".encode()).hexdigest()
        with self._lock:
            entry = self._entries.get(account_id)
            if entry is None or entry.fingerprint != fingerprint:
                if broker_kind == "paper":
                    broker: Broker = PaperBroker(account_id, price_lookup=self.reference_price)
                else:
                    broker = self.kotak_factory(decrypt_json(blob))
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
            _mark(account_id, ok=False, error=msg)
            raise BrokerError(msg) from exc
        entry.logged_in_at = datetime.now(IST)
        _mark(account_id, ok=True, client_name=info.client_name)
        return info

    def ensure_session(self, account_id: int, force: bool = False) -> Broker:
        entry = self._entry(account_id)
        with entry.lock:
            if force or not entry.fresh():
                self._login(account_id, entry)
        return entry.broker

    def call(self, account_id: int, fn: Callable[[Broker], T]) -> T:
        """Run `fn` with a logged-in broker, logging in again once if the session expired."""
        broker = self.ensure_session(account_id)
        try:
            return fn(broker)
        except SessionExpired:
            log.info("Session expired for account %s, logging in again", account_id)
            broker = self.ensure_session(account_id, force=True)
            return fn(broker)

    # -- market data helpers ----------------------------------------------------------
    def market_data_clients(self, user_id: int | None = None) -> list:
        """Kotak clients usable for quotes / scrip master (they need only the consumer key).

        The user's own accounts come first, healthy ones before ones with login errors.
        """
        with session_scope() as db:
            q = select(BrokerAccount).where(BrokerAccount.broker == "kotak")
            if user_id is not None:
                q = q.order_by((BrokerAccount.user_id == user_id).desc())
            accounts = db.scalars(q.order_by(BrokerAccount.status.asc(), BrokerAccount.id)).all()
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

    def reference_price(self, exchange_segment: str, trading_symbol: str, token: str | None) -> float | None:
        """Live LTP for paper fills when any Kotak account is available."""
        from ..services.scrips import scrip_master

        token = token or scrip_master.token_for(trading_symbol)
        client = self.market_data_client()
        if not token or client is None:
            return None
        return client.ltp([(exchange_segment, token)]).get(token)


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
