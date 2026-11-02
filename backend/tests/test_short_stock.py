"""Selling more than the stock on hand: the owner may (stock goes below zero), employees may not."""
import notify
from conftest import Shop


def _order(shop, items, headers=None, **extra):
    extra.setdefault("customer_phone", "9811100099")
    extra.setdefault("customer_name", "Test Customer")
    return shop.c.post("/api/invoices", headers=headers or shop.h, json={"items": items, **extra})


def _stock(shop, pid):
    return shop.get(f"/api/products/{pid}").json()


def test_owner_bills_below_stock_only_when_asked(client):
    shop = Shop(client, gst=True)
    rose = shop.product("Rose", 100, 3)
    line = [{"product_id": rose["id"], "name": "Rose", "quantity": 5, "price": 100}]
    # an owner on an app that doesn't ask (QA 1.1.0) still gets the usual refusal
    r = _order(shop, line)
    assert r.status_code == 400 and r.json()["code"] == "out_of_stock" and r.json()["detail"] == "Only 3 Rose left in stock."
    assert _stock(shop, rose["id"])["stock"] == 3
    r = _order(shop, line, allow_short=True)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["short_qty"] == 2
    p = _stock(shop, rose["id"])
    assert p["stock"] == -2 and p["out_of_stock"] and p["low_stock"]
    assert p["history"][0]["type"] == "sale" and p["history"][0]["new_stock"] == -2


def test_short_sale_keeps_gst_and_line_link(client):
    shop = Shop(client, gst=True)
    pot = shop.product("Ceramic Pot", 118, 0, category="Pots")
    inv = _order(shop, [{"product_id": pot["id"], "name": "Ceramic Pot", "quantity": 1, "price": 118}], allow_short=True).json()
    it = inv["items"][0]
    assert it["product_id"] == pot["id"] and it["gst_percent"] == 18 and it["short_qty"] == 1
    assert inv["gst_total"] == 18


def test_already_negative_and_enough_stock(client):
    shop = Shop(client)
    a = shop.product("Money Plant", 250, 1)
    b = shop.product("Snake Plant", 450, 10)
    _order(shop, [{"product_id": a["id"], "name": "Money Plant", "quantity": 2, "price": 250}], allow_short=True)
    inv = _order(shop, [{"product_id": a["id"], "name": "Money Plant", "quantity": 3, "price": 250},
                        {"product_id": b["id"], "name": "Snake Plant", "quantity": 2, "price": 450}], allow_short=True).json()
    assert inv["items"][0]["short_qty"] == 3  # nothing of it was in stock
    assert "short_qty" not in inv["items"][1]  # had enough
    assert _stock(shop, a["id"])["stock"] == -4 and _stock(shop, b["id"])["stock"] == 8


def test_decimal_short(client):
    shop = Shop(client)
    shop.post("/api/categories/bulk", {"items": [{"name": "Soil", "gst_percent": 0}]})
    shop.cat = {c["name"]: c["id"] for c in shop.get("/api/categories").json()}
    soil = shop.product("Cocopeat", 40, 1.5, category="Soil", unit="kg")
    inv = _order(shop, [{"product_id": soil["id"], "name": "Cocopeat", "quantity": 2.25, "price": 40}], allow_short=True).json()
    assert inv["items"][0]["short_qty"] == 0.75
    assert _stock(shop, soil["id"])["stock"] == -0.75


def test_employee_is_blocked_even_when_asking(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 3)
    _, worker = shop.worker()
    r = _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 4, "price": 100}], headers=worker, allow_short=True)
    assert r.status_code == 400
    assert r.json() == {"detail": "Only 3 Rose left in stock.", "code": "out_of_stock",
                        "key": "Only {{n}} {{name}} left in stock.", "params": {"n": "3", "name": "Rose"}}
    assert _stock(shop, rose["id"])["stock"] == 3
    # up to the last one in stock is fine
    r = _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 3, "price": 100}], headers=worker)
    assert r.status_code == 200, r.text


def test_stock_in_on_negative_and_removal_limit(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 1)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 3, "price": 100}], allow_short=True)
    assert shop.post(f"/api/products/{rose['id']}/stock", {"change": -1, "reason": "Damaged"}).status_code == 400
    r = shop.post(f"/api/products/{rose['id']}/stock", {"change": 5})
    assert r.status_code == 200 and r.json()["stock"] == 3 and not r.json()["out_of_stock"]


def test_products_list_and_filters_with_negative_stock(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 1)
    shop.product("Tulsi", 50, 20)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}], allow_short=True)
    listed = {p["name"]: p for p in shop.get("/api/products").json()}
    assert listed["Rose"]["stock"] == -1 and listed["Rose"]["out_of_stock"]
    assert [p["name"] for p in shop.get("/api/products?low=true").json()] == ["Rose"]
    only = shop.get(f"/api/products?ids={rose['id']}").json()
    assert [p["name"] for p in only] == ["Rose"]


def test_owner_home_lists_products_below_zero(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 1)
    tulsi = shop.product("Tulsi", 50, 0)
    shop.product("Fern", 80, 5)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100},
                  {"product_id": tulsi["id"], "name": "Tulsi", "quantity": 3, "price": 50}], allow_short=True)
    d = shop.get("/api/dashboard/today").json()
    assert [(p["name"], p["stock"], p["unit"]) for p in d["below_zero"]] == [("Tulsi", -3, "pcs"), ("Rose", -1, "pcs")]
    assert d["below_zero_count"] == 2
    _, worker = shop.worker(reports=True)
    assert "below_zero" not in client.get("/api/dashboard/today", headers=worker).json()


def test_stock_alert_hook(client, monkeypatch):
    seen = []

    async def alert(db, business_id, product, kind, stock):
        seen.append((product["name"], kind, stock))

    monkeypatch.setattr(notify, "stock_alert", alert)
    shop = Shop(client)
    rose = shop.product("Rose", 100, 8, min_stock=5)
    sell = [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}]
    _order(shop, sell)  # 8 -> 6: nothing to tell
    _order(shop, sell)  # 6 -> 4: low
    _order(shop, sell)  # 4 -> 2: already low, said once
    _order(shop, [{**sell[0], "quantity": 3}], allow_short=True)  # 2 -> -1: below zero
    assert seen == [("Rose", "low_stock", 4), ("Rose", "below_zero", -1)]


def test_stock_alert_never_breaks_a_sale(client, monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("push service down")

    monkeypatch.setattr(notify, "stock_alert", broken)
    shop = Shop(client)
    rose = shop.product("Rose", 100, 1)
    r = _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}], allow_short=True)
    assert r.status_code == 200


def test_crossed():
    assert notify.crossed(3, -1, 5) == "below_zero"
    assert notify.crossed(-1, -3, 5) is None
    assert notify.crossed(6, 5, 5) == "low_stock"
    assert notify.crossed(5, 4, 5) is None
    assert notify.crossed(0, 5, 5) is None
