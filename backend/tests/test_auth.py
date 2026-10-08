from conftest import signup


def test_first_signup_becomes_admin(client):
    r = signup(client, "owner@example.com")
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "approved"
    assert data["user"]["role"] == "admin"


def test_new_users_wait_for_approval_and_can_be_revoked(client, admin):
    r = signup(client, "friend@example.com", "Friend")
    assert r.json()["status"] == "pending"
    assert "token" not in r.json()

    r = client.post("/api/auth/login", json={"email": "friend@example.com", "password": "supersecret1"})
    assert r.status_code == 403
    assert "approval" in r.json()["detail"]

    users = client.get("/api/admin/users", headers=admin).json()
    friend = next(u for u in users if u["email"] == "friend@example.com")
    assert friend["status"] == "pending"
    r = client.patch(f"/api/admin/users/{friend['id']}", json={"status": "approved"}, headers=admin)
    assert r.json()["status"] == "approved"

    r = client.post("/api/auth/login", json={"email": "friend@example.com", "password": "supersecret1"})
    assert r.status_code == 200
    friend_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/auth/me", headers=friend_headers).status_code == 200
    # Members cannot open the admin panel.
    assert client.get("/api/admin/users", headers=friend_headers).status_code == 403

    client.patch(f"/api/admin/users/{friend['id']}", json={"status": "revoked"}, headers=admin)
    assert client.get("/api/auth/me", headers=friend_headers).status_code == 401
    r = client.post("/api/auth/login", json={"email": "friend@example.com", "password": "supersecret1"})
    assert r.status_code == 403


def test_admin_cannot_lock_themselves_out(client, admin):
    me = client.get("/api/auth/me", headers=admin).json()
    r = client.patch(f"/api/admin/users/{me['id']}", json={"status": "revoked"}, headers=admin)
    assert r.status_code == 400
    r = client.patch(f"/api/admin/users/{me['id']}", json={"role": "member"}, headers=admin)
    assert r.status_code == 400


def test_wrong_password_and_unauthenticated(client, admin):
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "nope-nope"})
    assert r.status_code == 401
    assert client.get("/api/accounts").status_code == 401


def test_change_password_signs_out_other_sessions(client, admin):
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "supersecret1", "new_password": "evenbetter22"},
        headers=admin,
    )
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=admin).status_code == 401
    new = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/auth/me", headers=new).status_code == 200


def test_spa_and_private_headers(client):
    r = client.get("/robots.txt")
    assert "Disallow: /" in r.text
    assert r.headers["X-Robots-Tag"].startswith("noindex")
    assert client.get("/api/nope").status_code == 404
