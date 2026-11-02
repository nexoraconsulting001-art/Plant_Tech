"""Totals and reports: the Orders tab's totals, Home's today card, and the sales and employees reports.

Moved from sales.py unchanged (WP-2 moves them onto daily rollups). This router is included before sales.router
so that /api/invoices/summary is not taken for a bill id by /api/invoices/{invoice_id}.
Cancelled bills count nowhere here (sales.LIVE). Workers see only their own bills (sales._scope).
"""
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query

import auth
from common import IST, date_key, money, now_ist
from db import get_db
from routers.sales import DATE, LIVE, _filters, _first_billed, _scope, _sees_all, _summary, date_q, method_q
from units import count, unit_of

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/invoices/summary")
async def invoices_summary(day: str = date_q(), date_from: str = date_q(), date_to: str = date_q(),
                           method: str = method_q(), made_by: str = "", search: str = Query("", max_length=60),
                           user: dict = Depends(auth.require("sell"))):
    """Totals for the same filters as the list: all of them, not just the loaded page, and per day.
    Cancelled bills are in the list (marked) but not in these totals."""
    q = {**_filters(user, day, date_from, date_to, method, made_by, search), **LIVE}
    invs = await get_db().invoices.find(q, {"_id": 0, "grand_total": 1, "payment_method": 1, "date_key": 1}).to_list(200000)
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



@router.get("/dashboard/today")
async def today(user: dict = Depends(auth.require("sell"))):
    """Home screen: today's sales and orders. Workers see only their own.
    The owner also gets `below_zero`: products sold below their stock count, most short first, and `low_stock`."""
    db = get_db()
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
        out.update(await _low_stock(db, user["business_id"]))
    return out


async def _low_stock(db, business_id: str) -> dict:
    """Home's "Low stock" card: products above zero but at or below their alert level (the Products tab's Low
    filter), lowest first, and how many are out of stock (its Out filter, which includes below zero)."""
    rows = await db.products.find({"business_id": business_id, "deleted_at": None},
                                  {"_id": 0, "id": 1, "name": 1, "stock": 1, "min_stock": 1, "unit": 1}).to_list(10000)
    low = sorted((p for p in rows if 0 < p.get("stock", 0) <= p.get("min_stock", 0)),
                 key=lambda p: (p["stock"], p["name"].lower()))
    return {"low_stock": [{**p, "unit": unit_of(p.get("unit"))} for p in low[:20]], "low_stock_count": len(low),
            "out_of_stock_count": sum(1 for p in rows if p.get("stock", 0) <= 0)}


def _report_buckets(range_: str, anchor: date) -> tuple[list[str], list[str], callable]:
    """(day keys to load, bucket labels, invoice -> bucket index) for the day, the Monday-Sunday week,
    or the calendar month that contains `anchor`."""
    if range_ == "day":
        labels = ["8am", "10am", "12pm", "2pm", "4pm", "6pm", "8pm"]

        def idx(inv):
            hour = datetime.fromisoformat(_first_billed(inv)).astimezone(IST).hour  # a corrected bill: when it was sold
            return min(max((hour - 8) // 2, 0), len(labels) - 1)
        return [anchor.isoformat()], labels, idx
    if range_ == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        days = [monday + timedelta(days=d) for d in range(7)]
        keys = [d.isoformat() for d in days]
        return keys, [f"{d.strftime('%a')} {d.day}" for d in days], lambda inv: keys.index(inv["date_key"])
    first = anchor.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    keys = [(first + timedelta(days=d)).isoformat() for d in range(last.day)]
    labels = [f"{s}–{min(s + 6, last.day)}" for s in range(1, last.day + 1, 7)]
    return keys, labels, lambda inv: (int(inv["date_key"][-2:]) - 1) // 7


@router.get("/reports/sales")
async def sales_report(range: Literal["day", "today", "week", "month"] = "week",  # noqa: A002
                       on: str = Query("", alias="date", pattern=f"^({DATE})?$"),
                       user: dict = Depends(auth.require("reports"))):
    """Sales for a day, a week (Monday to Sunday) or a month; `date` picks which one (default: today)."""
    anchor = datetime.strptime(on, "%Y-%m-%d").date() if on else now_ist().date()
    range = "day" if range == "today" else range  # noqa: A001
    keys, labels, idx = _report_buckets(range, anchor)
    invs = await get_db().invoices.find({"business_id": user["business_id"], **LIVE, "date_key": {"$in": keys}},
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
            row["amount"] += it.get("net", it["price"] * it["quantity"])  # after the bill's discount
            if it.get("cost_price"):  # profit only counts items with a known cost
                profit += (it.get("net", it["price"] * it["quantity"]) - it.get("tax", 0)) - it["cost_price"] * it["quantity"]
    items = sorted(sold.values(), key=lambda r: (-r["quantity"], -r["amount"], r["name"]))
    return {"range": range, "start": keys[0], "end": keys[-1],
            "sales": money(sum(i["grand_total"] for i in invs)), "orders": len(invs),
            "profit": money(profit), "buckets": [{"label": l, "sales": money(v)} for l, v in zip(labels, buckets)],
            "by_method": {k: money(v) for k, v in by_method.items()},
            "items": [{**r, "amount": money(r["amount"])} for r in items],  # every item sold in the period
            "top": [{"name": r["name"], "quantity": r["quantity"]} for r in items[:5]]}  # for app versions before 'items'


@router.get("/reports/employees")
async def employees_report(range: Literal["day", "week", "month"] = "day",  # noqa: A002
                           on: str = Query("", alias="date", pattern=f"^({DATE})?$"),
                           owner: dict = Depends(auth.require("owner"))):
    """Who billed what in a day, week or month: the owner and every employee (switched-off ones only if
    they billed), most sales first. `price_changes` = lines sold at a price other than the shop price."""
    db = get_db()
    bid = owner["business_id"]
    anchor = datetime.strptime(on, "%Y-%m-%d").date() if on else now_ist().date()
    keys, _, _ = _report_buckets(range, anchor)
    invs = await db.invoices.find({"business_id": bid, **LIVE, "date_key": {"$in": keys}},
                                  {"_id": 0, "grand_total": 1, "discount": 1, "created_at": 1, "first_billed_at": 1, "created_by": 1,
                                   "customer_id": 1, "items.quantity": 1, "items.unit": 1, "items.price": 1,
                                   "items.list_price": 1}).to_list(50000)
    users = await db.users.find({"business_id": bid, "role": {"$in": ["owner", "worker"]}},
                                {"_id": 0, "id": 1, "name": 1, "role": 1, "active": 1}).to_list(500)
    known = {u["id"]: u for u in users}
    people: dict[str, dict] = {}

    def person(pid: str) -> dict:
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
        sold = _first_billed(inv)  # a corrected bill counts at the time of the sale, like its day
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
    return {"range": range, "start": keys[0], "end": keys[-1], "sales": money(total), "orders": len(invs), "people": rows}
