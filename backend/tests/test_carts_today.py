"""Today's draft orders on New order (GET /api/orders/cart/today): saved today, not billed, newest first."""
import uuid
from datetime import datetime, timedelta, timezone

import db as db_module
from conftest import Shop, run


def _customer(shop, name="Kavita", phone="+919811100011"):
    r = shop.post("/api/customers", {"name": name, "phone": phone})
    assert r.status_code == 200, r.text
    return r.json()


def _save(shop, customer, items, h=None, discount=0):
    r = shop.c.put("/api/orders/cart", headers=h or shop.h,
                   json={"customer_id": customer["id"], "items": items, "discount": discount})
    assert r.status_code == 200, r.text


def test_todays_drafts_newest_first_with_totals_and_who(client):
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    kavita, rahul = _customer(shop), _customer(shop, "Rahul", "+919811100022")
    _, worker_h = shop.worker()
    _save(shop, kavita, [{"product_id": plant["id"], "name": "Money Plant", "quantity": 2, "price": 250},
                         {"product_id": "loose-a", "name": "Clay pot", "quantity": 1, "price": 80}], discount=30)
    _save(shop, rahul, [{"product_id": plant["id"], "name": "Money Plant", "quantity": 1, "price": 240}], h=worker_h)
    drafts = shop.get("/api/orders/cart/today").json()
    assert [d["customer"]["name"] for d in drafts] == ["Rahul", "Kavita"]
    assert drafts[1]["lines"] == 2 and drafts[1]["total"] == 550 and drafts[1]["updated_by"] == "Suresh"
    assert drafts[0]["total"] == 240 and drafts[0]["updated_by"] == "Ramesh"
    assert drafts[0]["customer"]["phone"].endswith("9811100022") and drafts[0]["customer"]["id"] == rahul["id"]
    # everyone who can sell sees all of them
    assert len(client.get("/api/orders/cart/today", headers=worker_h).json()) == 2


def test_billed_discarded_yesterdays_other_shops_and_erased_are_left_out(client):
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    a, b, c, d = (_customer(shop, n, f"+91981110003{i}") for i, n in enumerate("ABCD"))
    line = [{"product_id": plant["id"], "name": "Money Plant", "quantity": 1, "price": 250}]
    for x in (a, b, c, d):
        _save(shop, x, line)
    # A billed (the bill clears its saved order), B discarded, C saved yesterday, D's details erased
    r = shop.c.post("/api/invoices", headers={**shop.h, "Idempotency-Key": str(uuid.uuid4())}, json={
        "items": [{"product_id": plant["id"], "name": "Money Plant", "quantity": 1, "price": 250}],
        "customer_id": a["id"], "payment_method": "Cash"})
    assert r.status_code == 200, r.text
    assert shop.c.delete("/api/orders/cart", headers=shop.h, params={"customer_id": b["id"]}).status_code == 200
    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
    run(db_module.get_db().carts.update_one({"customer_id": c["id"]}, {"$set": {"saved_at": yesterday}}))
    assert shop.post(f"/api/customers/{d['id']}/erase", {}).status_code == 200
    other = Shop(client, phone="9811122999", name="Other")
    x = _customer(other, "X", "+919811100099")
    _save(other, x, [{"product_id": "loose-1", "name": "Pot", "quantity": 1, "price": 50}])
    assert shop.get("/api/orders/cart/today").json() == []
    assert [d["customer"]["name"] for d in other.get("/api/orders/cart/today").json()] == ["X"]


def test_drafts_need_the_sell_permission(client):
    shop = Shop(client)
    _, h = shop.worker(phone="9822233446", sell=False)
    assert client.get("/api/orders/cart/today", headers=h).status_code == 403
