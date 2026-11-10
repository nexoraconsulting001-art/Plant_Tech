"""Supplier bills: stock, new products priced at the shop's profit % or the price typed on the bill, an existing
product taking a changed price from the bill, one supplier record per supplier."""

import db as db_module
from conftest import Shop, photo_b64, run
from routers import suppliers


def _bill(shop, items, **extra):
    return shop.post("/api/purchases", {"supplier": "Green Farms", "invoice_number": "GF-1", "items": items, **extra})


ITEM = [{"name": "Areca Palm", "quantity": 1, "purchase_price": 100}]


def test_profit_percent_default_and_used_for_new_products(client):
    shop = Shop(client)
    assert client.get("/api/auth/me", headers=shop.h).json()["business"]["profit_percent"] == 60
    assert _bill(shop, [{"name": "Areca Palm", "quantity": 2, "purchase_price": 100}]).status_code == 200
    by_name = {p["name"]: p for p in shop.get("/api/products").json()}
    assert by_name["Areca Palm"]["selling_price"] == 160  # cost + 60%

    assert client.put("/api/business", headers=shop.h, json={"profit_percent": 40}).status_code == 200
    assert client.get("/api/auth/me", headers=shop.h).json()["business"]["profit_percent"] == 40
    _bill(shop, [{"name": "Jade Plant", "quantity": 1, "purchase_price": 100}])
    assert {p["name"]: p for p in shop.get("/api/products").json()}["Jade Plant"]["selling_price"] == 140
    assert client.put("/api/business", headers=shop.h, json={"profit_percent": -5}).status_code == 422


def test_selling_price_typed_on_the_bill(client, claude):
    """Owner, 10 Nov 2026: the selling price is set on the bill. A new item gets it; a product already in the shop
    takes a changed one as its price; none sent (older apps) or the same again leaves the price alone. An employee
    with stock permission may set it, as in Edit product; the cost stays the owner's."""
    shop = Shop(client)
    rose = shop.product("Rose", 199, 5)
    claude.invoice = {**claude.invoice, "items": [{"name": "Rose", "quantity": 10, "purchase_price": 90}]}
    line = shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()}).json()["items"][0]
    assert line["product_id"] == rose["id"] and line["current_price"] == 199  # the app starts the price box at it

    def price(name):
        return {p["name"]: p for p in shop.get("/api/products").json()}[name]

    assert _bill(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 10, "purchase_price": 90,
                         "selling_price": 220},
                        {"name": "Areca Palm", "quantity": 2, "purchase_price": 100, "selling_price": 250}]).status_code == 200
    assert price("Rose")["selling_price"] == 220 and price("Rose")["stock"] == 15 and price("Rose")["cost_price"] == 90
    assert price("Areca Palm")["selling_price"] == 250  # not cost + 60%
    _bill(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "purchase_price": 95}])  # an older app
    assert price("Rose")["selling_price"] == 220 and price("Rose")["cost_price"] == 95

    _, worker = shop.worker()
    r = client.post("/api/purchases", headers=worker, json={"supplier": "Green Farms", "items": [
        {"product_id": rose["id"], "name": "Rose", "quantity": 1, "purchase_price": 10, "selling_price": 210}]})
    assert r.status_code == 200, r.text
    assert price("Rose")["selling_price"] == 210 and price("Rose")["cost_price"] == 95  # cost only by the owner
    bill = shop.get("/api/purchases").json()[0]
    assert bill["items"][0]["selling_price"] == 210  # kept with the bill


def test_purchase_creates_supplier_once(client):
    shop = Shop(client)
    _bill(shop, ITEM, supplier="M/s Green Farms Pvt. Ltd.", supplier_phone="+91 98111 00000")
    _bill(shop, ITEM, supplier="Green Farms", supplier_address="Sohna Road")
    sups = shop.get("/api/suppliers").json()
    assert len(sups) == 1
    s = sups[0]
    assert s["name"] == "M/s Green Farms Pvt. Ltd." and s["phone"] == "+91 98111 00000"
    assert s["address"] == "Sohna Road" and s["bills"] == 2  # the second bill only filled in the missing address


