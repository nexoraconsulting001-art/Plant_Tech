"""Reports and daily rollups (WP-2): the totals, Home's today card and the sales and employees reports.

Golden tests: every report, read from the rollups and from the bills, equals the Python loops the reports
used to be (copied below as legacy_*), on a shop with cancelled, corrected, old-style and other days' bills.
`wired` is the app with REPORTS_FROM_ROLLUPS on; sales.py calls rollups.apply() inside the transactions that save,
cancel and correct a bill (WP-1).
"""
import asyncio
import importlib
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import auth
import db as db_module
import rollups
import test_bill_correction as bill_correction_tests
import test_notifications as notification_tests
import test_roles_and_tenancy as roles_tests
import test_sales as sales_tests
import test_short_stock as short_stock_tests
from common import IST, date_key, money, new_id, now_ist
from conftest import MOCK, Shop, run
from routers import sales
from routers.reports import _report_buckets
from routers.sales import LIVE, _filters, _first_billed, _scope, _sees_all, _summary
from units import count, unit_of


def _db():
    return db_module.state.db


# ---- The reports before WP-2 (routers/reports.py at d3fc28b), the reference for the golden tests ----

async def legacy_summary(user, day="", date_from="", date_to="", method="", made_by="", search=""):
    q = {**_filters(user, day, date_from, date_to, method, made_by, search), **LIVE}
    invs = await _db().invoices.find(q, {"_id": 0, "grand_total": 1, "payment_method": 1, "date_key": 1}).to_list(200000)
    by_method = {"Cash": 0.0, "UPI": 0.0, "Card": 0.0}
    by_day: dict[str, dict] = {}
    for i in invs:
        by_method[i.get("payment_method", "Cash")] = by_method.get(i.get("payment_method", "Cash"), 0) + i["grand_total"]
        d = by_day.setdefault(i["date_key"], {"orders": 0, "sales": 0.0})
        d["orders"] += 1
        d["sales"] += i["grand_total"]
    return {"orders": len(invs), "sales": money(sum(i["grand_total"] for i in invs)),
            "by_method": {k: money(v) for k, v in by_method.items()},
            "by_day": {k: {"orders": v["orders"], "sales": money(v["sales"])} for k, v in by_day.items()}}


async def legacy_today(user):
    db = _db()
    invs = await db.invoices.find({**_scope(user), **LIVE, "date_key": date_key()},
                                  {"_id": 0}).sort("created_at", -1).to_list(5000)
    by_method = {"Cash": 0.0, "UPI": 0.0, "Card": 0.0}
    for i in invs:
        by_method[i.get("payment_method", "Cash")] = by_method.get(i.get("payment_method", "Cash"), 0) + i["grand_total"]
    out = {"scope": "shop" if _sees_all(user) else "mine", "date": date_key(),
           "sales": money(sum(i["grand_total"] for i in invs)), "orders": len(invs),
           "by_method": {k: money(v) for k, v in by_method.items()}, "recent": [_summary(i) for i in invs[:20]]}
    if auth.can(user, "owner"):
        q = {"business_id": user["business_id"], "deleted_at": None, "stock": {"$lt": 0}}
        below = await db.products.find(q, {"_id": 0, "id": 1, "name": 1, "stock": 1, "unit": 1}).sort("stock", 1).to_list(20)
        out["below_zero"] = [{**p, "unit": unit_of(p.get("unit"))} for p in below]
        out["below_zero_count"] = await db.products.count_documents(q)
        rows = await db.products.find({"business_id": user["business_id"], "deleted_at": None},
                                      {"_id": 0, "id": 1, "name": 1, "stock": 1, "min_stock": 1, "unit": 1}).to_list(10000)
        low = sorted((p for p in rows if 0 < p.get("stock", 0) <= p.get("min_stock", 0)),
                     key=lambda p: (p["stock"], p["name"].lower()))
        out.update({"low_stock": [{**p, "unit": unit_of(p.get("unit"))} for p in low[:20]], "low_stock_count": len(low),
                    "out_of_stock_count": sum(1 for p in rows if p.get("stock", 0) <= 0)})
    return out


def _legacy_buckets(range_, anchor):
    keys, labels, idx = _report_buckets(range_, anchor)
    if range_ == "day":
        return keys, labels, lambda inv: idx("", datetime.fromisoformat(_first_billed(inv)).astimezone(IST).hour)
    return keys, labels, lambda inv: idx(inv["date_key"], 0)


