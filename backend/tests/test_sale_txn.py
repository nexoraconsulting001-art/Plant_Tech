"""Sale integrity: one bill = one transaction, gap-free unique numbers, one customer per phone, input limits,
saved-order expiry, the customer's bill link, and the indexes behind the hot queries (WP-1)."""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError

import db as db_module
import rollups
from conftest import MOCK, TEST_DB, Shop, run
from errors import AppError
from routers import sales

needs_rs = pytest.mark.skipif(MOCK, reason="needs the MongoDB replica set (transactions)")

WATCHED = ("products", "invoices", "customers", "counters", "stock_history", "carts", "idempotency_keys", "purchases")
WRITES = ("insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many",
          "find_one_and_update")


def _bill(shop, items, headers=None, **extra):
    extra.setdefault("customer_phone", "9811100099")
    extra.setdefault("customer_name", "Anita")
    return shop.c.post("/api/invoices", headers=headers or shop.h, json={"items": items, **extra})


def _line(p, qty, price=None):
    return {"product_id": p["id"], "name": p["name"], "quantity": qty, "price": p["selling_price"] if price is None else price}


def _snapshot(mongo) -> dict:
    db = mongo[TEST_DB]
    return {c: sorted(db[c].find({}), key=lambda d: str(d["_id"])) for c in WATCHED}


class Faults:
    """Makes the n-th write inside a transaction (a write with a session) raise, as if the server died there."""

    def __init__(self, monkeypatch):
        self.fail_at, self.seen = 0, 0
        for name in WRITES:
            monkeypatch.setattr(AsyncCollection, name, self._wrap(getattr(AsyncCollection, name)))

    def _wrap(self, method):
        faults = self

        async def write(coll, *args, **kwargs):
            if kwargs.get("session") is not None:
                faults.seen += 1
                if faults.seen == faults.fail_at:
                    raise RuntimeError(f"injected failure before write {faults.seen} ({coll.name})")
            return await method(coll, *args, **kwargs)
        return write

    def every_gap(self, mongo, attempt) -> int:
        """Fails before the 1st, 2nd, ... write until the call gets through; checks nothing changed each time.
        Returns how many writes the call makes."""
        n = 0
        while True:
            n += 1
            before = _snapshot(mongo)
            self.fail_at, self.seen = n, 0
            try:
                result = attempt()
            except RuntimeError as e:
                assert "injected" in str(e)
                assert _snapshot(mongo) == before, f"a failure before write {n} left changes behind"
                continue
            self.fail_at = 0
            assert self.seen == n - 1, "the call succeeded although a write failed"
            return n - 1, result


