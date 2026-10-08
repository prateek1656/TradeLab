from conftest import signup


def test_signup_logs_straight_in(client):
    r = signup(client, "owner@example.com")
    assert r.status_code == 201
    data = r.json()
    assert data["token"]
    assert data["user"]["email"] == "owner@example.com"
    assert "role" not in data["user"] and "status" not in data["user"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.status_code == 200


def test_everyone_can_sign_up_and_log_in_without_approval(client, owner):
    r = signup(client, "friend@example.com", "Friend")
    assert r.status_code == 201
    r = client.post("/api/auth/login", json={"email": "friend@example.com", "password": "supersecret1"})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 200
    assert signup(client, "friend@example.com").status_code == 409


def test_admin_feature_is_gone(client, owner):
    assert client.get("/api/admin/users", headers=owner).status_code == 404
    assert client.get("/api/admin/system", headers=owner).status_code == 404


def test_signup_needs_no_code_or_approval(client):
    assert client.get("/api/auth/config").status_code == 404
    r = client.post("/api/auth/signup", json={"name": "Friend", "email": "f@example.com", "password": "supersecret1"})
    assert r.status_code == 201 and r.json()["token"]


def test_wrong_password_and_unauthenticated(client, owner):
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "nope-nope"})
    assert r.status_code == 401
    assert client.get("/api/accounts").status_code == 401


def test_change_password_signs_out_other_sessions(client, owner):
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "supersecret1", "new_password": "evenbetter22"},
        headers=owner,
    )
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=owner).status_code == 401
    new = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/auth/me", headers=new).status_code == 200


def test_holidays_shared_by_all_users(client, owner):
    r = client.post("/api/holidays", json={"day": "2026-11-09", "description": "Diwali"}, headers=owner)
    assert r.status_code == 201
    other = signup(client, "b@example.com").json()["token"]
    rows = client.get("/api/holidays", headers={"Authorization": f"Bearer {other}"}).json()
    assert rows == [{"day": "2026-11-09", "description": "Diwali"}]
    assert client.delete("/api/holidays/2026-11-09", headers=owner).status_code == 204
    assert client.get("/api/holidays", headers=owner).json() == []
    assert client.get("/api/holidays").status_code == 401


def test_spa_and_private_headers(client):
    r = client.get("/robots.txt")
    assert "Disallow: /" in r.text
    assert r.headers["X-Robots-Tag"].startswith("noindex")
    assert client.get("/api/nope").status_code == 404
