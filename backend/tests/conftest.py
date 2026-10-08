import os
import tempfile
from datetime import datetime, timedelta

os.environ["TRADELAB_DATA_DIR"] = tempfile.mkdtemp(prefix="tradelab-test-")
os.environ["TRADELAB_DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.broker.base import BrokerError, Funds, LoginInfo, Order, OrderResult, Position, SessionExpired  # noqa: E402
from app.broker.manager import broker_manager  # noqa: E402
from app.config import IST  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.services import trading  # noqa: E402

TOTP_SECRET = "JBSWY3DPEHPK3PXP"


class FakeKotak:
    prices: dict[str, float] = {}  # token -> LTP (anything else is ₹100)
    quote_time: float | None = None  # last-update epoch for every quote (None = not reported)

    def __init__(self, creds):
        self.creds = creds
        self.funds_val = 25000.0
        self.positions_map: dict[tuple[str, str], Position] = {}
        self.order_book: list[Order] = []
        self.placed: list[tuple[float, str, str, str, int, str]] = []
        self.cancelled: list[str] = []
        self.logins = 0
        self.expire_next_order = False

    def login(self):
        if self.creds["mpin"] == "000000":
            raise BrokerError("MPIN validation failed: Invalid MPIN")
        self.logins += 1
        return LoginInfo(client_name="Test Client")

    def funds(self):
        return Funds(available=self.funds_val)

    def positions(self):
        return list(self.positions_map.values())

    def orders(self):
        return list(self.order_book)

    def place_market_order(self, seg, ts, side, qty, product="MIS"):
        if self.expire_next_order:
            self.expire_next_order = False
            raise SessionExpired("Invalid session, please re-login")
        self.placed.append((trading.now_ist().timestamp(), seg, ts, side, qty, product))
        key = (ts, product)
        pos = self.positions_map.get(key) or Position(seg, ts, product, 0)
        pos.net_qty += qty if side == "B" else -qty
        self.positions_map[key] = pos
        return OrderResult(ok=True, order_id=f"ORD{len(self.placed)}", message="Order placed")

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        for o in self.order_book:
            if o.order_id == order_id:
                o.status = "cancelled"
        return OrderResult(ok=True, order_id=order_id, message="Cancelled")

    def ltp(self, instruments):
        return {tok: FakeKotak.prices.get(tok, 100.0) for _, tok in instruments}

    def quote_times(self, instruments):
        if FakeKotak.quote_time is None:
            return {}
        return {tok: FakeKotak.quote_time for _, tok in instruments}

    def scrip_master_url(self, seg="nse_cm"):
        raise BrokerError("offline in tests")


# Tests run on a fixed Wednesday morning unless they move the clock.
MARKET_MORNING = datetime(2026, 10, 14, 9, 0, tzinfo=IST)


class Clock:
    """Real time passing, shifted to a chosen IST moment."""

    def __init__(self):
        self.offset = timedelta(0)

    def set(self, when: datetime):
        self.offset = when - datetime.now(IST)

    def now(self) -> datetime:
        return datetime.now(IST) + self.offset


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    c.set(MARKET_MORNING)
    monkeypatch.setattr(trading, "now_ist", c.now)
    return c


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    broker_manager._entries.clear()
    broker_manager.kotak_factory = FakeKotak
    FakeKotak.prices = {}
    FakeKotak.quote_time = None
    auth_router._attempts.clear()
    monkeypatch.setattr(trading, "VERIFY_DELAY", 0.01)
    monkeypatch.setattr(trading, "CANCEL_DELAY", 0.01)
    monkeypatch.setattr(trading, "PRICE_RETRY_DELAY", 0.01)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def signup(client, email, name="Trader", password="supersecret1"):
    return client.post("/api/auth/signup", json={"name": name, "email": email, "password": password})


@pytest.fixture
def owner(client):
    r = signup(client, "owner@example.com", "Owner")
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def custom_sizing(client, owner):
    """Use each stock's own size instead of the default equal split."""
    r = client.put("/api/automation/settings", json={"sizing_mode": "custom"}, headers=owner)
    assert r.status_code == 200, r.text


def kotak_payload(**over):
    body = {
        "name": "Main account",
        "broker": "kotak",
        "consumer_key": "ck-123",
        "mobile": "9876543210",
        "ucc": "abc12",
        "mpin": "123456",
        "totp_secret": TOTP_SECRET,
    }
    body.update(over)
    return body


def fake_for(account_id: int) -> FakeKotak:
    return broker_manager._entries[account_id].broker


def confirm(client, headers):
    """Press "Let's Trade" and answer "Yes" for the day the app offers."""
    st = client.get("/api/automation/status", headers=headers).json()["lets_trade"]
    r = client.post("/api/automation/lets-trade", json={"trade_date": st["trade_date"]}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def buy(client, headers, **kw):
    """Confirm with Let's Trade, then run today's entry straight away."""
    confirm(client, headers)
    user = client.get("/api/auth/me", headers=headers).json()["id"]
    return trading.run_entry(user, "manual", **kw)
