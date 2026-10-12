from conftest import fake_for, kotak_payload


def test_add_kotak_account_logs_in_once_and_hides_secrets(client, owner):
    r = client.post("/api/accounts", json=kotak_payload(), headers=owner)
    assert r.status_code == 201, r.text
    acc = r.json()
    assert acc["status"] == "connected"
    assert acc["client_name"] == "Test Client"
    assert acc["ucc_masked"].endswith("C12")
    assert "mpin" not in r.text and "123456" not in r.text and "ck-123" not in r.text
    assert fake_for(acc["id"]).logins == 1
    assert fake_for(acc["id"]).creds["mobile"] == "+919876543210"


def test_bad_login_is_rejected_and_not_saved(client, owner):
    r = client.post("/api/accounts", json=kotak_payload(mpin="000000"), headers=owner)
    assert r.status_code == 400
    assert "Invalid MPIN" in r.json()["detail"]
    assert client.get("/api/accounts", headers=owner).json() == []


def test_invalid_totp_secret_and_duplicates(client, owner):
    r = client.post("/api/accounts", json=kotak_payload(totp_secret="not base32!!"), headers=owner)
    assert r.status_code == 400
    assert client.post("/api/accounts", json=kotak_payload(), headers=owner).status_code == 201
    r = client.post("/api/accounts", json=kotak_payload(name="Again", ucc="ABC12"), headers=owner)
    assert r.status_code == 409


def test_otpauth_uri_is_accepted(client, owner):
    uri = "otpauth://totp/Kotak:ABC12?secret=JBSWY3DPEHPK3PXP&issuer=Kotak"
    r = client.post("/api/accounts", json=kotak_payload(totp_secret=uri), headers=owner)
    assert r.status_code == 201
    assert fake_for(r.json()["id"]).creds["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_rename_and_toggle(client, owner):
    acc = client.post("/api/accounts", json=kotak_payload(), headers=owner).json()
    r = client.patch(f"/api/accounts/{acc['id']}", json={"name": "Wife's account", "automation_enabled": False}, headers=owner)
    assert r.json()["name"] == "Wife's account"
    assert r.json()["automation_enabled"] is False


def test_delete_requires_zero_balance(client, owner):
    acc = client.post("/api/accounts", json=kotak_payload(), headers=owner).json()
    fake = fake_for(acc["id"])
    fake.funds_val = 1520.75
    r = client.delete(f"/api/accounts/{acc['id']}", headers=owner)
    assert r.status_code == 409
    assert "₹1,520.75" in r.json()["detail"]

    fake.funds_val = 0.0
    fake.place_market_order("nse_cm", "SBIN-EQ", "B", 5)
    r = client.delete(f"/api/accounts/{acc['id']}", headers=owner)
    assert r.status_code == 409
    assert "open position" in r.json()["detail"]

    fake.place_market_order("nse_cm", "SBIN-EQ", "S", 5)
    assert client.delete(f"/api/accounts/{acc['id']}", headers=owner).status_code == 204
    assert client.get("/api/accounts", headers=owner).json() == []


def test_delete_blocked_when_balance_cannot_be_verified(client, owner):
    acc = client.post("/api/accounts", json=kotak_payload(), headers=owner).json()
    fake = fake_for(acc["id"])
    fake.funds_val = 0.0

    def broken():
        raise RuntimeError("Kotak is down")

    fake.funds = broken
    r = client.delete(f"/api/accounts/{acc['id']}", headers=owner)
    assert r.status_code == 409
    assert "verify" in r.json()["detail"]


def test_paper_account_delete_rule(client, owner):
    acc = client.post("/api/accounts", json={"name": "Practice", "broker": "paper", "paper_funds": 50000}, headers=owner).json()
    assert client.delete(f"/api/accounts/{acc['id']}", headers=owner).status_code == 409
    client.patch(f"/api/accounts/{acc['id']}", json={"paper_funds": 0}, headers=owner)
    assert client.delete(f"/api/accounts/{acc['id']}", headers=owner).status_code == 204


def test_accounts_are_private_per_user(client, owner):
    acc = client.post("/api/accounts", json=kotak_payload(), headers=owner).json()
    body = {"name": "Bee", "email": "b@example.com", "password": "supersecret1"}
    tok = client.post("/api/auth/signup", json=body).json()["token"]
    other = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/accounts", headers=other).json() == []
    assert client.delete(f"/api/accounts/{acc['id']}", headers=other).status_code == 404
    assert client.patch(f"/api/accounts/{acc['id']}", json={"name": "x"}, headers=other).status_code == 404


def test_inr_grouping():
    from app.money import inr

    assert inr(250000) == "₹2,50,000.00"
    assert inr(1520.75) == "₹1,520.75"
    assert inr(12345678.9) == "₹1,23,45,678.90"
    assert inr(5) == "₹5.00"
    assert inr(-999.5) == "-₹999.50"


def test_a_refused_list_edit_changes_nothing(client, owner):
    """An edit refused for a stale account list must not save its other changes (e.g. Sell first)."""
    acc = client.post("/api/accounts", json=kotak_payload(), headers=owner).json()["id"]
    item = client.post("/api/basket", json={"symbol": "SBIN", "trading_symbol": "SBIN-EQ", "sizing_mode": "qty",
                                            "sizing_value": 5}, headers=owner).json()
    r = client.patch(f"/api/basket/{item['id']}", json={"sizing_value": 50, "active": False, "side": "S",
                                                        "account_ids": [acc, 987654]}, headers=owner)
    assert r.status_code == 400
    (after,) = client.get("/api/basket", headers=owner).json()
    assert (after["sizing_value"], after["active"], after["side"], after["account_ids"]) == (5, True, "B", None)
