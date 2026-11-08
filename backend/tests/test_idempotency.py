"""Idempotency-Key on POST /invoices and /correct: a retried request returns the first bill, never a second one."""
import asyncio
import uuid

import pytest
from fastapi import Response

import db as db_module
from conftest import MOCK, Shop, run
from routers import purchases, sales

needs_rs = pytest.mark.skipif(MOCK, reason="needs the MongoDB replica set (transactions)")


def _bill(shop, items, key=None, **extra):
    extra.setdefault("customer_phone", "9811100099")
    extra.setdefault("customer_name", "Anita")
    headers = {**shop.h, **({"Idempotency-Key": key} if key else {})}
    return shop.c.post("/api/invoices", headers=headers, json={"items": items, **extra})


def _line(p, qty):
    return {"product_id": p["id"], "name": p["name"], "quantity": qty, "price": p["selling_price"]}


def _count(coll="invoices", **q):
    return run(db_module.state.db[coll].count_documents(q))


def test_same_key_twice_makes_one_bill(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    key = str(uuid.uuid4())
    first = _bill(shop, [_line(rose, 2)], key=key)
    again = _bill(shop, [_line(rose, 2)], key=key)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() and again.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert _count() == 1 and shop.get(f"/api/products/{rose['id']}").json()["stock"] == 8
    assert shop.get("/api/customers").json()[0]["purchase_count"] == 1
    # A new key is a new bill; no key at all (app 1.1.0) works as it always did.
    assert _bill(shop, [_line(rose, 1)], key=str(uuid.uuid4())).json()["invoice_number"] == "INV-0002"
    assert _bill(shop, [_line(rose, 1)]).json()["invoice_number"] == "INV-0003"
    assert _bill(shop, [_line(rose, 1)]).json()["invoice_number"] == "INV-0004"


def test_a_refused_bill_does_not_use_up_its_key(client):
    """The app keeps one key for the attempt, including the owner's "Bill anyway" resend after a stock warning."""
    shop = Shop(client)
    rose = shop.product("Rose", 100, 1)
    key = str(uuid.uuid4())
    r = _bill(shop, [_line(rose, 3)], key=key)
    assert r.status_code == 400 and r.json()["code"] == "out_of_stock"
    r = _bill(shop, [_line(rose, 3)], key=key, allow_short=True)
    assert r.status_code == 200 and r.json()["items"][0]["short_qty"] == 2
    assert _bill(shop, [_line(rose, 3)], key=key, allow_short=True).json()["id"] == r.json()["id"]
    assert _count() == 1


def test_keys_belong_to_one_shop_and_one_kind_of_request(client):
    shop = Shop(client)
    other = Shop(client, phone="9822299887", name="Other")
    key = str(uuid.uuid4())
    seeds = [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}]
    a = _bill(shop, seeds, key=key).json()
    b = _bill(other, seeds, key=key).json()
    assert a["id"] != b["id"] and _count() == 2  # the same key in another shop is another bill
    # A key that made a bill cannot be used to correct one.
    r = shop.c.post(f"/api/invoices/{a['id']}/correct", headers={**shop.h, "Idempotency-Key": key},
                    json={"items": seeds, "customer_id": a["customer_id"]})
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"
    for bad in ("short", "has space in it", "x" * 101):
        assert _bill(shop, seeds, key=bad).status_code == 422, bad