@needs_rs
def test_a_failure_between_any_two_writes_of_a_bill_changes_nothing(client, mongo, monkeypatch):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10, min_stock=5)
    pot = shop.product("Ceramic Pot", 118, 5, category="Pots")
    first = _bill(shop, [_line(rose, 1)]).json()
    cust = shop.post("/api/customers", {"name": "Kavita", "phone": "9811155667"}).json()
    assert client.put("/api/orders/cart", headers=shop.h, json={"customer_id": cust["id"], "items": [
        {"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}]}).status_code == 200
    faults = Faults(monkeypatch)

    # A new customer, two products, the counter, the bill, totals, the saved order (another customer's) and history.
    writes, r = faults.every_gap(mongo, lambda: _bill(shop, [_line(rose, 6), _line(pot, 1)], customer_phone="9811177788",
                                                      customer_name="New Person"))
    assert r.status_code == 200 and writes >= 7
    assert r.json()["invoice_number"] == "INV-0002"  # no number was used up by the failed tries
    # An existing customer with a saved order.
    writes, r = faults.every_gap(mongo, lambda: _bill(shop, [_line(rose, 1)], customer_id=cust["id"]))
    assert r.status_code == 200 and r.json()["invoice_number"] == "INV-0003"
    assert client.get("/api/orders/cart", headers=shop.h, params={"customer_id": cust["id"]}).json() is None

    writes, r = faults.every_gap(mongo, lambda: shop.post(f"/api/invoices/{first['id']}/correct", {
        "items": [_line(rose, 2), _line(pot, 1)], "customer_id": first["customer_id"], "reason": "Two roses"}))
    assert r.status_code == 200 and r.json()["invoice_number"] == "INV-0004" and writes >= 6
    corrected = r.json()

    writes, r = faults.every_gap(mongo, lambda: shop.post(f"/api/invoices/{corrected['id']}/cancel", {"reason": "Returned"}))
    assert r.status_code == 200 and r.json()["status"] == "Cancelled" and writes >= 4

    writes, r = faults.every_gap(mongo, lambda: shop.post("/api/purchases", {
        "supplier": "Green Farms", "invoice_number": "GF-1",
        "items": [{"product_id": rose["id"], "name": "Rose", "quantity": 5, "purchase_price": 60},
                  {"name": "Snake Plant", "quantity": 3, "purchase_price": 200}]}))
    assert r.status_code == 200 and writes >= 4
    # bills 2 and 3 stand (6 + 1); bill 1 became bill 4, which was cancelled; the supplier bill added 5
    assert shop.get(f"/api/products/{rose['id']}").json()["stock"] == 10 - 6 - 1 + 5


@needs_rs
def test_stock_alerts_go_out_only_after_the_commit(client, monkeypatch):
    import notify

    seen = []

    async def alert(db, business_id, product, kind, stock):
        seen.append((product["name"], kind, stock))

    monkeypatch.setattr(notify, "stock_alert", alert)
    shop = Shop(client)
    rose = shop.product("Rose", 100, 6, min_stock=5)

    async def broken(*_a, **_k):
        raise RuntimeError("rollup failed")

    monkeypatch.setattr(rollups, "apply", broken)
    with pytest.raises(RuntimeError):
        _bill(shop, [_line(rose, 2)])
    assert seen == [] and shop.get(f"/api/products/{rose['id']}").json()["stock"] == 6
    monkeypatch.undo()
    monkeypatch.setattr(notify, "stock_alert", alert)
    assert _bill(shop, [_line(rose, 2)]).json()["invoice_number"] == "INV-0001"  # the failed bill's number came back
    assert seen == [("Rose", "low_stock", 4)]


def _owner(shop) -> dict:
    me = shop.get("/api/auth/me").json()
    return run(db_module.state.db.users.find_one({"id": me["id"]}, {"_id": 0}))


async def _sell_many(owner, product, n, phone):
    body = sales.InvoiceIn(items=[sales.LineIn(product_id=product["id"], name=product["name"], quantity=1, price=100)],
                           customer_phone=phone, customer_name="Rush Hour")
    return await asyncio.gather(*[sales.create_invoice(body=body, response=Response(), user=owner, idempotency_key=None)
                                  for _ in range(n)], return_exceptions=True)


@needs_rs
def test_fifty_phones_selling_the_last_ten(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    results = run(_sell_many(_owner(shop), rose, 50, "9811100555"))
    sold = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, AppError)]
    assert len(sold) == 10 and len(refused) == 40, [r for r in results if not isinstance(r, (dict, AppError))]
    assert all(e.code == "out_of_stock" for e in refused)
    assert shop.get(f"/api/products/{rose['id']}").json()["stock"] == 0
    assert sorted(b["invoice_number"] for b in sold) == [f"INV-{i:04d}" for i in range(1, 11)]  # no gaps, no repeats
    db = db_module.state.db
    assert run(db.invoices.count_documents({})) == 10
    customers = run(db.customers.find({"phone": "+919811100555"}, {"_id": 0}).to_list(None))
    assert len(customers) == 1 and customers[0]["purchase_count"] == 10  # one new customer, not ten
    assert len({b["customer_id"] for b in sold}) == 1
    assert run(db.stock_history.count_documents({"type": "sale"})) == 10


def test_input_limits(client):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 10)
    line = _line(rose, 1)
    assert _bill(shop, [line] * 201).status_code == 422
    assert _bill(shop, [{**line, "quantity": 1_000_001}]).status_code == 422
    assert _bill(shop, [{**line, "price": 10_000_001}]).status_code == 422
    assert _bill(shop, [line], discount=-1).status_code == 422
    for bad in ("Infinity", "NaN", "-Infinity"):  # Python's JSON parser accepts these words
        body = '{"items": [{"product_id": "%s", "name": "Rose", "quantity": %s, "price": 100}],' \
               ' "customer_phone": "9811100099", "customer_name": "A"}' % (rose["id"], bad)
        try:
            r = client.post("/api/invoices", headers={**shop.h, "Content-Type": "application/json"}, content=body)
            assert r.status_code == 422, bad
        except ValueError:  # FastAPI's 422 handler cannot write `inf` back as JSON (server.py; WP-0): still refused
            pass
    assert run(db_module.state.db.invoices.count_documents({})) == 0
    assert _bill(shop, [line] * 200).status_code == 400  # 200 lines are fine (here: not enough stock)
    cust = shop.post("/api/customers", {"name": "Kavita", "phone": "9811155667"}).json()
    r = client.put("/api/orders/cart", headers=shop.h, json={"customer_id": cust["id"], "items": [
        {"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 1e12}]})
    assert r.status_code == 422
    r = shop.post("/api/purchases", {"items": [{"name": "Rose", "quantity": 1e9, "purchase_price": 1}]})
    assert r.status_code == 422
    assert shop.get(f"/api/products/{rose['id']}").json()["stock"] == 10


def test_bill_without_gst_stays_until_the_owner_decides(client, monkeypatch):
    """Decision D6 is open: the toggle works as before; BILL_WITHOUT_GST=0 switches it off for every shop."""
    from settings import get_settings

    shop = Shop(client, gst=True)
    pot = shop.product("Pot", 118, 5, category="Pots")
    assert _bill(shop, [_line(pot, 1)], gst_removed=True).json()["gst_total"] == 0
    monkeypatch.setattr(get_settings(), "bill_without_gst", False)
    inv = _bill(shop, [_line(pot, 1)], gst_removed=True).json()
    assert inv["gst_removed"] is False and inv["gst_total"] == 18 and inv["grand_total"] == 118


def test_saved_orders_have_a_date_and_expire(client, mongo):
    shop = Shop(client)
    cust = shop.post("/api/customers", {"name": "Kavita", "phone": "9811155667"}).json()
    line = {"product_id": "loose-1", "name": "Pot", "quantity": 1, "price": 100}
    saved = client.put("/api/orders/cart", headers=shop.h, json={"customer_id": cust["id"], "items": [line]}).json()
    assert datetime.fromisoformat(saved["updated_at"]).tzinfo is not None  # the app still gets an ISO string
    got = client.get("/api/orders/cart", headers=shop.h, params={"customer_id": cust["id"]}).json()
    assert got["items"][0]["name"] == "Pot" and datetime.fromisoformat(got["updated_at"])
    if MOCK:
        return
    raw = mongo[TEST_DB].carts.find_one({})
    # Review fix: updated_at stays the ISO string the previous version parses (a rollback runs it on this data);
    # the TTL index is on saved_at, a date.
    assert isinstance(raw["saved_at"], datetime) and datetime.fromisoformat(raw["updated_at"])
    ttl = [i for i in mongo[TEST_DB].carts.list_indexes() if "expireAfterSeconds" in i]
    assert [(list(i["key"]), i["expireAfterSeconds"]) for i in ttl] == [(["saved_at"], 7 * 86400)]
    # An order saved by an older server (ISO string, no saved_at) is read meanwhile and gets saved_at.
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    mongo[TEST_DB].carts.update_one({}, {"$set": {"updated_at": old}, "$unset": {"saved_at": ""}})
    assert client.get("/api/orders/cart", headers=shop.h, params={"customer_id": cust["id"]}).json() is not None
    run(importlib.import_module("migrations.0104_carts_ttl").up(db_module.state.db))
    raw = mongo[TEST_DB].carts.find_one({})
    assert raw["updated_at"] == old and isinstance(raw["saved_at"], datetime)
    # One whose updated_at an earlier build made a date gets its ISO string back.
    mongo[TEST_DB].carts.update_one({}, {"$set": {"updated_at": datetime.now(timezone.utc)}, "$unset": {"saved_at": ""}})
    run(importlib.import_module("migrations.0104_carts_ttl").up(db_module.state.db))
    raw = mongo[TEST_DB].carts.find_one({})
    assert isinstance(raw["updated_at"], str) and isinstance(raw["saved_at"], datetime)
    assert datetime.fromisoformat(raw["updated_at"])  # what the previous version runs on it: no TypeError


def test_bill_link_headers_expiry_and_new_link(client, monkeypatch):
    shop = Shop(client)
    inv = _bill(shop, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}]).json()
    link = shop.get(f"/api/invoices/{inv['id']}/share").json()
    page = client.get(f"/i/{link['token']}")
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "no-referrer" and page.headers["cache-control"] == "private, no-store"
    assert page.headers["x-robots-tag"] == "noindex"
    missing = client.get("/i/" + "x" * 22)
    assert missing.status_code == 404 and missing.headers["cache-control"] == "private, no-store"
    assert client.get("/i/" + "x" * 200).status_code == 404

    # A new link ends the old one; stopping it ends both, and sharing again makes a fresh one.
    new = shop.post(f"/api/invoices/{inv['id']}/share/rotate").json()
    assert new["token"] != link["token"] and new["url"].endswith(new["token"])
    assert client.get(f"/i/{link['token']}").status_code == 404 and client.get(f"/i/{new['token']}").status_code == 200
    assert shop.c.delete(f"/api/invoices/{inv['id']}/share", headers=shop.h).json() == {"ok": True}
    assert client.get(f"/i/{new['token']}").status_code == 404
    again = shop.get(f"/api/invoices/{inv['id']}/share").json()
    assert again["token"] not in (link["token"], new["token"]) and client.get(f"/i/{again['token']}").status_code == 200

    # Links expire BILL_LINK_DAYS after they were made (bills from before count from the bill); sharing renews.
    run(db_module.state.db.invoices.update_one({"id": inv["id"]}, {"$set": {
        "public_token_at": (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()}}))
    assert client.get(f"/i/{again['token']}").status_code == 410
    monkeypatch.setenv("BILL_LINK_DAYS", "0")  # no limit
    assert client.get(f"/i/{again['token']}").status_code == 200
    monkeypatch.setenv("BILL_LINK_DAYS", "30")
    renewed = shop.get(f"/api/invoices/{inv['id']}/share").json()
    assert renewed["token"] != again["token"] and client.get(f"/i/{renewed['token']}").status_code == 200

    # Another shop cannot touch this bill's link.
    other = Shop(client, phone="9822299887", name="Other")
    assert other.post(f"/api/invoices/{inv['id']}/share/rotate").status_code == 404
    assert other.c.delete(f"/api/invoices/{inv['id']}/share", headers=other.h).status_code == 404


# --- migrations ---------------------------------------------------------------------------------------------
def _migration(name):
    return importlib.import_module(f"migrations.{name}")


@needs_rs
def test_duplicate_bill_numbers_are_renamed_and_the_counter_caught_up(client, mongo):
    shop = Shop(client)
    a = _bill(shop, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}]).json()
    b = _bill(shop, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 30}]).json()
    db = mongo[TEST_DB]
    db.invoices.drop_index("business_id_1_invoice_number_1")
    try:
        db.invoices.update_one({"id": b["id"]}, {"$set": {"invoice_number": a["invoice_number"]}})  # as if the counter was reset
        db.invoices.update_one({"id": a["id"]}, {"$set": {"replaced_by": {"id": b["id"], "invoice_number": "INV-0001"}}})
        db.invoices.insert_one({**{k: v for k, v in b.items() if k != "correct_until"}, "id": "x", "invoice_number": "INV-0041",
                                "public_token": "t" * 22})
        db.counters.update_one({}, {"$set": {"seq": 1}})
        run(_migration("0101_invoice_numbers").up(db_module.state.db))
    finally:
        db.invoices.create_index([("business_id", 1), ("invoice_number", 1)], unique=True)
    assert db.invoices.find_one({"id": a["id"]})["invoice_number"] == "INV-0001"
    moved = db.invoices.find_one({"id": b["id"]})
    assert (moved["invoice_number"], moved["invoice_number_was"]) == ("INV-0001-2", "INV-0001")
    assert db.invoices.find_one({"id": a["id"]})["replaced_by"]["invoice_number"] == "INV-0001-2"
    nxt = _bill(shop, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}]).json()
    assert nxt["invoice_number"] == "INV-0042"  # after the highest number in use, not after the counter


