"""The admin page: a secret address, its own login, and who may use TradeLab."""

import pytest
from conftest import MARKET_MORNING, confirm, kotak_payload, signup

from app.config import get_settings
from app.db import session_scope
from app.models import TradeSession
from app.security import hash_admin_password

PATH = "s3cret-admin-path-0123456789abcdef"
NAME = "admin-test"
PASSWORD = "Very-Strong.Admin#Pass+2026"


@pytest.fixture
def admin(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "admin_username", NAME)
    monkeypatch.setattr(s, "admin_password_hash", hash_admin_password(PASSWORD))
    monkeypatch.setattr(s, "admin_path", PATH)
    return s


def _login(client, password=PASSWORD, name=NAME):
    return client.post(f"/api/admin/{PATH}/login", json={"username": name, "password": password})


def _admin_headers(client):
    r = _login(client)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_without_an_admin_there_is_no_admin_page_and_sign_ups_work_as_before(client):
    assert client.get(f"/api/admin/{PATH}/check").status_code == 404
    r = signup(client, "a@example.com")
    assert r.status_code == 201 and r.json()["token"]


def test_a_wrong_address_looks_exactly_like_any_unknown_address(client, admin):
    unknown = client.get("/api/no-such-thing")
    for path in ("wrong", PATH[:-1], PATH + "x", "admin"):
        r = client.get(f"/api/admin/{path}/check")
        assert r.status_code == 404 and r.json() == unknown.json()
        assert client.post(f"/api/admin/{path}/login", json={"username": NAME, "password": PASSWORD}).status_code == 404
    assert client.get(f"/api/admin/{PATH}/check").status_code == 200


def test_admin_login_needs_the_exact_username_and_password(client, admin):
    assert _login(client, password="wrong").status_code == 401
    assert _login(client, name="admin").status_code == 401
    assert _login(client).status_code == 200


def test_admin_routes_need_an_admin_session_and_a_users_token_is_not_one(client, owner, admin):
    assert client.get(f"/api/admin/{PATH}/users").status_code == 401
    assert client.get(f"/api/admin/{PATH}/users", headers=owner).status_code == 401
    assert client.get(f"/api/admin/{PATH}/users", headers=_admin_headers(client)).status_code == 200
    # and an admin session isn't a user's
    assert client.get("/api/auth/me", headers=_admin_headers(client)).status_code == 401


def test_changing_the_admin_password_ends_admin_sessions(client, admin, monkeypatch):
    h = _admin_headers(client)
    monkeypatch.setattr(admin, "admin_password_hash", hash_admin_password("Another-Strong-Pass-123"))
    assert client.get(f"/api/admin/{PATH}/users", headers=h).status_code == 401


def test_a_new_sign_up_waits_for_approval(client, admin):
    r = signup(client, "new@example.com", "Newcomer")
    assert r.status_code == 202 and r.json()["pending"] and "token" not in r.json()
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": "supersecret1"})
    assert r.status_code == 403 and "waiting for the administrator" in r.json()["detail"]
    h = _admin_headers(client)
    (u,) = [u for u in client.get(f"/api/admin/{PATH}/users", headers=h).json() if u["email"] == "new@example.com"]
    assert u["status"] == "pending"
    assert client.post(f"/api/admin/{PATH}/users/{u['id']}/status", json={"status": "active"}, headers=h).status_code == 200
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": "supersecret1"})
    assert r.status_code == 200 and r.json()["token"]


def test_a_sign_up_request_can_be_turned_down(client, admin):
    signup(client, "stranger@example.com")
    h = _admin_headers(client)
    (u,) = client.get(f"/api/admin/{PATH}/users", headers=h).json()
    assert client.delete(f"/api/admin/{PATH}/users/{u['id']}", headers=h).status_code == 200
    assert client.get(f"/api/admin/{PATH}/users", headers=h).json() == []


def test_blocking_signs_out_at_once_cancels_new_buys_and_keeps_square_off(client, owner, clock, monkeypatch):
    clock.set(MARKET_MORNING)
    client.post("/api/accounts", json=kotak_payload(), headers=owner)
    client.post("/api/basket", json={"symbol": "AAA", "trading_symbol": "AAA-EQ", "token": "1",
                                     "sizing_mode": "qty", "sizing_value": 1}, headers=owner)
    confirm(client, owner)
    s = get_settings()  # the owner existed before the admin was set up: still active
    monkeypatch.setattr(s, "admin_username", NAME)
    monkeypatch.setattr(s, "admin_password_hash", hash_admin_password(PASSWORD))
    monkeypatch.setattr(s, "admin_path", PATH)
    assert client.get("/api/auth/me", headers=owner).status_code == 200
    h = _admin_headers(client)
    (u,) = client.get(f"/api/admin/{PATH}/users", headers=h).json()
    r = client.post(f"/api/admin/{PATH}/users/{u['id']}/status", json={"status": "blocked"}, headers=h)
    assert r.json()["cancelled_confirmations"] == 1
    assert client.get("/api/auth/me", headers=owner).status_code == 401  # signed out on every device
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "supersecret1"})
    assert r.status_code == 403 and "removed" in r.json()["detail"]
    with session_scope() as db:
        assert db.find_one(TradeSession, {"user_id": u["id"]}).status == "cancelled"
    with session_scope() as db:  # the square-off setting is untouched: what's open still closes
        from app.services.trading import get_section
        assert get_section(db, u["id"], "intraday").auto_exit
    client.post(f"/api/admin/{PATH}/users/{u['id']}/status", json={"status": "active"}, headers=h)
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "supersecret1"}).status_code == 200


def test_sign_ups_can_be_closed(client, admin):
    h = _admin_headers(client)
    assert client.put(f"/api/admin/{PATH}/settings", json={"signups_open": False}, headers=h).json() == {"signups_open": False}
    r = signup(client, "late@example.com")
    assert r.status_code == 403 and "closed" in r.json()["detail"]
    client.put(f"/api/admin/{PATH}/settings", json={"signups_open": True}, headers=h)
    assert signup(client, "late@example.com").status_code == 202


def test_the_credential_tool_makes_a_hash_that_verifies():
    from app.security import verify_admin_password
    h = hash_admin_password(PASSWORD)
    assert "$" not in h and verify_admin_password(PASSWORD, h) and not verify_admin_password("x", h)