def test_correction_retried_with_its_key(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    old = _bill(shop, [_line(rose, 3)]).json()
    key = str(uuid.uuid4())
    body = {"items": [_line(rose, 2)], "customer_id": old["customer_id"], "reason": "One less"}
    first = shop.c.post(f"/api/invoices/{old['id']}/correct", headers={**shop.h, "Idempotency-Key": key}, json=body)
    again = shop.c.post(f"/api/invoices/{old['id']}/correct", headers={**shop.h, "Idempotency-Key": key}, json=body)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"] and again.headers["idempotent-replayed"] == "true"
    assert _count() == 2 and shop.get(f"/api/products/{rose['id']}").json()["stock"] == 8
    # Without the key the old bill is already cancelled, as before.
    r = shop.post(f"/api/invoices/{old['id']}/correct", body)
    assert r.status_code == 400 and r.json()["code"] == "bill_cancelled"


@needs_rs
def test_the_same_key_sent_at_the_same_moment(client):
    """A phone that retries while the first request is still running: both get the one bill."""
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    me = shop.get("/api/auth/me").json()
    owner = run(db_module.state.db.users.find_one({"id": me["id"]}, {"_id": 0}))
    body = sales.InvoiceIn(items=[sales.LineIn(**_line(rose, 1))], customer_phone="9811100099", customer_name="Anita")
    key = str(uuid.uuid4())

    async def send():
        return await asyncio.gather(*[sales.create_invoice(body=body, response=Response(), user=owner,
                                                           idempotency_key=key) for _ in range(8)],
                                    return_exceptions=True)

    results = run(send())
    assert all(isinstance(r, dict) for r in results), results
    assert len({r["id"] for r in results}) == 1
    assert _count() == 1 and _count("idempotency_keys") == 1
    assert shop.get(f"/api/products/{rose['id']}").json()["stock"] == 9


@needs_rs
def test_keys_are_forgotten_after_a_week(client, mongo):
    from conftest import TEST_DB

    ttl = [i for i in mongo[TEST_DB].idempotency_keys.list_indexes() if "expireAfterSeconds" in i]
    assert [(list(i["key"]), i["expireAfterSeconds"]) for i in ttl] == [(["created_at"], sales.KEY_KEEP_DAYS * 86400)]
    unique = [i for i in mongo[TEST_DB].idempotency_keys.list_indexes() if i.get("unique")]
    assert [list(i["key"]) for i in unique] == [["business_id", "key"]]


def _owner(shop):
    me = shop.get("/api/auth/me").json()
    return run(db_module.state.db.users.find_one({"id": me["id"]}, {"_id": 0}))


@needs_rs
def test_the_same_key_at_once_on_the_last_plant_gets_the_bill_not_out_of_stock(client):
    """Review fix: the key is claimed first in the transaction, so a duplicate that arrives while the first is
    still running replays the bill instead of failing on the stock the first one took."""
    shop = Shop(client)
    rose = shop.product("Rose", 100, 1)
    owner = _owner(shop)
    body = sales.InvoiceIn(items=[sales.LineIn(**_line(rose, 1))], customer_phone="9811100099", customer_name="Anita")
    key = str(uuid.uuid4())

    async def send():
        return await asyncio.gather(*[sales.create_invoice(body=body, response=Response(), user=owner,
                                                           idempotency_key=key) for _ in range(6)],
                                    return_exceptions=True)

    results = run(send())
    assert all(isinstance(r, dict) for r in results), results
    assert {r["invoice_number"] for r in results} == {"INV-0001"}
    assert _count() == 1 and shop.get(f"/api/products/{rose['id']}").json()["stock"] == 0


@needs_rs
def test_the_same_correction_at_once_gets_the_corrected_bill_not_bill_cancelled(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 3)
    old = _bill(shop, [_line(rose, 2)]).json()
    owner = _owner(shop)
    body = sales.CorrectIn(items=[sales.LineIn(**_line(rose, 3))], customer_id=old["customer_id"], reason="One more")
    key = str(uuid.uuid4())

    async def send():
        return await asyncio.gather(*[sales.correct_invoice(invoice_id=old["id"], body=body, response=Response(),
                                                            owner=owner, idempotency_key=key) for _ in range(6)],
                                    return_exceptions=True)

    results = run(send())
    assert all(isinstance(r, dict) for r in results), results
    assert {r["invoice_number"] for r in results} == {"INV-0002"}
    assert _count() == 2 and shop.get(f"/api/products/{rose['id']}").json()["stock"] == 0


def test_the_same_key_with_a_changed_order_is_refused_and_names_the_bill(client):
    """Review fix: the first try was billed but its answer lost; the worker then added a pot or switched to UPI.
    The same key must not quietly answer with the old bill as if it were the changed order."""
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    pot = shop.product("Pot", 50, 10)
    key = str(uuid.uuid4())
    first = _bill(shop, [_line(rose, 1)], key=key).json()
    for changed in ({"items": [_line(rose, 1), _line(pot, 1)]}, {"items": [_line(rose, 1)], "payment_method": "UPI"},
                    {"items": [_line(rose, 1)], "discount": 10}):
        r = _bill(shop, changed.pop("items"), key=key, **changed)
        assert r.status_code == 409 and r.json()["code"] == "idempotency_mismatch", r.text
        assert r.json()["params"] == {"number": first["invoice_number"], "invoice_id": first["id"]}
    assert _count() == 1 and shop.get(f"/api/products/{pot['id']}").json()["stock"] == 10
    # The same order again still replays; "Bill anyway" (allow_short) and the UPI ID are not part of the order.
    assert _bill(shop, [_line(rose, 1)], key=key, allow_short=True).json()["id"] == first["id"]
    # A key saved before fingerprints (no fingerprint) replays as before.
    run(db_module.state.db.idempotency_keys.update_one({"key": key}, {"$unset": {"fingerprint": ""}}))
    assert _bill(shop, [_line(rose, 1), _line(pot, 1)], key=key).json()["id"] == first["id"]


# ---- supplier bills (POST /purchases): the same key never adds the stock twice (review fix)

def _supplier_bill(shop, items, key=None, **extra):
    headers = {**shop.h, **({"Idempotency-Key": key} if key else {})}
    return shop.c.post("/api/purchases", headers=headers,
                       json={"supplier": "Green Farms", "invoice_number": "GF-1", "items": items, **extra})


def _stock(shop, p):
    return shop.get(f"/api/products/{p['id']}").json()["stock"]


def test_a_supplier_bill_sent_twice_adds_its_stock_once(client):
    """The worker taps Save, the answer is lost on a slow network, and they tap Save again with the same key."""
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    items = [{"product_id": rose["id"], "name": "Rose", "quantity": 5, "purchase_price": 60},
             {"name": "Areca Palm", "quantity": 2, "purchase_price": 100}]
    key = str(uuid.uuid4())
    first = _supplier_bill(shop, items, key)
    again = _supplier_bill(shop, items, key)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json() == first.json() and again.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert _count("purchases") == 1 and _stock(shop, rose) == 15
    assert [p["name"] for p in shop.get("/api/products").json()].count("Areca Palm") == 1
    # Changed items with the same key: refused, naming the supplier bill that was saved.
    r = _supplier_bill(shop, [{**items[0], "quantity": 6}], key)
    assert r.status_code == 409 and r.json()["code"] == "idempotency_mismatch", r.text
    assert r.json()["params"] == {"purchase_id": first.json()["id"]}
    # A new key is a new supplier bill; no key at all (app 1.1.0) works as it always did.
    assert _supplier_bill(shop, items, str(uuid.uuid4())).status_code == 200
    assert _supplier_bill(shop, items).status_code == 200
    assert _count("purchases") == 3 and _stock(shop, rose) == 25


def test_a_refused_supplier_bill_does_not_use_up_its_key(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    key = str(uuid.uuid4())
    gone = [{"product_id": "no-such-product", "name": "Lily", "quantity": 1, "purchase_price": 10}]
    r = _supplier_bill(shop, gone, key)
    assert r.status_code == 400 and r.json()["code"] == "product_missing"
    r = _supplier_bill(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "purchase_price": 60}], key)
    assert r.status_code == 200, r.text
    assert _count("purchases") == 1 and _stock(shop, rose) == 11


