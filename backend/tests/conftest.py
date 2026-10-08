import os
import tempfile
import time

os.environ["TRADELAB_DATA_DIR"] = tempfile.mkdtemp(prefix="tradelab-test-")
os.environ["TRADELAB_DISABLE_SCHEDULER"] = "1"
os.environ.pop("TRADELAB_ADMIN_EMAIL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.broker.base import BrokerError, Funds, LoginInfo, Order, OrderResult, Position, SessionExpired  # noqa: E402
from app.broker.manager import broker_manager  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.services import trading  # noqa: E402

TOTP_SECRET = "JBSWY3DPEHPK3PXP"


class FakeKotak:
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
        self.placed.append((time.time(), seg, ts, side, qty, product))
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
        return {tok: 100.0 for _, tok in instruments}

    def scrip_master_url(self, seg="nse_cm"):
        raise BrokerError("offline in tests")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    broker_manager._entries.clear()
    broker_manager.kotak_factory = FakeKotak
    auth_router._attempts.clear()
    monkeypatch.setattr(trading, "VERIFY_DELAY", 0.01)
    monkeypatch.setattr(trading, "CANCEL_DELAY", 0.01)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def signup(client, email, name="Trader", password="supersecret1"):
    return client.post("/api/auth/signup", json={"name": name, "email": email, "password": password})


@pytest.fixture
def admin(client):
    r = signup(client, "owner@example.com", "Owner")
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


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