async def legacy_sales_report(user, range_="week", on=""):
    anchor = datetime.strptime(on, "%Y-%m-%d").date() if on else now_ist().date()
    range_ = "day" if range_ == "today" else range_
    keys, labels, idx = _legacy_buckets(range_, anchor)
    invs = await _db().invoices.find({"business_id": user["business_id"], **LIVE, "date_key": {"$in": keys}},
                                     {"_id": 0}).to_list(50000)
    buckets = [0.0] * len(labels)
    by_method = {"Cash": 0.0, "UPI": 0.0, "Card": 0.0}
    sold: dict[str, dict] = {}
    profit = 0.0
    for inv in invs:
        buckets[idx(inv)] += inv["grand_total"]
        by_method[inv.get("payment_method", "Cash")] += inv["grand_total"]
        for it in inv["items"]:
            row = sold.setdefault(it["name"], {"name": it["name"], "quantity": 0.0, "amount": 0.0})
            row["quantity"] += it["quantity"]
            row["amount"] += it.get("net", it["price"] * it["quantity"])
            if it.get("cost_price"):
                profit += (it.get("net", it["price"] * it["quantity"]) - it.get("tax", 0)) - it["cost_price"] * it["quantity"]
    items = sorted(sold.values(), key=lambda r: (-r["quantity"], -r["amount"], r["name"]))
    return {"range": range_, "start": keys[0], "end": keys[-1],
            "sales": money(sum(i["grand_total"] for i in invs)), "orders": len(invs),
            "profit": money(profit), "buckets": [{"label": l, "sales": money(v)} for l, v in zip(labels, buckets)],
            "by_method": {k: money(v) for k, v in by_method.items()},
            "items": [{**r, "amount": money(r["amount"])} for r in items],
            "top": [{"name": r["name"], "quantity": r["quantity"]} for r in items[:5]]}


async def legacy_employees_report(owner, range_="day", on=""):
    db = _db()
    bid = owner["business_id"]
    anchor = datetime.strptime(on, "%Y-%m-%d").date() if on else now_ist().date()
    keys, _, _ = _report_buckets(range_, anchor)
    invs = await db.invoices.find({"business_id": bid, **LIVE, "date_key": {"$in": keys}},
                                  {"_id": 0, "grand_total": 1, "discount": 1, "created_at": 1, "first_billed_at": 1, "created_by": 1,
                                   "customer_id": 1, "items.quantity": 1, "items.unit": 1, "items.price": 1,
                                   "items.list_price": 1}).to_list(50000)
    users = await db.users.find({"business_id": bid, "role": {"$in": ["owner", "worker"]}},
                                {"_id": 0, "id": 1, "name": 1, "role": 1, "active": 1}).to_list(500)
    known = {u["id"]: u for u in users}
    people: dict[str, dict] = {}

    def person(pid):
        u = known.get(pid, {})
        return people.setdefault(pid, {
            "id": pid, "name": u.get("name", ""), "role": "owner" if u.get("role") == "owner" else "employee",
            "active": bool(u) and u.get("active", True), "orders": 0, "sales": 0.0, "items": 0.0, "discount": 0.0,
            "customers": set(), "first_at": None, "last_at": None, "price_lines": 0, "price_less": 0.0})

    for u in users:
        if u.get("active", True) or u["role"] == "owner":
            person(u["id"])
    for inv in invs:
        by = inv.get("created_by") or {}
        r = person(by.get("id", "?"))
        r["name"] = r["name"] or by.get("name", "")
        r["orders"] += 1
        r["sales"] += inv["grand_total"]
        r["discount"] += inv.get("discount", 0)
        if inv.get("customer_id"):
            r["customers"].add(inv["customer_id"])
        sold = _first_billed(inv)
        r["first_at"] = min(x for x in (r["first_at"], sold) if x)
        r["last_at"] = max(x for x in (r["last_at"], sold) if x)
        for it in inv.get("items", []):
            r["items"] += count(it)
            if it.get("list_price") is not None:
                r["price_lines"] += 1
                r["price_less"] += (it["list_price"] - it["price"]) * it["quantity"]
    total = sum(r["sales"] for r in people.values())
    rows = []
    for r in people.values():
        lines, less = r.pop("price_lines"), r.pop("price_less")
        rows.append({**r, "sales": money(r["sales"]), "discount": money(r["discount"]), "customers": len(r["customers"]),
                     "avg_bill": money(r["sales"] / r["orders"]) if r["orders"] else 0,
                     "share": round(r["sales"] / total, 4) if total else 0,
                     "price_changes": {"lines": lines, "less": money(less)}})
    rows.sort(key=lambda r: (-r["sales"], r["role"] != "owner", r["name"].lower()))
    return {"range": range_, "start": keys[0], "end": keys[-1], "sales": money(total), "orders": len(invs), "people": rows}