def test_a_bill_key_is_not_a_supplier_bill_key(client):
    shop = Shop(client)
    key = str(uuid.uuid4())
    seeds = [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}]
    assert _bill(shop, seeds, key=key).status_code == 200
    r = _supplier_bill(shop, [{"name": "Areca Palm", "quantity": 1, "purchase_price": 100}], key)
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"
    assert _count("purchases") == 0


@needs_rs
def test_the_same_supplier_bill_at_the_same_moment(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    owner = _owner(shop)
    body = purchases.PurchaseIn(supplier="Green Farms", items=[
        purchases.PurchaseItem(product_id=rose["id"], name="Rose", quantity=3, purchase_price=60),
        purchases.PurchaseItem(name="Areca Palm", quantity=1, purchase_price=100)])
    key = str(uuid.uuid4())

    async def send():
        return await asyncio.gather(*[purchases.create_purchase(body=body, response=Response(), user=owner,
                                                                idempotency_key=key) for _ in range(6)],
                                    return_exceptions=True)

    results = run(send())
    assert all(isinstance(r, dict) for r in results), results
    assert len({r["id"] for r in results}) == 1
    assert _count("purchases") == 1 and _stock(shop, rose) == 13
    assert [p["name"] for p in shop.get("/api/products").json()].count("Areca Palm") == 1
