"""Supplier bill count, total and last bill: counted by the database, the same numbers as before."""
import pytest

import db as db_module
from conftest import MOCK, Shop, run
from routers import suppliers

needs_mongo = pytest.mark.skipif(MOCK, reason="needs a real MongoDB")


async def _old_stats(db, business_id: str) -> dict:
    """The Python loop _stats replaced (every purchase of the shop read on every call): the reference."""
    out: dict = {}
    async for p in db.purchases.find({"business_id": business_id, "supplier_id": {"$ne": None}},
                                     {"_id": 0, "supplier_id": 1, "total": 1, "created_at": 1}):
        s = out.setdefault(p["supplier_id"], {"bills": 0, "total": 0.0, "last": None})
        s["bills"] += 1
        s["total"] += p.get("total") or 0
        s["last"] = max(s["last"] or "", p.get("created_at") or "") or None
    return out


def _rounded(stats: dict) -> dict:
    return {k: {**v, "total": round(v["total"], 6)} for k, v in stats.items()}


def _bill(shop, supplier, price, **extra):
    r = shop.post("/api/purchases", {"supplier": supplier, "invoice_number": f"B-{price}", "total": 2 * price, "items": [
        {"name": "Areca Palm", "quantity": 2, "purchase_price": price}], **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_stats_match_the_old_count(client):
    shop = Shop(client)
    other = Shop(client, phone="9811199887", name="Kamal")
    db = db_module.state.db
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    for price in (100, 150.5, 99.99):
        _bill(shop, "Green Farms", price)
    _bill(shop, "Blue Pots", 40)
    _bill(other, "Green Farms", 1000)  # another shop's bill never counts
    quiet = shop.post("/api/suppliers", {"name": "No Bills Yet"}).json()
    green = next(s for s in shop.get("/api/suppliers").json() if s["name"] == "Green Farms")
    # older rows: a bill without a total, one without a date, and one not linked to any supplier
    run(db.purchases.insert_many([
        {"id": "old-1", "business_id": bid, "supplier_id": green["id"], "total": None, "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "old-2", "business_id": bid, "supplier_id": green["id"], "total": 5},
        {"id": "old-3", "business_id": bid, "supplier_id": None, "supplier": "", "total": 7, "created_at": "2026-02-01"},
    ]))

    old = run(_old_stats(db, bid))
    assert _rounded(run(suppliers._stats(db, bid))) == _rounded(old)
    listed = {s["id"]: s for s in shop.get("/api/suppliers").json()}
    for sid, want in old.items():
        if sid in listed:
            assert (listed[sid]["bills"], listed[sid]["total"], listed[sid]["last_bill_at"]) == \
                (want["bills"], round(want["total"], 2), want["last"])
    assert (listed[green["id"]]["bills"], listed[green["id"]]["total"]) == (5, round(2 * (100 + 150.5 + 99.99) + 5, 2))
    assert (listed[quiet["id"]]["bills"], listed[quiet["id"]]["total"], listed[quiet["id"]]["last_bill_at"]) == (0, 0, None)

    # one supplier's page and an edit count only that supplier, with the same numbers
    one = shop.get(f"/api/suppliers/{green['id']}").json()
    assert (one["bills"], one["total"], one["last_bill_at"]) == (5, listed[green["id"]]["total"], listed[green["id"]]["last_bill_at"])
    assert _rounded(run(suppliers._stats(db, bid, green["id"]))) == _rounded({green["id"]: old[green["id"]]})
    edited = shop.c.patch(f"/api/suppliers/{green['id']}", headers=shop.h, json={"notes": "Pays in 30 days"}).json()
    assert (edited["bills"], edited["total"]) == (5, listed[green["id"]]["total"])
    assert run(suppliers._stats(db, bid, "nobody")) == {}


@needs_mongo
def test_one_suppliers_stats_read_only_its_bills(client):
    shop = Shop(client)
    db = db_module.state.db
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    for i in range(30):
        _bill(shop, "Green Farms" if i % 3 else "Blue Pots", 10 + i)
    blue = next(s for s in shop.get("/api/suppliers").json() if s["name"] == "Blue Pots")
    plan = run(db.command("explain", {"aggregate": "purchases", "cursor": {}, "pipeline": [
        {"$match": {"business_id": bid, "supplier_id": blue["id"]}},
        {"$group": {"_id": "$supplier_id", "bills": {"$sum": 1}, "total": {"$sum": {"$ifNull": ["$total", 0]}},
                    "last": {"$max": "$created_at"}}}]}, verbosity="executionStats"))  # as in suppliers._stats
    assert "IXSCAN" in str(plan) and "business_id_1_supplier_id_1_created_at_-1" in str(plan)
    stats = plan["stages"][0]["$cursor"]["executionStats"] if "stages" in plan else plan["executionStats"]
    assert stats["totalDocsExamined"] == 10 and stats["totalKeysExamined"] <= 11  # Blue Pots' 10 bills, nothing else