# ---- helpers ----

def same(new, old, path="$"):
    """Equal, with numbers equal to within 1e-6 (a sum of quantities like 0.1 + 0.2 may differ in the last bits;
    any money difference is at least 0.01). Lists in the same order, dict keys the same."""
    if isinstance(old, dict):
        assert isinstance(new, dict) and set(new) == set(old), f"{path}: keys {sorted(new)} != {sorted(old)}"
        for k in old:
            same(new[k], old[k], f"{path}.{k}")
    elif isinstance(old, list):
        assert isinstance(new, list) and len(new) == len(old), f"{path}: {len(new)} items != {len(old)}"
        for i, (a, b) in enumerate(zip(new, old)):
            same(a, b, f"{path}[{i}]")
    elif isinstance(old, (int, float)) and not isinstance(old, bool):
        assert isinstance(new, (int, float)) and abs(new - old) < 1e-6, f"{path}: {new} != {old}"
    else:
        assert new == old, f"{path}: {new!r} != {old!r}"


@pytest.fixture
def wired(client, monkeypatch):
    """The app with reports read from the rollups (sales.py calls rollups.apply() inside its transactions)."""
    monkeypatch.setenv("REPORTS_FROM_ROLLUPS", "1")
    return client


def _rollup_docs(bid):
    """The shop's rollups with zero entries left out (apply() leaves {orders: 0, paise: 0} after a cancel;
    a rebuild has no entry at all) and profit rounded to the paisa."""
    def clean(d):
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                v = clean(v)
                if v:
                    out[k] = v
            elif k == "profit":
                out[k] = round(v, 6)
            elif v or k in ("business_id", "date_key", "name", "_id"):
                out[k] = v
        return out

    async def read():
        sales_docs = [clean(d) async for d in _db()[rollups.SALES].find({"business_id": bid}).sort("_id", 1)]
        items = [clean(d) async for d in _db()[rollups.ITEMS].find({"business_id": bid}).sort("_id", 1)]
        return [d for d in sales_docs if d.get("orders")], [d for d in items if d.get("lines")]
    return run(read())


