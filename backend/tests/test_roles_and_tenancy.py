"""Workers only do what the owner allowed; one shop never sees another shop's data."""
from conftest import Shop, run
from db import get_db
from test_auth import add_worker


def test_worker_permissions(client):
    shop = Shop(client)
    shop.product("Money Plant", 250, 10)
    _, wh = add_worker(shop)
    me = client.get("/api/auth/me", headers=wh).json()
    assert me["role"] == "worker" and me["permissions"]["reports"] is False
    assert client.get("/api/staff", headers=wh).status_code == 403
    assert client.get("/api/reports/sales", headers=wh).status_code == 403
    assert client.put("/api/business", headers=wh, json={"name": "X"}).status_code == 403
    products = client.get("/api/products", headers=wh).json()
    assert "cost_price" not in products[0]  # workers never see cost prices
    pid = products[0]["id"]
    r = client.post("/api/invoices", headers=wh, json={"items": [{"product_id": pid, "name": "Money Plant",
                                                                    "quantity": 1, "price": 250}], "customer_phone": "9811100099", "customer_name": "Test Customer"})
    assert r.status_code == 200 and r.json()["created_by"]["name"] == "Ramesh"


def test_worker_without_stock_permission_cannot_add_products(client):
    shop = Shop(client)
    _, wh = add_worker(shop, stock=False)
    r = client.post("/api/products", headers=wh, json={"name": "Rose", "selling_price": 100})
    assert r.status_code == 403


def test_switching_off_or_new_pin_logs_worker_out(client):
    shop = Shop(client)
    wid, wh = add_worker(shop)
    assert client.patch(f"/api/staff/{wid}", headers=shop.h, json={"password": "9999"}).status_code == 400  # 6+ now
    assert client.patch(f"/api/staff/{wid}", headers=shop.h, json={"password": "907153"}).status_code == 200
    assert client.get("/api/auth/me", headers=wh).status_code == 401
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "907153"}).status_code == 200
    client.patch(f"/api/staff/{wid}", headers=shop.h, json={"active": False})
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "907153"}).status_code == 403
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "000000"}).status_code == 401


def test_owner_new_pin_ends_the_wait(client):
    shop = Shop(client)
    wid, _ = add_worker(shop)
    for _ in range(5):
        client.post("/api/auth/login", json={"phone": "9822233445", "password": "wrong1"})
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "482913"}).status_code == 429
    assert client.patch(f"/api/staff/{wid}", headers=shop.h, json={"password": "730214"}).status_code == 200
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "730214"}).status_code == 200


def test_staff_changes_are_audited(client):
    shop = Shop(client)
    wid, _ = add_worker(shop)
    client.patch(f"/api/staff/{wid}", headers=shop.h, json={"permissions": {"reports": True}, "password": "730214"})
    entries = run(get_db().audit_log.find({"action": {"$regex": "^staff"}}, {"_id": 0}).sort("at", 1).to_list(10))
    assert [e["action"] for e in entries] == ["staff.add", "staff.update"]
    assert entries[0]["details"]["phone"] == "******3445"  # never the full number
    update = entries[1]
    assert update["actor"]["role"] == "owner" and update["target"]["id"] == wid
    assert update["details"]["permissions"]["reports"] is True and update["details"]["permissions_before"]["reports"] is False
    assert update["details"]["pin_changed"] is True and "password_hash" not in update["details"]


def test_removing_an_employee_erases_their_login(client):
    shop = Shop(client)
    p = shop.product("Money Plant", 250, 10)
    wid, wh = add_worker(shop)
    client.post("/api/invoices", headers=wh, json={"items": [{"product_id": p["id"], "name": "Money Plant",
                                                               "quantity": 1, "price": 250}],
                                                    "customer_phone": "9811100099", "customer_name": "Test Customer"})
    other = Shop(client, phone="9811155667", name="Bina")
    assert client.delete(f"/api/staff/{wid}", headers=other.h).status_code == 404  # not their employee
    assert client.delete(f"/api/staff/{wid}", headers=shop.h).status_code == 200
    assert client.get("/api/auth/me", headers=wh).status_code == 401
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "482913"}).status_code == 401
    assert shop.get("/api/staff").json() == []
    gone = run(get_db().users.find_one({"id": wid}, {"_id": 0}))
    assert gone["name"] == "Former employee" and "phone" not in gone and "password_hash" not in gone
    assert shop.get("/api/invoices").json()[0]["created_by"]["id"] == wid  # the shop's bills are kept
    # the number is free again
    add_worker(shop)


def test_shops_are_isolated(client):
    a = Shop(client, phone="9811122334", name="Asha")
    b = Shop(client, phone="9811155667", name="Bina")
    pa = a.product("Money Plant", 250, 5)
    a.post("/api/invoices", {"items": [{"product_id": pa["id"], "name": "Money Plant", "quantity": 1, "price": 250}],
                             "customer_phone": "9876501234", "customer_name": "Anita"})
    assert b.get("/api/products").json() == []
    assert b.get("/api/customers").json() == []
    assert b.get("/api/invoices").json() == []
    assert b.get(f"/api/products/{pa['id']}").status_code == 404
    # B can't sell A's product, and A's stock is untouched by the attempt
    r = b.post("/api/invoices", {"items": [{"product_id": pa["id"], "name": "x", "quantity": 1, "price": 1}], "customer_phone": "9811100099", "customer_name": "Test Customer"})
    assert r.status_code == 400
    assert a.get(f"/api/products/{pa['id']}").json()["stock"] == 4
    # a worker of B can't edit A's categories either
    assert client.patch(f"/api/categories/{a.cat['Pots']}", headers=b.h, json={"gst_percent": 5}).status_code == 404


def test_worker_sees_only_own_orders(client):
    shop = Shop(client)
    p = shop.product("Money Plant", 250, 10)
    _, wh = add_worker(shop)
    line = {"items": [{"product_id": p["id"], "name": "Money Plant", "quantity": 1, "price": 250}], "customer_phone": "9811100099", "customer_name": "Test Customer"}
    shop.post("/api/invoices", line)
    client.post("/api/invoices", headers=wh, json=line)
    assert len(shop.get("/api/invoices").json()) == 2
    assert len(client.get("/api/invoices", headers=wh).json()) == 1
    assert shop.get("/api/dashboard/today").json()["orders"] == 2
    mine = client.get("/api/dashboard/today", headers=wh).json()
    assert mine["scope"] == "mine" and mine["orders"] == 1
