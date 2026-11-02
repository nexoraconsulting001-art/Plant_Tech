"""Demo shop data: seeded once, refreshed daily by moving every date forward, demo rows removable."""
from datetime import datetime, timedelta

import db as db_module
import demo_data
from common import now_ist
from conftest import Shop, run

# This afternoon: the shop's own test bill below is made with the real clock, so the demo uses the same day.
NOW = now_ist().replace(hour=15, minute=5, second=0, microsecond=0)


def test_seed_refresh_remove(client):
    shop = Shop(client)
    own = shop.product("Kopi Plant", 850, 18)
    shop.post("/api/invoices", {"items": [{"product_id": own["id"], "name": "Kopi Plant", "quantity": 1, "price": 850}],
                                "customer_phone": "9811100099", "customer_name": "Own Customer"})
    db = db_module.state.db

    r = run(demo_data.seed(db, "9811122334", now=NOW))
    assert r["seeded"] and r["products"] == 40 and r["customers"] == len(demo_data.CUSTOMERS) and r["orders"] > 300

    by_name = {p["name"]: p for p in shop.get("/api/products").json()}
    assert len(by_name) == 41 and by_name["Kopi Plant"]["stock"] == 17  # own product untouched by the demo
    assert by_name["Fiddle Leaf Fig"]["out_of_stock"] and by_name["ZZ Plant"]["low_stock"]
    assert all(p["stock"] >= 0 for p in by_name.values())

    invs = run(db.invoices.find({"demo": True}, {"_id": 0, "date_key": 1, "customer_id": 1}).to_list(5000))
    assert all(i["customer_id"] for i in invs)  # every order has a customer
    assert max(i["date_key"] for i in invs) == NOW.date().isoformat()  # a full "today"
    assert min(i["date_key"] for i in invs) >= (NOW - timedelta(days=30)).date().isoformat()
    # the calendar month around two weeks ago always holds at least 15 of the demo's 30 days (not so this month's)
    month = shop.get("/api/reports/sales", params={"range": "month", "date": (NOW - timedelta(days=15)).date().isoformat()}).json()
    assert month["orders"] > 100 and month["profit"] > 0 and month["top"]
    assert run(demo_data.seed(db, "9811122334", now=NOW))["seeded"] is False  # seeding twice: refused

    # Next morning: everything (the shop's own bill too) moves one day forward, so "today" is full again.
    before = {i["id"]: i["date_key"] for i in run(db.invoices.find({}, {"_id": 0, "id": 1, "date_key": 1}).to_list(5000))}
    tomorrow = NOW.date() + timedelta(days=1)
    assert run(demo_data.refresh(db, "9811122334", today=tomorrow))["shifted_days"] == 1
    after = {i["id"]: i["date_key"] for i in run(db.invoices.find({}, {"_id": 0, "id": 1, "date_key": 1}).to_list(5000))}
    assert all(after[k] == (datetime.fromisoformat(v) + timedelta(days=1)).date().isoformat() for k, v in before.items())
    today = shop.get("/api/invoices/summary", params={"day": tomorrow.isoformat()}).json()
    assert today["orders"] > 0
    assert run(demo_data.refresh(db, "9811122334", today=tomorrow))["shifted_days"] == 0  # once a day only
    # Missed days catch up in one go.
    assert run(demo_data.refresh(db, "9811122334", today=tomorrow + timedelta(days=3)))["shifted_days"] == 3

    # A shop without demo data is never moved.
    Shop(client, phone="9811155667", name="Real")
    assert run(demo_data.refresh(db, "9811155667", today=tomorrow))["shifted_days"] == 0

    business_id = shop.get("/api/auth/me").json()["business"]["id"]
    run(demo_data.remove_demo(db, business_id))
    assert [p["name"] for p in shop.get("/api/products").json()] == ["Kopi Plant"]