def _bill(c, headers, items, **extra):
    extra.setdefault("customer_phone", "9811100099")
    extra.setdefault("customer_name", "Anita")
    r = c.post("/api/invoices", headers=headers, json={"items": items, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _line(p, qty, price=None):
    return {"product_id": p["id"], "name": p["name"], "quantity": qty, "price": p["selling_price"] if price is None else price}


def _at(day: date, hour: int, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(timezone.utc).isoformat()


def _history(c):
    """A shop with today's bills made through the app (cash, UPI, card, a worker's, a cancel, a correction, a
    discount, a price change, kg and a loose item) and older bills written straight into the database: several
    days and hours, a cancelled one, a correction a day later, bills from before `net`/`tax`/`status` were
    saved, and one without a maker. Plus another shop's bill. Returns (shop, worker id, worker headers, days)."""
    shop = Shop(c)
    wid, wh = shop.worker()
    rose = shop.product("Rose", 100, 200, cost_price=60)
    pot = shop.product("Ceramic Pot", 118, 50, category="Pots", cost_price=50)
    soil = shop.product("Garden Soil", 40, 100, category="Pots", unit="kg", cost_price=25.5, min_stock=120)
    shop.product("Lily", 80, 2, min_stock=5)
    shop.product("Fern", 60, 0)

    a = _bill(c, shop.h, [_line(rose, 3), _line(pot, 1)], discount=20)
    _bill(c, shop.h, [_line(rose, 1, price=90)], payment_method="UPI", customer_phone="9811100022", customer_name="Bina")
    _bill(c, wh, [_line(soil, 2.5), {"product_id": None, "name": "Seeds", "quantity": 1, "price": 20}], payment_method="Card")
    w2 = _bill(c, wh, [_line(rose, 2)])
    gone = _bill(c, shop.h, [_line(pot, 2)], payment_method="UPI")
    assert shop.post(f"/api/invoices/{gone['id']}/cancel", {"reason": "Returned"}).status_code == 200
    r = shop.post(f"/api/invoices/{w2['id']}/correct", {"items": [_line(rose, 1), _line(soil, 0.7)],
                                                         "customer_id": w2["customer_id"], "payment_method": "UPI"})
    assert r.status_code == 200, r.text
    _bill(c, shop.h, [{"product_id": None, "name": "Rose", "quantity": 1, "price": 33.33}], payment_method="Card")

    bid = shop.get("/api/auth/me").json()["business"]["id"]
    template = run(_db().invoices.find_one({"id": a["id"]}, {"_id": 0}))
    today = now_ist().date()
    d1, d2, d9, d40 = (today - timedelta(days=n) for n in (1, 2, 9, 40))
    owner = template["created_by"]
    worker = {"id": wid, "name": "Ramesh"}

    def old(day, hour, by=owner, method="Cash", **extra):
        doc = {**template, "id": new_id(), "invoice_number": f"INV-9{len(older):03d}", "date_key": day.isoformat(),
               "created_at": _at(day, hour, 15), "created_by": by, "payment_method": method,
               "public_token": new_id(), **extra}
        older.append({k: v for k, v in doc.items() if v is not None})

    older: list[dict] = []
    old(d1, 6)  # before 8am: the first bucket of the day report
    old(d1, 13, worker, "UPI")
    old(d1, 22, method="Card")  # after 8pm: the last bucket
    old(d2, 10, worker, status="Cancelled")
    old(d9, 11, worker, "Card", discount=0, grand_total=354.0, subtotal=354.0,
        items=[{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100, "cost_price": 60},  # no net, tax
               {"product_id": soil["id"], "name": "Garden Soil", "quantity": 3.3, "price": 40, "unit": "kg"},
               {"product_id": None, "name": "Seeds", "quantity": 1, "price": 22}])
    old(d9, 15, status=None)  # bills from before `status` was saved count as paid
    old(d9, 17, by=None, customer_id=None)  # no maker, no customer
    # A bill sold on d40 at 9:40 and corrected the next day: it stays on d40 at 9:40, made by the worker.
    first = _at(d40, 9, 40)
    old(d40, 9, worker, status="Cancelled", created_at=first)
    old(d40, 9, worker, "UPI", created_at=_at(d40 + timedelta(days=1), 12), first_billed_at=first,
        corrected_by=owner, grand_total=200.0, items=[{**template["items"][0], "quantity": 2, "net": 200.0}])
    run(_db().invoices.insert_many(older))
    other = Shop(c, phone="9822200000", name="Other")
    _bill(c, other.h, [{"product_id": None, "name": "Rose", "quantity": 5, "price": 100}])
    run(rollups.rebuild(_db(), bid))  # the bills written straight into the database
    return shop, wid, wh, {"today": today, "d1": d1, "d2": d2, "d9": d9, "d40": d40}


def _user(c, headers):
    me = c.get("/api/auth/me", headers=headers).json()
    return run(_db().users.find_one({"id": me["id"]}, {"_id": 0}))


def _golden(c, shop, wid, wh, days):
    owner, worker = _user(c, shop.h), _user(c, wh)
    d = {k: v.isoformat() for k, v in days.items()}
    summaries = [{}, {"day": d["d1"]}, {"day": d["d40"]}, {"date_from": d["d9"], "date_to": d["d1"]},
                 {"date_from": d["d9"]}, {"date_to": d["d2"]}, {"date_from": d["d1"], "date_to": d["d9"]},
                 {"method": "UPI"}, {"method": "Card", "date_from": d["d9"]}, {"made_by": wid},
                 {"made_by": wid, "method": "UPI"}, {"made_by": "nobody"}, {"search": "INV-9"}, {"search": "bina"},
                 {"search": "98111", "method": "Cash"}]
    for who, h in ((owner, shop.h), (worker, wh)):
        for params in summaries:
            got = c.get("/api/invoices/summary", headers=h, params=params)
            assert got.status_code == 200, got.text
            same(got.json(), run(legacy_summary(who, **params)), f"summary {params}")
        same(c.get("/api/dashboard/today", headers=h).json(), run(legacy_today(who)), "today")
    for rng, on in (("day", ""), ("today", ""), ("day", d["d1"]), ("day", d["d40"]), ("week", ""), ("week", d["d9"]),
                    ("month", ""), ("month", d["d40"]), ("month", d["d9"])):
        params = {"range": rng, **({"date": on} if on else {})}
        same(shop.get("/api/reports/sales", params=params).json(), run(legacy_sales_report(owner, rng, on)), f"sales {params}")
        if rng != "today":
            same(shop.get("/api/reports/employees", params=params).json(), run(legacy_employees_report(owner, rng, on)),
                 f"employees {params}")


def test_golden_reports_from_rollups(wired):
    shop, wid, wh, days = _history(wired)
    month = shop.get("/api/reports/sales", params={"range": "month", "date": days["d40"].isoformat()}).json()
    assert month["orders"] >= 1 and shop.get("/api/invoices/summary").json()["orders"] == 12  # the fixture is what it says
    _golden(wired, shop, wid, wh, days)


def test_golden_reports_from_bills(client, monkeypatch):
    """Rollups off (the default until WP-1 calls apply()): the same answers straight from the bills."""
    monkeypatch.delenv("REPORTS_FROM_ROLLUPS", raising=False)
    shop, wid, wh, days = _history(client)
    _golden(client, shop, wid, wh, days)


def test_apply_keeps_the_rollups_equal_to_a_rebuild(wired):
    """Bills, cancels, corrections (including one that failed and was undone), applied one by one, give exactly
    the rollups a rebuild from the bills gives."""
    shop = Shop(wired)
    wid, wh = shop.worker()
    rose = shop.product("Rose", 100, 20, cost_price=61.37)
    soil = shop.product("Garden Soil", 40.5, 50, category="Pots", unit="kg", cost_price=25.25)
    lily = shop.product("Lily", 80, 1)
    a = _bill(wired, shop.h, [_line(rose, 3), _line(soil, 1.25)], discount=7.5)
    b = _bill(wired, wh, [_line(rose, 1, price=95.5)], payment_method="Card")
    _bill(wired, wh, [{"product_id": None, "name": "Seeds. Mixed", "quantity": 3, "price": 9.99}], payment_method="UPI")
    assert shop.post(f"/api/invoices/{a['id']}/cancel", {"reason": "Returned"}).status_code == 200
    r = shop.post(f"/api/invoices/{b['id']}/correct", {"items": [_line(lily, 5)], "customer_id": b["customer_id"]})
    assert r.status_code == 400 and r.json()["code"] == "out_of_stock"  # undone: the bill is back
    assert shop.post(f"/api/invoices/{b['id']}/correct", {"items": [_line(soil, 2.2)], "customer_id": b["customer_id"],
                                                          "payment_method": "UPI"}).status_code == 200
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    applied = _rollup_docs(bid)
    assert applied[0] and applied[1]
    assert run(rollups.rebuild(_db(), bid)) == 1
    assert _rollup_docs(bid) == applied
    day = applied[0][0]
    assert day["orders"] == 2 and day["by_user"][wid]["orders"] == 2 and "Card" not in day["by_method"]
    assert {i["name"] for i in applied[1]} == {"Garden Soil", "Seeds. Mixed"}  # a dot in a name is fine
    # Cancelling everything leaves exact zeros (whole paise), and the reports show nothing.
    for o in wired.get("/api/invoices", headers=shop.h).json():
        if o["status"] != "Cancelled":
            assert shop.post(f"/api/invoices/{o['id']}/cancel", {"reason": "x"}).status_code == 200
    doc = run(_db()[rollups.SALES].find_one({"business_id": bid}))
    assert doc["orders"] == 0 and doc["paise"] == 0 and all(v["paise"] == 0 for v in doc["by_method"].values())
    assert shop.get("/api/invoices/summary").json() == {"orders": 0, "sales": 0, "by_method": {"Cash": 0, "UPI": 0, "Card": 0},
                                                        "by_day": {}}
    rep = shop.get("/api/reports/sales", params={"range": "day"}).json()
    assert rep["items"] == [] and rep["top"] == [] and rep["sales"] == 0 and rep["profit"] == 0


@pytest.mark.skipif(MOCK, reason="the in-memory database has no transactions")
def test_apply_is_part_of_the_transaction(client):
    """A bill's rollup written in a transaction that fails is not written at all."""
    shop = Shop(client)
    inv = _bill(client, shop.h, [{"product_id": None, "name": "Seeds", "quantity": 2, "price": 10}])
    bill = run(_db().invoices.find_one({"id": inv["id"]}, {"_id": 0}))
    bid = bill["business_id"]
    run(rollups.rebuild(_db(), bid))

    async def broken(session):
        await rollups.apply(session, _db(), bill, +1)
        raise RuntimeError("after the rollup, before the commit")

    with pytest.raises(RuntimeError):
        run(db_module.in_transaction(broken))
    assert run(_db()[rollups.SALES].find_one({"_id": rollups.day_id(bid, bill["date_key"])}))["orders"] == 1

    async def fine(session):
        await rollups.apply(session, _db(), bill, -1)
    run(db_module.in_transaction(fine))
    assert run(_db()[rollups.SALES].find_one({"_id": rollups.day_id(bid, bill["date_key"])}))["orders"] == 0
    with pytest.raises(ValueError):
        run(rollups.apply(None, _db(), bill, 2))


def test_read_day_for_the_daily_summary(client, monkeypatch):
    shop = Shop(client)
    wid, wh = shop.worker()
    _bill(client, shop.h, [{"product_id": None, "name": "Seeds", "quantity": 2, "price": 10}], payment_method="UPI")
    _bill(client, wh, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 15.5}])
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    run(rollups.rebuild(_db(), bid))
    monkeypatch.delenv("REPORTS_FROM_ROLLUPS", raising=False)
    assert run(rollups.read_day(_db(), bid, date_key())) is None  # off: callers read the bills
    monkeypatch.setenv("REPORTS_FROM_ROLLUPS", "1")
    assert run(rollups.read_day(_db(), bid, date_key())) == {"orders": 2, "sales": 35.5,
                                                             "by_method": {"Cash": 15.5, "UPI": 20, "Card": 0}}
    assert run(rollups.read_day(_db(), bid, date_key(), wid)) == {"orders": 1, "sales": 15.5,
                                                                  "by_method": {"Cash": 15.5, "UPI": 0, "Card": 0}}
    assert run(rollups.read_day(_db(), bid, "2020-01-01"))["orders"] == 0


def test_backfill_migration_and_rebuild_are_repeatable(client):
    shop = Shop(client)
    _bill(client, shop.h, [{"product_id": None, "name": "Seeds", "quantity": 2, "price": 10}])
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    backfill = importlib.import_module("migrations.0202_rollups_backfill")
    run(backfill.up(_db()))
    first = _rollup_docs(bid)
    run(backfill.up(_db()))
    assert _rollup_docs(bid) == first and first[0][0]["orders"] == 1
    # A day whose bills are gone (e.g. demo data removed) loses its rollups on the next rebuild.
    stale = {"_id": rollups.day_id(bid, "2020-01-01"), "business_id": bid, "date_key": "2020-01-01", "orders": 3, "paise": 900}
    run(_db()[rollups.SALES].insert_one(stale))
    run(_db()[rollups.ITEMS].insert_one({"_id": rollups.item_id(bid, "2020-01-01", "Rose"), "business_id": bid,
                                         "date_key": "2020-01-01", "name": "Rose", "lines": 1}))
    assert run(rollups.rebuild(_db(), bid)) == 2
    assert _rollup_docs(bid) == first


def _stale_days(bid, days):
    """Rollups of days without bills (a rebuild removes them): quick shop-days to rebuild."""
    run(_db()[rollups.SALES].insert_many([{"_id": rollups.day_id(bid, d), "business_id": bid, "date_key": d,
                                           "orders": 1, "paise": 100} for d in days]))


def _slow_days(monkeypatch, seconds, fail=()):
    """rollups.rebuild_day taking `seconds` per shop-day (failing for the shops in `fail`); returns the days done."""
    real, done = rollups.rebuild_day, []

    async def slow(db, bid, day):
        await asyncio.sleep(seconds)
        if bid in fail:
            raise RuntimeError("rebuild_day failed")
        done.append((bid, day))
        return await real(db, bid, day)

    monkeypatch.setattr(rollups, "rebuild_day", slow)
    return done


@pytest.mark.skipif(MOCK, reason="time limits need a real MongoDB")
def test_the_backfill_has_no_limit_on_the_whole_run(client, monkeypatch):
    """Load-test fix review: a full rebuild handles every shop-day (about 20 ms each; a year of 1000 shops is
    hours), so neither migration 0202 nor `python rollups.py rebuild` may put one deadline on the whole run. Here
    the runner's limit per migration is 0.5 s and the backfill takes longer; it still finishes."""
    import migrations

    days = [f"2020-01-0{i}" for i in range(1, 5)]
    for bid in ("slow-a", "slow-b"):
        _stale_days(bid, days)
    done = _slow_days(monkeypatch, 0.2)
    monkeypatch.setattr(migrations, "STEP_TIMEOUT", 0.5)
    run(_db().migrations.delete_one({"_id": "0202_rollups_backfill"}))
    start = time.monotonic()
    assert run(migrations.up(_db())) == ["0202_rollups_backfill"]
    assert time.monotonic() - start > 0.75  # each shop's 4 days one after another, the 2 shops side by side
    assert {(b, d) for b in ("slow-a", "slow-b") for d in days} <= set(done)
    assert run(_db()[rollups.SALES].count_documents({"business_id": {"$in": ["slow-a", "slow-b"]}})) == 0


def test_a_shop_that_fails_to_rebuild_does_not_stop_the_others(client, monkeypatch, caplog):
    _stale_days("fail-a", ["2020-01-01", "2020-01-02"])
    _stale_days("fail-b", ["2020-01-01", "2020-01-02"])
    done = _slow_days(monkeypatch, 0, fail={"fail-a"})
    with pytest.raises(RuntimeError, match="1 shop.*fail-a"):
        run(rollups.rebuild(_db()))
    assert ("fail-b", "2020-01-01") in done and ("fail-b", "2020-01-02") in done
    assert run(_db()[rollups.SALES].count_documents({"business_id": "fail-b"})) == 0
    assert run(_db()[rollups.SALES].count_documents({"business_id": "fail-a"})) == 2
    assert any("python rollups.py rebuild --business fail-a" in r.getMessage() for r in caplog.records)


def test_rollup_ranges_stay_inside_the_shop_and_days():
    r = rollups.id_range("b1", "2026-11-01", "2026-11-07")
    assert r["$gte"] <= "b1:2026-11-01" and "b1:2026-11-07" <= r["$lte"] and not "b1:2026-11-08" <= r["$lte"]
    i = rollups.id_range("b1", "2026-11-01", "2026-11-07", items=True)
    assert i["$gte"] <= "b1:2026-11-07:\U0001f331 Ζ" < i["$lt"] and not "b1:2026-11-08:A" < i["$lt"]
    every = rollups.id_range("b1")
    assert every["$gte"] <= "b1:2026-11-07:zz" < every["$lt"] and not every["$gte"] <= "b10:2026"
    assert rollups.field("a.b") == "a_b" and rollups.field("$x") == "_x" and rollups.field(None) == "?"


@pytest.mark.parametrize("case", [
    sales_tests.test_reports, sales_tests.test_order_history_filters_and_pages,
    sales_tests.test_employee_report_shows_who_billed_what,
    bill_correction_tests.test_cancel_gives_stock_back_and_leaves_every_total,
    bill_correction_tests.test_correct_makes_a_linked_new_bill_and_moves_only_the_difference,
    bill_correction_tests.test_correct_failure_leaves_everything_as_it_was,
    roles_tests.test_worker_sees_only_own_orders, roles_tests.test_shops_are_isolated,
    notification_tests.test_dashboard_low_stock_card, short_stock_tests.test_owner_home_lists_products_below_zero,
], ids=lambda f: f.__name__)
def test_existing_report_tests_pass_on_rollups(wired, case):
    case(wired)


def test_low_stock_counts_in_the_database(client):
    shop = Shop(client)
    for name, stock, low in (("b plant", 3, 5), ("A plant", 3, 5), ("c", 5, 5), ("d", 6, 5), ("e", 0, 5), ("f", 2, 0)):
        shop.product(name, 10, stock, min_stock=low)
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    run(_db().products.insert_one({"id": new_id(), "business_id": bid, "name": "old", "deleted_at": None}))  # no stock
    d = shop.get("/api/dashboard/today").json()
    assert [p["name"] for p in d["low_stock"]] == ["A plant", "b plant", "c"]
    assert d["low_stock_count"] == 3 and d["out_of_stock_count"] == 2


@pytest.mark.skipif(MOCK, reason="the in-memory database has no query plans")
def test_hot_report_queries_use_indexes(client):
    """Home's recent bills and the low-stock counts are index scans without an in-memory sort."""
    shop = Shop(client)
    _bill(client, shop.h, [{"product_id": None, "name": "Seeds", "quantity": 1, "price": 10}])
    bid = shop.get("/api/auth/me").json()["business"]["id"]

    def stages(plan):
        out, todo = [], [plan["queryPlanner"]["winningPlan"]]
        while todo:
            s = todo.pop()
            s = s.get("queryPlan", s)
            out.append(s.get("stage"))
            todo += [s["inputStage"]] if "inputStage" in s else s.get("inputStages", [])
        return out

    recent = run(_db().invoices.find({"business_id": bid, **LIVE, "date_key": date_key()}).sort("created_at", -1)
                 .limit(20).explain())
    assert "IXSCAN" in stages(recent) and "SORT" not in stages(recent)
    low = run(_db().command("explain", {"count": "products", "query": {
        "business_id": bid, "deleted_at": None, "stock": {"$not": {"$gt": 0}}}}, verbosity="queryPlanner"))
    assert "IXSCAN" in stages(low) or "COUNT_SCAN" in stages(low)
    days = run(_db()[rollups.SALES].find({"_id": rollups.id_range(bid)}).sort("_id", 1).explain())
    assert "IXSCAN" in stages(days) and "SORT" not in stages(days)


@pytest.mark.skipif(MOCK, reason="timed on a real MongoDB")
def test_all_dates_summary_of_a_big_shop_is_fast(client, monkeypatch):
    """500k bills = 2,500 days of 200: the "all dates" totals 1.1.0 asks for read 2,500 small documents."""
    monkeypatch.setenv("REPORTS_FROM_ROLLUPS", "1")
    shop = Shop(client)
    wid, wh = shop.worker()
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    owner_id = shop.get("/api/auth/me").json()["id"]
    start = date(2019, 1, 1)
    docs = []
    for n in range(2500):
        day = (start + timedelta(days=n)).isoformat()
        per = {"orders": 100, "paise": 2_500_000}
        docs.append({"_id": rollups.day_id(bid, day), "business_id": bid, "date_key": day, "orders": 200, "paise": 5_000_000,
                     "profit": 12000.5, "by_method": {"Cash": per, "UPI": per},
                     "by_user": {owner_id: {**per, "by_method": {"Cash": per}}, wid: {**per, "by_method": {"UPI": per}}},
                     "by_hour": {f"h{h:02d}": {"orders": 20, "paise": 500_000} for h in range(8, 18)}})
    run(_db()[rollups.SALES].insert_many(docs))

    def timed(headers):
        took = []
        for _ in range(7):
            t = time.perf_counter()
            r = client.get("/api/invoices/summary", headers=headers)
            took.append(time.perf_counter() - t)
        return r.json(), statistics.median(took)

    out, took = timed(shop.h)
    assert out["orders"] == 500_000 and out["sales"] == 125_000_000 and len(out["by_day"]) == 2500
    assert out["by_method"] == {"Cash": 62_500_000, "UPI": 62_500_000, "Card": 0}
    assert took < 0.1, f"all-dates summary took {took * 1000:.0f} ms"
    mine, took = timed(wh)
    assert mine["orders"] == 250_000 and mine["by_method"]["UPI"] == 62_500_000 and took < 0.1


def test_rebuild_since_adds_the_bills_an_old_version_made_during_a_deploy(wired):
    """Review fix: bills the old version saved (no apply()) after migration 0202, and a bill it corrected that
    keeps an older sale day, are added by `rollups.py rebuild --touched-since <day>` after the swap. Days before
    that are not read again; other shops get their days rebuilt too."""
    shop = Shop(wired)
    rose = shop.product("Rose", 100, 50)
    _bill(wired, shop.h, [_line(rose, 1)])
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    today = now_ist().date()
    template = run(_db().invoices.find_one({"business_id": bid}, {"_id": 0}))

    def old_version_bill(day, **extra):  # saved straight into the bills, as a version without rollups does
        run(_db().invoices.insert_one({**template, "id": new_id(), "invoice_number": f"INV-8{new_id()[:3]}",
                                       "date_key": day.isoformat(), "public_token": new_id(), **extra}))

    old_version_bill(today)
    old_version_bill(today - timedelta(days=5), status="Cancelled")  # cancelled by a correction during the deploy
    d5 = (today - timedelta(days=5)).isoformat()
    run(_db()[rollups.SALES].insert_one({"_id": rollups.day_id(bid, d5), "business_id": bid, "date_key": d5,
                                         "orders": 1, "paise": 10000}))  # it was live when 0202 ran
    stale_old = (today - timedelta(days=30)).isoformat()
    run(_db()[rollups.SALES].insert_one({"_id": rollups.day_id(bid, stale_old), "business_id": bid,
                                         "date_key": stale_old, "orders": 99, "paise": 1}))
    assert shop.get("/api/invoices/summary", params={"day": today.isoformat()}).json()["orders"] == 1  # missed

    since = rollups.touched_since(today.isoformat())
    assert since == (today - timedelta(days=sales.CORRECT_DAYS)).isoformat()
    assert run(rollups.rebuild(_db(), None, since)) == 2  # today and the cancelled bill's day
    assert shop.get("/api/invoices/summary", params={"day": today.isoformat()}).json()["orders"] == 2
    assert run(_db()[rollups.SALES].find_one({"_id": rollups.day_id(bid, (today - timedelta(days=5)).isoformat())})) is None
    assert run(_db()[rollups.SALES].find_one({"_id": rollups.day_id(bid, stale_old)}))["orders"] == 99  # not read
