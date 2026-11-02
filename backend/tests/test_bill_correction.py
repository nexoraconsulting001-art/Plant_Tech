"""Correcting or cancelling a bill: the owner only, within 7 days of the first bill, never in place."""
import asyncio
from datetime import datetime, timedelta, timezone

import db as db_module
from common import date_key
from conftest import Shop
from routers import sales


def _bill(shop, items, headers=None, **extra):
    extra.setdefault("customer_phone", "9811100099")
    extra.setdefault("customer_name", "Anita")
    r = shop.c.post("/api/invoices", headers=headers or shop.h, json={"items": items, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _line(p, qty, price=None):
    return {"product_id": p["id"], "name": p["name"], "quantity": qty, "price": p["selling_price"] if price is None else price}


def _stock(shop, pid):
    return shop.get(f"/api/products/{pid}").json()


def _customer(shop, cid):
    return shop.get(f"/api/customers/{cid}").json()


def _set(where, change):
    asyncio.run(db_module.state.db.invoices.update_one(where, change))


def _age(inv_id, days):
    """Pretend the bill was made `days` ago."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    _set({"id": inv_id}, {"$set": {"created_at": when}})


def test_cancel_gives_stock_back_and_leaves_every_total(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    keep = _bill(shop, [_line(rose, 1)])
    inv = _bill(shop, [_line(rose, 3)], payment_method="UPI")
    assert _stock(shop, rose["id"])["stock"] == 6
    assert inv["correct_until"]

    r = shop.post(f"/api/invoices/{inv['id']}/cancel", {"reason": "  "})
    assert r.status_code == 400  # a reason is needed
    r = shop.post(f"/api/invoices/{inv['id']}/cancel", {"reason": "Customer returned the plants"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "Cancelled" and out["cancel_reason"] == "Customer returned the plants" and out["cancelled_by"]["name"] == "Suresh"

    p = _stock(shop, rose["id"])
    assert p["stock"] == 9
    assert p["history"][0]["type"] == "bill_cancel" and p["history"][0]["change"] == 3 and p["history"][0]["bill"] == inv["invoice_number"]
    c = _customer(shop, inv["customer_id"])
    assert c["purchase_count"] == 1 and c["total_purchases"] == 100
    assert {b["invoice_number"]: b["status"] for b in c["invoices"]} == {keep["invoice_number"]: "Paid", inv["invoice_number"]: "Cancelled"}

    today = shop.get("/api/dashboard/today").json()
    assert today["sales"] == 100 and today["orders"] == 1 and today["by_method"]["UPI"] == 0
    summary = shop.get("/api/invoices/summary").json()
    assert summary["orders"] == 1 and summary["sales"] == 100
    listed = {o["invoice_number"]: o["status"] for o in shop.get("/api/invoices").json()}
    assert listed == {keep["invoice_number"]: "Paid", inv["invoice_number"]: "Cancelled"}  # still listed, marked
    report = shop.get("/api/reports/sales?range=day").json()
    assert report["sales"] == 100 and report["orders"] == 1 and report["items"][0]["quantity"] == 1
    people = shop.get("/api/reports/employees?range=day").json()
    assert people["orders"] == 1 and people["sales"] == 100

    again = shop.post(f"/api/invoices/{inv['id']}/cancel", {"reason": "twice"})
    assert again.status_code == 400 and again.json()["code"] == "bill_cancelled"
    assert _stock(shop, rose["id"])["stock"] == 9  # not given back twice


def test_correct_makes_a_linked_new_bill_and_moves_only_the_difference(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    pot = shop.product("Ceramic Pot", 118, 5, category="Pots")
    tulsi = shop.product("Tulsi", 50, 4)
    old = _bill(shop, [_line(rose, 3), _line(pot, 1)], payment_method="Cash")
    assert _stock(shop, rose["id"])["stock"] == 7 and _stock(shop, pot["id"])["stock"] == 4

    r = shop.post(f"/api/invoices/{old['id']}/correct", {
        "items": [_line(rose, 2, price=90), _line(tulsi, 1)], "customer_id": old["customer_id"],
        "payment_method": "UPI", "discount": 10, "reason": "Wrong quantity"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["invoice_number"] != old["invoice_number"] and new["status"] == "Paid"
    assert new["replaces"] == {"id": old["id"], "invoice_number": old["invoice_number"]}
    assert new["payment_method"] == "UPI" and new["upi_id"] == "shop@upi" and new["grand_total"] == 220
    assert new["first_billed_at"] == old["created_at"] and new["correct_until"] == old["correct_until"]
    assert new["created_by"] == old["created_by"] and new["corrected_by"]["name"] == "Suresh"

    was = shop.get(f"/api/invoices/{old['id']}").json()
    assert was["status"] == "Cancelled" and was["cancel_reason"] == "Wrong quantity"
    assert was["replaced_by"] == {"id": new["id"], "invoice_number": new["invoice_number"]}

    # rose 3 -> 2: one back; pot 1 -> 0: back; tulsi 0 -> 1: taken
    assert _stock(shop, rose["id"])["stock"] == 8 and _stock(shop, pot["id"])["stock"] == 5 and _stock(shop, tulsi["id"])["stock"] == 3
    h = _stock(shop, rose["id"])["history"][0]
    assert h["type"] == "bill_correct" and h["change"] == 1 and h["bill"] == new["invoice_number"]

    c = _customer(shop, old["customer_id"])
    assert c["purchase_count"] == 1 and c["total_purchases"] == 220
    today = shop.get("/api/dashboard/today").json()
    assert today["sales"] == 220 and today["orders"] == 1 and today["by_method"]["Cash"] == 0
    assert [o["invoice_number"] for o in today["recent"]] == [new["invoice_number"]]

    # the old bill cannot be corrected again; the new one can (same 7 days)
    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 1)], "customer_id": old["customer_id"]})
    assert r.status_code == 400 and r.json()["code"] == "bill_cancelled"


def test_correct_failure_leaves_everything_as_it_was(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 5)
    lily = shop.product("Lily", 80, 1)
    old = _bill(shop, [_line(rose, 2)])
    before = (_stock(shop, rose["id"])["stock"], _stock(shop, lily["id"])["stock"])
    # Lily has only 1 and the app did not allow selling below stock
    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 1), _line(lily, 3)], "customer_id": old["customer_id"]})
    assert r.status_code == 400 and r.json()["code"] == "out_of_stock" and r.json()["params"]["name"] == "Lily"
    assert (_stock(shop, rose["id"])["stock"], _stock(shop, lily["id"])["stock"]) == before
    assert shop.get(f"/api/invoices/{old['id']}").json()["status"] == "Paid"
    assert _stock(shop, rose["id"])["history"][0]["type"] == "sale"  # no history for what was undone
    assert _customer(shop, old["customer_id"])["purchase_count"] == 1
    # the owner may go below zero when asked
    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(lily, 3)], "customer_id": old["customer_id"], "allow_short": True})
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["short_qty"] == 2
    assert _stock(shop, lily["id"])["stock"] == -2 and _stock(shop, rose["id"])["stock"] == 5


def test_correct_counts_the_old_quantity_as_back_in_stock(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 3)
    old = _bill(shop, [_line(rose, 3)])  # stock now 0
    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 3, price=95)], "customer_id": old["customer_id"]})
    assert r.status_code == 200, r.text  # same quantity again: fits, though the shelf shows 0
    assert "short_qty" not in r.json()["items"][0] and r.json()["items"][0]["list_price"] == 100
    assert _stock(shop, rose["id"])["stock"] == 0
    r = shop.post(f"/api/invoices/{r.json()['id']}/correct", {"items": [_line(rose, 4)], "customer_id": old["customer_id"]})
    assert r.status_code == 400 and r.json()["detail"] == "Only 3 Rose left in stock."


def test_owner_only_and_seven_days_from_the_first_bill(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 20)
    wid, wh = shop.worker(reports=True)
    inv = _bill(shop, [_line(rose, 1)], headers=wh)
    for url, body in (("cancel", {"reason": "x"}), ("correct", {"items": [_line(rose, 2)], "customer_id": inv["customer_id"]})):
        r = client.post(f"/api/invoices/{inv['id']}/{url}", headers=wh, json=body)
        assert r.status_code == 403
    assert shop.get(f"/api/invoices/{inv['id']}").json()["status"] == "Paid"

    _age(inv["id"], 6)
    new = shop.post(f"/api/invoices/{inv['id']}/correct", {"items": [_line(rose, 2)], "customer_id": inv["customer_id"]}).json()
    assert new["created_by"]["id"] == wid  # still the employee's sale
    # the replacement is brand new, but the 7 days count from the first bill
    first = datetime.now(timezone.utc) - timedelta(days=7, minutes=1)
    _set({"id": new["id"]}, {"$set": {"first_billed_at": first.isoformat()}})
    r = shop.post(f"/api/invoices/{new['id']}/cancel", {"reason": "late"})
    assert r.status_code == 400 and r.json()["code"] == "bill_locked" and r.json()["params"] == {"n": sales.CORRECT_DAYS}
    assert r.json()["detail"] == "A bill can be corrected or cancelled only within 7 days."

    other = _bill(shop, [_line(rose, 1)])
    _age(other["id"], 8)
    r = shop.post(f"/api/invoices/{other['id']}/correct", {"items": [_line(rose, 1)], "customer_id": other["customer_id"]})
    assert r.status_code == 400 and r.json()["code"] == "bill_locked"


def test_correct_to_another_customer_and_deleted_products(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 5)
    gone = shop.product("Old Fern", 60, 5)
    old = _bill(shop, [_line(rose, 1), _line(gone, 2)])
    assert shop.c.delete(f"/api/products/{gone['id']}", headers=shop.h).status_code == 200
    # a deleted product can't be on the new bill
    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(gone, 1)], "customer_id": old["customer_id"]})
    assert r.status_code == 400 and r.json()["code"] == "product_missing"
    # left out: fine, and the bill moves to a new customer
    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 1)], "customer_phone": "9811100123",
                                                        "customer_name": "Kavita"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["customer_name"] == "Kavita"
    assert _customer(shop, old["customer_id"])["purchase_count"] == 0
    assert _customer(shop, new["customer_id"])["purchase_count"] == 1
    assert _stock(shop, rose["id"])["stock"] == 4


def test_old_bill_without_customer_and_loose_lines(client):
    shop = Shop(client)
    inv = _bill(shop, [{"product_id": None, "name": "Gift wrap", "quantity": 1, "price": 20}])
    _set({"id": inv["id"]}, {"$set": {"customer_id": None}})  # a walk-in bill from before
    r = shop.post(f"/api/invoices/{inv['id']}/cancel", {"reason": "Test bill"})
    assert r.status_code == 200 and r.json()["status"] == "Cancelled"


def test_printed_and_public_bill_marks(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 5)
    old = _bill(shop, [_line(rose, 1)])
    new = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 2)], "customer_id": old["customer_id"]}).json()
    page = shop.get(f"/api/invoices/{old['id']}/html").text
    assert "CANCELLED" in page and f"Replaced by {new['invoice_number']}" in page and "PAID ·" not in page
    token = shop.get(f"/api/invoices/{old['id']}/share").json()["token"]
    public = client.get(f"/i/{token}").text
    assert "CANCELLED" in public and f"Replaced by {new['invoice_number']}" in public
    page = shop.get(f"/api/invoices/{new['id']}/html").text
    assert f"Replaces {old['invoice_number']}" in page and "CANCELLED" not in page and "PAID · Cash" in page


def test_correction_stays_on_the_day_of_the_sale(client):
    """Made on Monday, corrected on Thursday: Monday keeps the sale, Thursday gets nothing."""
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    old = _bill(shop, [_line(rose, 5)])
    sold = (datetime.now(timezone.utc) - timedelta(days=3)).replace(hour=6, minute=0, second=0, microsecond=0)  # 11:30 India time
    monday, today = date_key(sold), date_key()
    _set({"id": old["id"]}, {"$set": {"created_at": sold.isoformat(), "date_key": monday}})

    r = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 4)], "customer_id": old["customer_id"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["date_key"] == monday and new["first_billed_at"] == sold.isoformat()
    assert new["created_at"] > sold.isoformat()  # when the corrected bill was made

    assert shop.get(f"/api/invoices/summary?day={monday}").json() == {
        "orders": 1, "sales": 400, "by_method": {"Cash": 400, "UPI": 0, "Card": 0}, "by_day": {monday: {"orders": 1, "sales": 400}}}
    assert shop.get(f"/api/invoices/summary?day={today}").json()["orders"] == 0
    assert shop.get("/api/dashboard/today").json()["sales"] == 0
    report = shop.get(f"/api/reports/sales?range=day&date={monday}").json()
    assert report["sales"] == 400 and report["buckets"][1]["sales"] == 400  # 10am-12pm, when it was sold
    assert shop.get("/api/reports/sales?range=day").json()["sales"] == 0
    people = shop.get(f"/api/reports/employees?range=day&date={monday}").json()
    assert people["sales"] == 400 and people["people"][0]["first_at"] == sold.isoformat()
    assert shop.get("/api/reports/employees?range=day").json()["sales"] == 0

    # correcting the correction: still Monday
    again = shop.post(f"/api/invoices/{new['id']}/correct", {"items": [_line(rose, 3)], "customer_id": old["customer_id"]}).json()
    assert again["date_key"] == monday and again["first_billed_at"] == sold.isoformat()


def test_demo_refresh_moves_correction_times_too(client):
    import demo_data

    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    old = _bill(shop, [_line(rose, 2)])
    new = shop.post(f"/api/invoices/{old['id']}/correct", {"items": [_line(rose, 1)], "customer_id": old["customer_id"]}).json()
    cancelled_at = shop.get(f"/api/invoices/{old['id']}").json()["cancelled_at"]
    asyncio.run(demo_data.shift_dates(db_module.state.db, old["business_id"], 2))
    moved = lambda iso: (datetime.fromisoformat(iso) + timedelta(days=2)).isoformat()  # noqa: E731
    was, now = shop.get(f"/api/invoices/{old['id']}").json(), shop.get(f"/api/invoices/{new['id']}").json()
    assert was["cancelled_at"] == moved(cancelled_at)
    assert now["first_billed_at"] == moved(new["first_billed_at"]) and now["created_at"] == moved(new["created_at"])
    assert now["correct_until"] == moved(new["correct_until"])
