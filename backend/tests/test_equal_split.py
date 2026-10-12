"""Default sizing: each account's money is split equally across that day's stocks."""

from conftest import FakeKotak, buy, fake_for, kotak_payload

from app.services import trading

PRICES = [100, 250, 333, 999, 1500, 2600, 3333, 5000, 9999, 12000]


def _account(client, owner, name, ucc, funds):
    acc = client.post("/api/accounts", json=kotak_payload(name=name, ucc=ucc), headers=owner).json()
    fake_for(acc["id"]).funds_val = funds
    return acc["id"]


def _stock(client, owner, sym, token, **extra):
    r = client.post("/api/basket", json={"symbol": sym, "trading_symbol": f"{sym}-EQ", "token": token, **extra},
                    headers=owner)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _me(client, owner):
    return client.get("/api/auth/me", headers=owner).json()["id"]


def test_equal_split_is_the_default(client, owner):
    s = client.get("/api/automation/settings", headers=owner).json()
    assert s["sizing_mode"] == "equal" and s["reserve_pct"] == 0


def test_one_lakh_over_ten_stocks_buys_max_shares_of_10k_each(client, owner):
    acc = _account(client, owner, "Main", "UCC1", 100000)
    for i, price in enumerate(PRICES):
        FakeKotak.prices[str(1000 + i)] = price
        _stock(client, owner, f"S{i}", str(1000 + i))
    buy(client, owner)

    placed = {p[2]: p[4] for p in fake_for(acc).placed}
    expected = {f"S{i}-EQ": int(10000 // p) for i, p in enumerate(PRICES) if 10000 // p >= 1}
    assert placed == expected  # S9 at ₹12,000 doesn't fit in ₹10,000 and is skipped
    assert placed["S0-EQ"] == 100 and placed["S2-EQ"] == 30 and placed["S8-EQ"] == 1
    assert all(p[3] == "B" and p[5] == "MIS" for p in fake_for(acc).placed)
    # Never spends more than the allotment per stock, or more than the account holds.
    assert all(placed[f"S{i}-EQ"] * PRICES[i] <= 10000 for i in range(9))

    logs = client.get("/api/automation/logs", headers=owner).json()
    ok = next(l for l in logs if l["symbol"] == "S0-EQ")
    assert "₹10,000 allotted (1/10 of ₹1,00,000)" in ok["message"]
    skipped = next(l for l in logs if l["symbol"] == "S9-EQ")
    assert skipped["status"] == "skipped" and "less than the price of one share" in skipped["message"]


def test_each_account_splits_its_own_money(client, owner):
    a = _account(client, owner, "Big", "UCC1", 100000)
    b = _account(client, owner, "Small", "UCC2", 50000)
    FakeKotak.prices.update({"1": 500.0, "2": 2000.0})
    _stock(client, owner, "AAA", "1")
    _stock(client, owner, "BBB", "2")
    buy(client, owner)
    assert sorted((p[2], p[4]) for p in fake_for(a).placed) == [("AAA-EQ", 100), ("BBB-EQ", 25)]
    assert sorted((p[2], p[4]) for p in fake_for(b).placed) == [("AAA-EQ", 50), ("BBB-EQ", 12)]


def test_parts_count_only_stocks_that_trade_in_that_account(client, owner):
    a = _account(client, owner, "A", "UCC1", 90000)
    b = _account(client, owner, "B", "UCC2", 90000)
    _stock(client, owner, "AAA", "1")
    _stock(client, owner, "BBB", "2", account_ids=[a])           # only account A
    off = _stock(client, owner, "CCC", "3")
    client.patch(f"/api/basket/{off}", json={"active": False}, headers=owner)  # switched off: not counted
    buy(client, owner)
    assert sorted((p[2], p[4]) for p in fake_for(a).placed) == [("AAA-EQ", 450), ("BBB-EQ", 450)]  # 45k each
    assert sorted((p[2], p[4]) for p in fake_for(b).placed) == [("AAA-EQ", 900)]                   # all 90k


def test_reserve_keeps_money_aside(client, owner):
    acc = _account(client, owner, "A", "UCC1", 100000)
    client.put("/api/automation/settings", json={"reserve_pct": 10}, headers=owner)
    _stock(client, owner, "AAA", "1")
    _stock(client, owner, "BBB", "2")
    buy(client, owner)
    assert [p[4] for p in fake_for(acc).placed] == [450, 450]  # (1,00,000 − 10%) / 2 / ₹100
    assert client.put("/api/automation/settings", json={"reserve_pct": 80}, headers=owner).status_code == 422


def test_empty_or_unreadable_funds_place_nothing(client, owner):
    empty = _account(client, owner, "Empty", "UCC1", 0)
    broken = _account(client, owner, "Broken", "UCC2", 50000)

    def fail():
        raise RuntimeError("limits API down")

    fake_for(broken).funds = fail
    _stock(client, owner, "AAA", "1")
    buy(client, owner)
    assert fake_for(empty).placed == [] and fake_for(broken).placed == []
    msgs = {l["account_name"]: l["message"] for l in client.get("/api/automation/logs", headers=owner).json()}
    assert msgs["Empty"] == "Not enough money: this account has ₹0.00 available"
    assert "Could not read available funds" in msgs["Broken"]


def test_missing_price_skips_only_that_stock(client, owner):
    acc = _account(client, owner, "A", "UCC1", 20000)
    _stock(client, owner, "AAA", "1")
    _stock(client, owner, "ZZZ", None)  # no instrument token -> no live quote
    buy(client, owner)
    # The allotment stays split in two; the unpriced stock is skipped, not re-shared.
    assert [(p[2], p[4]) for p in fake_for(acc).placed] == [("AAA-EQ", 100)]


def test_short_side_uses_the_same_allotment(client, owner):
    acc = _account(client, owner, "A", "UCC1", 10000)
    _stock(client, owner, "AAA", "1", side="S")
    buy(client, owner)
    assert [(p[3], p[4]) for p in fake_for(acc).placed] == [("S", 100)]


def test_preview_shows_orders_without_placing_them(client, owner):
    acc = _account(client, owner, "A", "UCC1", 100000)
    FakeKotak.prices.update({"1": 400.0, "2": 60000.0})
    _stock(client, owner, "AAA", "1")
    _stock(client, owner, "BIG", "2")
    rows = client.get("/api/automation/preview", headers=owner).json()
    assert fake_for(acc).placed == []
    by = {r["symbol"]: r for r in rows}
    assert by["AAA"]["quantity"] == 125 and by["AAA"]["skip"] is None and by["AAA"]["price"] == 400.0
    assert by["BIG"]["quantity"] == 0 and "less than the price of one share" in by["BIG"]["skip"]


def test_custom_mode_still_uses_each_stocks_size(client, owner, custom_sizing):
    acc = _account(client, owner, "A", "UCC1", 100000)
    _stock(client, owner, "AAA", "1", sizing_mode="qty", sizing_value=7)
    buy(client, owner)
    assert [p[4] for p in fake_for(acc).placed] == [7]