@needs_rs
def test_duplicate_customers_are_merged_into_the_oldest(client, mongo):
    shop = Shop(client)
    first = _bill(shop, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}], customer_phone="9811100444",
                  customer_name="Meena").json()
    db = mongo[TEST_DB]
    db.customers.drop_index("business_id_1_phone_1")
    try:
        keep = db.customers.find_one({"id": first["customer_id"]}, {"_id": 0})
        dup = {**keep, "id": "dup-1", "name": "Meena ji", "address": "Sector 9", "total_purchases": 50.0, "purchase_count": 2,
               "created_at": "2099-01-01T00:00:00+00:00", "last_purchase_at": "2099-01-02T00:00:00+00:00"}
        db.customers.insert_one(dup)
        db.invoices.update_one({"id": first["id"]}, {"$set": {"customer_id": "dup-1"}})
        db.carts.insert_one({"business_id": keep["business_id"], "customer_id": "dup-1", "items": [],
                             "updated_at": datetime.now(timezone.utc)})
        run(_migration("0102_customers_unique_phone").up(db_module.state.db))
    finally:
        indexes = {i["name"] for i in db.customers.list_indexes()}
        assert "business_id_1_phone_1" in indexes
    rows = list(db.customers.find({"phone": "+919811100444"}, {"_id": 0}))
    assert len(rows) == 1 and rows[0]["id"] == keep["id"] and rows[0]["name"] == "Meena"
    assert rows[0]["purchase_count"] == 3 and rows[0]["total_purchases"] == 70 and rows[0]["address"] == "Sector 9"
    assert rows[0]["last_purchase_at"] == "2099-01-02T00:00:00+00:00"
    assert db.invoices.find_one({"id": first["id"]})["customer_id"] == keep["id"]
    assert db.carts.find_one({})["customer_id"] == keep["id"]
    assert db.customers_merged.find_one({"id": "dup-1"})["merged_into"] == keep["id"]
    # A phone that is taken: the upsert at billing time reuses it, a second insert is refused by the index.
    with pytest.raises(DuplicateKeyError):
        db.customers.insert_one({**keep, "id": "dup-2"})


