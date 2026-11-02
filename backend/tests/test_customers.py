"""Customers: editing one, and the customer's address on their bills."""
import asyncio

import db as db_module
from conftest import Shop


def _bill(shop, customer_id):
    r = shop.post("/api/invoices", {"items": [{"product_id": None, "name": "Rose", "quantity": 1, "price": 100}],
                                    "customer_id": customer_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_edit_customer(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()
    shop.post("/api/customers", {"name": "Rajesh", "phone": "9811155667"})
    r = client.patch(f"/api/customers/{c['id']}", headers=shop.h,
                     json={"name": " Meena Gupta ", "phone": "+91 98111 00077", "address": "12 Mall Road\nSector 5"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["phone"], r.json()["address"]) == ("Meena Gupta", "+919811100077", "12 Mall Road\nSector 5")
    got = shop.get(f"/api/customers/{c['id']}").json()
    assert (got["name"], got["phone"], got["address"]) == ("Meena Gupta", "+919811100077", "12 Mall Road\nSector 5")

    # Only the fields sent change; clearing a field is allowed.
    r = client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"address": ""})
    assert r.json()["address"] == "" and r.json()["name"] == "Meena Gupta"
    # Saving its own number again is fine; another customer's number is not.
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "9811100077"}).status_code == 200
    r = client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "98111 55667"})
    assert r.status_code == 409
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "12345"}).status_code == 400
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"name": "  "}).status_code == 400
    assert client.patch("/api/customers/nope", headers=shop.h, json={"name": "X"}).status_code == 404


def test_edit_customer_is_per_shop_and_needs_customers_permission(client):
    shop = Shop(client)
    other = Shop(client, phone="9811199887", name="Kamal")
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066"}).json()
    # The same number in another shop is not a clash, and another shop can't edit this customer.
    other.post("/api/customers", {"name": "Someone", "phone": "9811100088"})
    assert client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"phone": "9811100088"}).status_code == 200
    assert client.patch(f"/api/customers/{c['id']}", headers=other.h, json={"name": "Hacked"}).status_code == 404
    assert shop.get(f"/api/customers/{c['id']}").json()["name"] == "Meena"

    _, seller = shop.worker(customers=False)
    assert client.patch(f"/api/customers/{c['id']}", headers=seller, json={"name": "X"}).status_code == 403
    _, helper = shop.worker(phone="9822233446", sell=False)
    assert client.patch(f"/api/customers/{c['id']}", headers=helper, json={"name": "Meena G"}).status_code == 200


def test_bill_keeps_the_customer_address_of_the_day(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066", "address": "12 Mall Road <b>"}).json()
    inv = _bill(shop, c["id"])
    assert inv["customer_address"] == "12 Mall Road <b>"
    got = shop.get(f"/api/invoices/{inv['id']}").json()
    assert got["shop"] == {"name": "Suresh's Nursery", "address": "Sector 5", "phone": "9811122334"}
    page = shop.get(f"/api/invoices/{inv['id']}/html").text
    assert "Bill to" in page and "12 Mall Road &lt;b&gt;" in page and "Sector 5" in page

    # Editing the customer later leaves the bill as it was.
    client.patch(f"/api/customers/{c['id']}", headers=shop.h, json={"name": "Meena Gupta", "address": "New Colony"})
    got = shop.get(f"/api/invoices/{inv['id']}").json()
    assert (got["customer_name"], got["customer_address"]) == ("Meena", "12 Mall Road <b>")
    url = shop.get(f"/api/invoices/{inv['id']}/share").json()["url"]
    public = client.get(url.replace("https://pos.test", "")).text
    assert "12 Mall Road &lt;b&gt;" in public and "New Colony" not in public
    assert _bill(shop, c["id"])["customer_address"] == "New Colony"


def test_old_bill_without_address_still_renders(client):
    shop = Shop(client)
    c = shop.post("/api/customers", {"name": "Meena", "phone": "9811100066", "address": "12 Mall Road"}).json()
    inv = _bill(shop, c["id"])
    db = db_module.state.db  # a bill made before addresses were kept has no such field
    asyncio.run(db.invoices.update_one({"id": inv["id"]}, {"$unset": {"customer_address": ""}}))
    assert "customer_address" not in shop.get(f"/api/invoices/{inv['id']}").json()
    page = shop.get(f"/api/invoices/{inv['id']}/html").text
    assert "Meena" in page and "12 Mall Road" not in page and 'class="m addr"' not in page