def test_match_priority_gstin_phone_name(client):
    shop = Shop(client)
    _bill(shop, ITEM, supplier="Green Farms", supplier_gstin="06ABCDE1234F1Z5")
    _bill(shop, ITEM, supplier="GF Nursery Supplies", supplier_gstin="06abcde1234f1z5")  # same GSTIN
    _bill(shop, ITEM, supplier="Blue Pots", supplier_phone="9811100001")
    _bill(shop, ITEM, supplier="Blue Pottery House", supplier_phone="098111 00001")  # same phone
    assert sorted(s["name"] for s in shop.get("/api/suppliers").json()) == ["Blue Pots", "Green Farms"]


def test_fill_missing_never_overwrites(client):
    shop = Shop(client)
    s = shop.post("/api/suppliers", {"name": "Green Farms", "phone": "9811100002", "address": "Old address"}).json()
    _bill(shop, ITEM, supplier_id=s["id"], supplier_phone="9999999999", supplier_address="New address",
          supplier_gstin="06ABCDE1234F1Z5")
    got = shop.get(f"/api/suppliers/{s['id']}").json()
    assert got["phone"] == "9811100002" and got["address"] == "Old address" and got["gstin"] == "06ABCDE1234F1Z5"
    assert got["purchases"][0]["supplier"] == "Green Farms" and got["bills"] == 1


def test_create_duplicate_supplier_409(client):
    shop = Shop(client)
    first = shop.post("/api/suppliers", {"name": "Green Farms Pvt Ltd"}).json()
    r = shop.post("/api/suppliers", {"name": "green farms"})
    assert r.status_code == 409 and r.json()["code"] == "supplier_exists"
    assert r.json()["params"] == {"name": "Green Farms Pvt Ltd", "id": first["id"]}
    other = shop.post("/api/suppliers", {"name": "Blue Pots"}).json()
    assert client.patch(f"/api/suppliers/{other['id']}", headers=shop.h, json={"name": "Green Farms"}).status_code == 409


def test_suppliers_are_kept_apart_per_shop_and_owner_only_edits(client):
    a, b = Shop(client), Shop(client, phone="9811155667")
    s = a.post("/api/suppliers", {"name": "Green Farms"}).json()
    assert b.get("/api/suppliers").json() == []
    assert b.get(f"/api/suppliers/{s['id']}").status_code == 404
    _, worker = a.worker()
    assert client.get("/api/suppliers", headers=worker).status_code == 200  # stock permission
    assert client.patch(f"/api/suppliers/{s['id']}", headers=worker, json={"name": "X"}).status_code == 403
    assert client.delete(f"/api/suppliers/{s['id']}", headers=a.h).status_code == 200
    assert a.get("/api/suppliers").json() == []


def test_supplier_with_bills_cannot_be_deleted(client):
    shop = Shop(client)
    _bill(shop, ITEM)
    s = shop.get("/api/suppliers").json()[0]
    assert client.delete(f"/api/suppliers/{s['id']}", headers=shop.h).status_code == 400


def test_backfill_links_old_purchases_and_runs_twice_safely(client):
    shop = Shop(client)
    bid = client.get("/api/auth/me", headers=shop.h).json()["business"]["id"]
    db = db_module.state.db
    for no in ("A-1", "A-2"):  # saved before suppliers existed: a name only
        run(db.purchases.insert_one({"id": no, "business_id": bid, "supplier": "Green Farms Ltd",
                                             "invoice_number": no, "items": [], "total": 10, "created_at": "2026-10-01"}))
    run(suppliers.backfill(db))
    run(suppliers.backfill(db))
    sups = shop.get("/api/suppliers").json()
    assert len(sups) == 1 and sups[0]["bills"] == 2


def test_purchase_page_photos_and_older_single_photo(client):
    a, b = Shop(client), Shop(client, phone="9811155667")
    pages = [a.post("/api/upload-base64", {"image_base64": photo_b64()}).json()["path"] for _ in range(2)]
    r = _bill(a, ITEM, image_paths=pages)
    assert r.status_code == 200 and r.json()["image_paths"] == pages
    r = _bill(a, ITEM, image_path=pages[0])  # older app: one photo
    assert r.json()["image_paths"] == [pages[0]]
    assert sorted(len(p["image_paths"]) for p in a.get("/api/purchases").json()) == [1, 2]
    other = b.post("/api/upload-base64", {"image_base64": photo_b64()}).json()["path"]
    assert _bill(a, ITEM, image_paths=[other]).status_code == 400