# --- indexes ------------------------------------------------------------------------------------------------
def _stages(plan) -> set[str]:
    out = set()
    if isinstance(plan, dict):
        if "stage" in plan:
            out.add(plan["stage"])
        for v in plan.values():
            out |= _stages(v)
    elif isinstance(plan, list):
        for v in plan:
            out |= _stages(v)
    return out


@needs_rs
def test_hot_queries_use_an_index_and_no_sort(client, mongo):
    shop = Shop(client)
    _, wh = shop.worker()
    rose = shop.product("Rose", 100, 500)
    for i in range(12):
        _bill(shop, [_line(rose, 1)], customer_phone=f"98111000{i:02d}", customer_name=f"C{i}")
        _bill(shop, [_line(rose, 1)], headers=wh, customer_phone=f"98111000{i:02d}", customer_name=f"C{i}")
    other = Shop(client, phone="9822299887", name="Other")
    _bill(other, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}])
    inv = shop.get("/api/invoices").json()[0]
    bid, uid = shop.get("/api/auth/me").json()["business"]["id"], inv["created_by"]["id"]
    worker_id = next(o["created_by"]["id"] for o in shop.get("/api/invoices").json() if o["created_by"]["id"] != uid)
    day = shop.get(f"/api/invoices/{inv['id']}").json()["date_key"]
    db = mongo[TEST_DB]
    newest = [("created_at", -1)]
    queries = [
        ("orders", db.invoices, {"business_id": bid}, newest),
        ("orders of a day", db.invoices, {"business_id": bid, "date_key": day}, newest),
        ("an employee's orders", db.invoices, {"business_id": bid, "created_by.id": worker_id}, newest),
        ("an employee's day", db.invoices, {"business_id": bid, "created_by.id": worker_id, "date_key": day}, newest),
        ("a customer's bills", db.invoices, {"business_id": bid, "customer_id": inv["id"]}, newest),
        ("a product's history", db.stock_history, {"business_id": bid, "product_id": rose["id"]}, newest),
        ("a bill", db.invoices, {"business_id": bid, "id": inv["id"]}, None),
        ("a bill link", db.invoices, {"public_token": "x" * 22}, None),
        ("a customer by phone", db.customers, {"business_id": bid, "phone": "+919811100001"}, None),
        ("a retry key", db.idempotency_keys, {"business_id": bid, "key": "k" * 36}, None),
    ]
    for name, coll, q, sort in queries:
        cur = coll.find(q)
        if sort:
            cur = cur.sort(sort).limit(50)
        stages = _stages(cur.explain()["queryPlanner"]["winningPlan"])
        assert "IXSCAN" in stages or "EXPRESS_IXSCAN" in stages or "IDHACK" in stages, (name, stages)
        assert "SORT" not in stages and "COLLSCAN" not in stages, (name, stages)
