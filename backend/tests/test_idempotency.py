"""Idempotency-Key on POST /invoices and /correct: a retried request returns the first bill, never a second one."""
import asyncio
import uuid

import pytest
from fastapi import Response

import db as db_module
from conftest import MOCK, Shop, run
from routers import sales

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
