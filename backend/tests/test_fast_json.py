"""fast_json: the busiest routes answer byte for byte what FastAPI's own path (jsonable_encoder, then JSONResponse)
answered, document by document and route by route."""
import math
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import pytest
from bson import ObjectId
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import Response

import fast_json
from conftest import Shop

IST = timezone(timedelta(hours=5, minutes=30))


def old(content) -> bytes:
    return JSONResponse(jsonable_encoder(content)).body


def new(content) -> bytes:
    return fast_json.FastJSONResponse(content).body


class Unit(str, Enum):
    PCS = "pcs"


class Line(BaseModel):
    name: str
    quantity: float


DOCS = [
    None, [], {}, "", 0, -0.0, 1.0, 100.0, 0.1 + 0.2, 1e-05, 1e16, 123456789012345678, True, False,
    {"id": "a1", "name": "गुलाब (Rose)", "notes": "line\nbreak \"quoted\" \\ tab\t   \x01 é ✓", "price": 199.0,
     "stock": -2.5, "min_stock": 5, "low_stock": True, "photo_path": None, "aliases": [], "deleted_at": None},
    {"items": [{"product_id": None, "name": "Pot", "quantity": 1.25, "price": 49.5, "gst_percent": 18,
                "net": 41.95, "tax": [3.78, 3.77], "nested": [[1, [2, [3, None]]], {"deep": {"er": [True]}}]}],
     "discount": 0, "grand_total": 1234.56, "created_at": "2026-11-03T10:00:00.123456+00:00", "next_cursor": None},
    {"naive": datetime(2026, 11, 3, 10, 0, 0), "micro": datetime(2026, 11, 3, 10, 0, 0, 123000),
     "utc": datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc), "ist": datetime(2026, 11, 3, 15, 30, tzinfo=IST),
     "day": date(2026, 11, 3), "in_list": [datetime(2026, 1, 1), {"at": datetime(2026, 1, 2, 3, 4, 5)}]},
    {"tuple": (1, "two", 3.0), "enum": Unit.PCS, "model": Line(name="Rose", quantity=2), "set": {7},
     "int_keys": {1: "a", 2: None}, "empty": {"list": [], "dict": {}, "str": ""}},
    [{"id": f"c{i}", "name": f"Customer {i}", "phone": f"+9198{i:08d}", "purchase_count": i,
      "last_purchase_at": None if i % 3 else f"2026-11-0{i % 9 + 1}T08:00:00+00:00"} for i in range(50)],
]


@pytest.mark.parametrize("doc", DOCS, ids=range(len(DOCS)))
def test_same_bytes_as_fastapi(doc):
    assert new(doc) == old(doc)


def test_what_fastapi_refuses_is_refused_too():
    for bad in ({"_id": ObjectId()}, {"x": math.nan}, {"x": math.inf}):
        with pytest.raises(ValueError):
            old(bad)
        with pytest.raises(ValueError):
            new(bad)


def test_status_and_headers_of_the_route_are_kept():
    sub = Response()
    sub.status_code = None
    sub.headers["Idempotent-Replayed"] = "true"
    r = fast_json.respond({"id": "x"}, sub)
    assert r.status_code == 200 and r.headers["idempotent-replayed"] == "true"
    assert r.headers["content-type"] == "application/json" and r.body == b'{"id":"x"}'
    sub.status_code = 201
    assert fast_json.respond(None, sub).status_code == 201 and fast_json.respond(None).body == b"null"


# ---- route by route: fast_json on and off give the same answer ---------------------------------------------------
def _both(monkeypatch, call):
    """(answer with fast_json, answer through FastAPI's own path) of the same request."""
    monkeypatch.setattr(fast_json, "ENABLED", True)
    fast = call()
    monkeypatch.setattr(fast_json, "ENABLED", False)
    slow = call()
    monkeypatch.setattr(fast_json, "ENABLED", True)
    return fast, slow


def _same(fast, slow):
    assert fast.status_code == slow.status_code
    assert fast.content == slow.content
    keep = ("content-type", "content-length", "idempotent-replayed", "content-encoding")
    assert {k: fast.headers.get(k) for k in keep} == {k: slow.headers.get(k) for k in keep}


def test_hot_routes_answer_the_same(client, monkeypatch):
    shop = Shop(client)
    rose = shop.product("Rose", 199.5, 10, notes="गुलाब, red")
    pot = shop.product("Plastic Pot", 49, 0, category="Pots")
    shop.product("Money Plant", 299, 3, botanical_name="Epipremnum aureum")
    worker_id, worker = shop.worker()
    kavita = shop.post("/api/customers", {"name": "Kavita Rao", "phone": "9811100066"}).json()
    cart = {"customer_id": kavita["id"], "discount": 10, "items": [
        {"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 180, "list_price": 199.5, "gst_percent": 0},
        {"product_id": "loose-1", "name": "Ribbon", "quantity": 1.5, "price": 20, "unit": "metre"}]}
    assert shop.c.put("/api/orders/cart", headers=shop.h, json=cart).status_code == 200
    key = {"Idempotency-Key": "bill-key-12345678"}
    bill = shop.c.post("/api/invoices", headers={**shop.h, **key}, json={
        "items": [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 199.5},
                  {"product_id": pot["id"], "name": "Plastic Pot", "quantity": 1, "price": 49}],
        "customer_id": kavita["id"], "payment_method": "UPI", "allow_short": True})
    assert bill.status_code == 200, bill.text
    inv = bill.json()

    gets = ["/api/products", "/api/products?limit=2", "/api/products?search=ro", "/api/products?search=गुलाब",
            "/api/products?search=pot&limit=5", f"/api/products?ids={rose['id']},{pot['id']}",
            "/api/products?search=zzzz", "/api/customers/find", "/api/customers/find?q=kav", "/api/customers/find?q=(",
            f"/api/orders/cart?customer_id={kavita['id']}", "/api/orders/cart?customer_id=nobody",
            f"/api/invoices/{inv['id']}", "/api/dashboard/today"]
    for url in gets:
        _same(*_both(monkeypatch, lambda: shop.get(url)))
    for url in ("/api/dashboard/today", "/api/products?search=ro", f"/api/invoices/{inv['id']}"):
        _same(*_both(monkeypatch, lambda: shop.c.get(url, headers=worker)))  # an employee's view (no cost price)

    # the bill again with its key: the stored bill and the Idempotent-Replayed header, either way
    body = {"items": [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 199.5},
                      {"product_id": pot["id"], "name": "Plastic Pot", "quantity": 1, "price": 49}],
            "customer_id": kavita["id"], "payment_method": "UPI", "allow_short": True}
    fast, slow = _both(monkeypatch, lambda: shop.c.post("/api/invoices", headers={**shop.h, **key}, json=body))
    _same(fast, slow)
    assert fast.headers["idempotent-replayed"] == "true" and fast.json()["id"] == inv["id"]

    # emptying a saved order answers null either way
    empty = {"customer_id": kavita["id"], "items": []}
    _same(*_both(monkeypatch, lambda: shop.c.put("/api/orders/cart", headers=shop.h, json=empty)))
    assert worker_id
