"""Totals and reports: the Orders tab's totals, Home's today card, and the sales and employees reports.

This router is included before sales.router so that /api/invoices/summary is not taken for a bill id by
/api/invoices/{invoice_id}. Cancelled bills count nowhere here (sales.LIVE). Workers see only their own bills
(sales._scope). The responses are what they were when these were Python loops over the bills (app 1.1.0 reads them).

Where the numbers come from (none of it loads more than a page of bills into the server):
- With REPORTS_FROM_ROLLUPS on (rollups.py): the totals, Home's today card and the sales report read the daily
  rollups, so even "all dates" costs one small document per day. The Orders totals with a search typed in
  still read the bills (a search is not in the rollups).
- With it off, and for the search: one aggregation over the bills ($match/$group), or for the sales report
  the bills streamed through rollups.changes(), so both ways count exactly the same.
- The employees report always aggregates the bills: distinct customers and first/last sale cannot be rolled up.
"""
import csv
import io
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import audit
import auth
import rollups
from common import IST, csv_text, date_key, money, now_ist
from db import get_db
from fast_json import respond
from routers.exports import _download
from routers.sales import DATE, LIVE, _filters, _scope, _sees_all, _summary, date_q, method_q
from units import DECIMAL_UNITS, unit_of

router = APIRouter(prefix="/api", tags=["reports"])

RECENT_FIELDS = {"_id": 0, "id": 1, "invoice_number": 1, "customer_name": 1, "customer_phone": 1, "grand_total": 1,
                 "payment_method": 1, "created_at": 1, "created_by": 1, "replaced_by": 1, "replaces": 1, "status": 1,
                 "items.quantity": 1, "items.unit": 1}  # what sales._summary shows


class DayTotals(BaseModel):
    orders: int
    sales: float


class SummaryOut(BaseModel):
    """The Orders tab's totals. Declared so FastAPI writes the JSON through Pydantic: "all dates" has a day per entry."""
    orders: int
    sales: float
    by_method: dict[str, float]
    by_day: dict[str, DayTotals]


class _Totals:
    """Orders and sales in all, per payment type and per day, summed in paise."""

    def __init__(self):
        self.orders, self.paise = 0, 0
        self.by_method = {m: 0 for m in rollups.METHODS}
        self.by_day: dict[str, list] = {}

    def add(self, day: str, orders: int, paise: int, by_method: dict):
        if not orders:
            return
        self.orders += orders
        self.paise += paise
        d = self.by_day.setdefault(day, [0, 0])
        d[0] += orders
        d[1] += paise
        for m, p in by_method.items():
            self.by_method[m] = self.by_method.get(m, 0) + p

    def out(self) -> dict:
        return {"orders": self.orders, "sales": rollups.rupees(self.paise),
                "by_method": {m: rollups.rupees(p) for m, p in self.by_method.items()},
                "by_day": {d: {"orders": o, "sales": rollups.rupees(p)} for d, (o, p) in sorted(self.by_day.items())}}


async def _totals_from_bills(db, q: dict) -> _Totals:
    t = _Totals()
    pipeline = [{"$match": q}, {"$group": {"_id": {"d": "$date_key", "m": {"$ifNull": ["$payment_method", "Cash"]}},
                                           "orders": {"$sum": 1}, "sales": {"$sum": "$grand_total"}}}]
    async for g in await rollups.aggregate(db.invoices, pipeline):
        paise = round(g["sales"] * 100)  # bill totals are whole paise, so their sum is too
        t.add(g["_id"]["d"], g["orders"], paise, {g["_id"]["m"]: paise})
    return t


async def _totals_from_rollups(db, business_id: str, q: dict) -> _Totals:
    """The same totals as _totals_from_bills for a filter of sales._filters (without a search)."""
    days = q.get("date_key") or {}
    first, last = (days, days) if isinstance(days, str) else (days.get("$gte", ""), days.get("$lte", ""))
    user, method = q.get("created_by.id"), q.get("payment_method")
    path = ".".join(p for p in (f"by_user.{rollups.field(user)}" if user else "",
                                f"by_method.{rollups.field(method)}" if method else "") if p)
    t = _Totals()
    if not (first and last and first > last):
        async for doc in rollups.read_days(db, business_id, first, last,
                                           {"date_key": 1, **({path: 1} if path else {"orders": 1, "paise": 1, "by_method": 1})}):
            node = doc
            for p in path.split(".") if path else ():
                node = (node or {}).get(p)
            if not node:
                continue
            by_method = {method: node.get("paise", 0)} if method else \
                {m: v.get("paise", 0) for m, v in (node.get("by_method") or {}).items()}
            t.add(doc["date_key"], node.get("orders", 0), node.get("paise", 0), by_method)
    return t


@router.get("/invoices/summary")
async def invoices_summary(day: str = date_q(), date_from: str = date_q(), date_to: str = date_q(),
                           method: str = method_q(), made_by: str = "", search: str = Query("", max_length=60),
                           user: dict = Depends(auth.require("sell"))) -> SummaryOut:
    """Totals for the same filters as the list: all of them, not just the loaded page, and per day.
    Cancelled bills are in the list (marked) but not in these totals."""
    q = {**_filters(user, day, date_from, date_to, method, made_by, search), **LIVE}
    db = get_db()
    if rollups.enabled() and "$or" not in q:  # "$or": a search, which only the bills can answer
        return (await _totals_from_rollups(db, user["business_id"], q)).out()
    return (await _totals_from_bills(db, q)).out()


@router.get("/dashboard/today")
async def today(user: dict = Depends(auth.require("sell"))):
    """Home screen: today's sales and orders. Workers see only their own.
    The owner also gets `below_zero`: products sold below their stock count, most short first, and `low_stock`."""
    db = get_db()
    day, scope = date_key(), _scope(user)
    q = {**scope, **LIVE, "date_key": day}
    totals = await rollups.read_day(db, user["business_id"], day, scope.get("created_by.id"))
    if totals is None:
        t = (await _totals_from_bills(db, q)).out()
        totals = {k: t[k] for k in ("orders", "sales", "by_method")}
    recent = await db.invoices.find(q, RECENT_FIELDS).sort("created_at", -1).limit(20).to_list(20)
    out = {"scope": "shop" if _sees_all(user) else "mine", "date": day, "sales": totals["sales"],
           "orders": totals["orders"], "by_method": totals["by_method"], "recent": [_summary(i) for i in recent]}
    if auth.can(user, "owner"):
        q = {"business_id": user["business_id"], "deleted_at": None, "stock": {"$lt": 0}}
        below = await db.products.find(q, {"_id": 0, "id": 1, "name": 1, "stock": 1, "unit": 1}).sort("stock", 1).to_list(20)
        out["below_zero"] = [{**p, "unit": unit_of(p.get("unit"))} for p in below]
        out["below_zero_count"] = await db.products.count_documents(q)
        out.update(await _low_stock(db, user["business_id"]))
    return respond(out)  # fast_json: Home asks for this after every bill


async def _low_stock(db, business_id: str) -> dict:
    """Home's "Low stock" card: products above zero but at or below their alert level (the Products tab's Low
    filter), lowest first, and how many are out of stock (its Out filter, which includes below zero).
    Counted in the database on {business_id, deleted_at, stock} (migration 0201)."""
    shop = {"business_id": business_id, "deleted_at": None}
    low_q = {**shop, "stock": {"$gt": 0}, "$expr": {"$lte": ["$stock", {"$ifNull": ["$min_stock", 0]}]}}
    pipeline = [{"$match": low_q}, {"$addFields": {"_name": {"$toLower": "$name"}}}, {"$sort": {"stock": 1, "_name": 1}},
                {"$limit": 20}, {"$project": {"_id": 0, "id": 1, "name": 1, "stock": 1, "min_stock": 1, "unit": 1}}]
    low = [p async for p in await rollups.aggregate(db.products, pipeline)]
    return {"low_stock": [{**p, "unit": unit_of(p.get("unit"))} for p in low],
            "low_stock_count": await db.products.count_documents(low_q),
            "out_of_stock_count": await db.products.count_documents({**shop, "stock": {"$not": {"$gt": 0}}})}


def _report_buckets(range_: str, anchor: date) -> tuple[list[str], list[str], callable]:
    """(day keys, bucket labels, (day key, hour of the sale) -> bucket index) for the day, the Monday-Sunday
    week, or the calendar month that contains `anchor`."""
    if range_ == "day":
        labels = ["8am", "10am", "12pm", "2pm", "4pm", "6pm", "8pm"]
        return [anchor.isoformat()], labels, lambda day, hour: min(max((hour - 8) // 2, 0), len(labels) - 1)
    if range_ == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        days = [monday + timedelta(days=d) for d in range(7)]
        keys = [d.isoformat() for d in days]
        return keys, [f"{d.strftime('%a')} {d.day}" for d in days], lambda day, hour: keys.index(day)
    first = anchor.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    keys = [(first + timedelta(days=d)).isoformat() for d in range(last.day)]
    labels = [f"{s}–{min(s + 6, last.day)}" for s in range(1, last.day + 1, 7)]
    return keys, labels, lambda day, hour: (int(day[-2:]) - 1) // 7


async def _period(db, business_id: str, first: str, last: str) -> tuple[list[dict], dict[str, dict]]:
    """(daily_sales documents, {item name: {lines, qty_milli, paise}}) for days first..last."""
    if rollups.enabled():
        days = [d async for d in rollups.read_days(db, business_id, first, last, {"by_user": 0})]
        return days, await rollups.read_items(db, business_id, first, last)
    by_day, by_day_items = await rollups.from_bills(db, business_id, first, last)
    items: dict[str, dict] = {}
    for rows in by_day_items.values():
        for name, row in rows.items():
            into = items.setdefault(name, {})
            for k, v in row.items():
                into[k] = into.get(k, 0) + v
    return [{"date_key": k, **v} for k, v in sorted(by_day.items())], items


@router.get("/reports/sales")
async def sales_report(range: Literal["day", "today", "week", "month"] = "week",  # noqa: A002
                       on: str = Query("", alias="date", pattern=f"^({DATE})?$"),
                       user: dict = Depends(auth.require("reports"))):
    """Sales for a day, a week (Monday to Sunday) or a month; `date` picks which one (default: today)."""
    anchor = datetime.strptime(on, "%Y-%m-%d").date() if on else now_ist().date()
    return await _sales(get_db(), user["business_id"], "day" if range == "today" else range, anchor)


async def _sales(db, business_id: str, range: str, anchor: date) -> dict:  # noqa: A002
    keys, labels, idx = _report_buckets(range, anchor)
    days, sold = await _period(db, business_id, keys[0], keys[-1])
    buckets = [0] * len(labels)
    by_method = {m: 0 for m in rollups.METHODS}
    orders = paise = 0
    profit = 0.0
    for d in days:
        orders += d.get("orders", 0)
        paise += d.get("paise", 0)
        profit += d.get("profit", 0)
        for m, v in (d.get("by_method") or {}).items():
            by_method[m] = by_method.get(m, 0) + v.get("paise", 0)
        if range == "day":  # by the hour of the sale
            for h, v in (d.get("by_hour") or {}).items():
                buckets[idx(d["date_key"], int(h[1:]))] += v.get("paise", 0)
        else:
            buckets[idx(d["date_key"], 0)] += d.get("paise", 0)
    items = sorted(({"name": n, "quantity": r["qty_milli"] / 1000, "amount": r["paise"]}
                    for n, r in sold.items() if r.get("lines")),
                   key=lambda r: (-r["quantity"], -r["amount"], r["name"]))
    return {"range": range, "start": keys[0], "end": keys[-1], "sales": rollups.rupees(paise), "orders": orders,
            "profit": money(profit), "buckets": [{"label": l, "sales": rollups.rupees(v)} for l, v in zip(labels, buckets)],
            "by_method": {k: rollups.rupees(v) for k, v in by_method.items()},
            "items": [{**r, "amount": rollups.rupees(r["amount"])} for r in items],  # every item sold in the period
            "top": [{"name": r["name"], "quantity": r["quantity"]} for r in items[:5]]}  # for app versions before 'items'


_MAKER = {"$ifNull": ["$created_by.id", "?"]}
_SOLD = {"$ifNull": ["$first_billed_at", "$created_at"]}  # a corrected bill counts at the time of the sale, like its day
_HAS_LIST_PRICE = {"$ne": [{"$ifNull": ["$items.list_price", None]}, None]}


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
    match = {"$match": {"business_id": bid, **LIVE, "date_key": {"$in": keys}}}
    bills = [match, {"$group": {
        "_id": _MAKER, "name": {"$first": {"$ifNull": ["$created_by.name", ""]}}, "orders": {"$sum": 1},
        "sales": {"$sum": "$grand_total"}, "discount": {"$sum": {"$ifNull": ["$discount", 0]}},
        "customers": {"$addToSet": "$customer_id"}, "first_at": {"$min": _SOLD}, "last_at": {"$max": _SOLD}}}]
    lines = [match, {"$unwind": "$items"}, {"$group": {
        "_id": _MAKER,
        # units.count: a line adds its quantity, or 1 for something sold by weight, volume or length
        "items": {"$sum": {"$cond": [{"$in": [{"$ifNull": ["$items.unit", "pcs"]}, sorted(DECIMAL_UNITS)]},
                                     1, "$items.quantity"]}},
        "price_lines": {"$sum": {"$cond": [_HAS_LIST_PRICE, 1, 0]}},
        "price_less": {"$sum": {"$cond": [_HAS_LIST_PRICE, {"$multiply": [
            {"$subtract": ["$items.list_price", "$items.price"]}, "$items.quantity"]}, 0]}}}}]
    billed = [g async for g in await rollups.aggregate(db.invoices, bills)]
    per_line = {g["_id"]: g async for g in await rollups.aggregate(db.invoices, lines)}
    users = await db.users.find({"business_id": bid, "role": {"$in": ["owner", "worker"]}},
                                {"_id": 0, "id": 1, "name": 1, "role": 1, "active": 1}).to_list(500)
    known = {u["id"]: u for u in users}
    people: dict[str, dict] = {}

    def person(pid: str) -> dict:
        u = known.get(pid, {})
        return people.setdefault(pid, {
            "id": pid, "name": u.get("name", ""), "role": "owner" if u.get("role") == "owner" else "employee",
            "active": bool(u) and u.get("active", True), "orders": 0, "sales": 0.0, "items": 0.0, "discount": 0.0,
            "customers": 0, "first_at": None, "last_at": None, "price_lines": 0, "price_less": 0.0})

    for u in users:
        if u.get("active", True) or u["role"] == "owner":
            person(u["id"])
    for g in billed:
        r = person(g["_id"])
        r["name"] = r["name"] or g["name"]
        for k in ("orders", "sales", "discount", "first_at", "last_at"):
            r[k] = g[k]
        for k in ("items", "price_lines", "price_less"):
            r[k] = per_line.get(g["_id"], {}).get(k, r[k])
        r["customers"] = sum(1 for c in g["customers"] if c)  # bills without a customer count none
    total = sum(r["sales"] for r in people.values())
    orders = sum(r["orders"] for r in people.values())
    rows = []
    for r in people.values():
        lines, less = r.pop("price_lines"), r.pop("price_less")
        rows.append({**r, "sales": money(r["sales"]), "discount": money(r["discount"]),
                     "avg_bill": money(r["sales"] / r["orders"]) if r["orders"] else 0,
                     "share": round(r["sales"] / total, 4) if total else 0,
                     "price_changes": {"lines": lines, "less": money(less)}})
    rows.sort(key=lambda r: (-r["sales"], r["role"] != "owner", r["name"].lower()))
    return {"range": range, "start": keys[0], "end": keys[-1], "sales": money(total), "orders": orders, "people": rows}


# ---- export (owner, 10 Nov 2026: "CSV and xlsx export of the data directly in the sales report section") ----

EXPORT_MAX_BILLS = 20_000
EXPORT_BILL_FIELDS = {"_id": 0, "invoice_number": 1, "date_key": 1, "created_at": 1, "status": 1, "customer_name": 1,
                      "customer_phone": 1, "payment_method": 1, "subtotal": 1, "discount": 1, "gst_total": 1,
                      "grand_total": 1, "created_by": 1}
BILL_HEADERS = ["Bill", "Date", "Time", "Customer", "Phone", "Payment", "Items total", "Discount", "GST", "Total",
                "Status", "Billed by"]
BILL_TEXT = {3, 11}  # columns typed by a person: written as text, never as a formula


def _bill_row(b: dict) -> list:
    at = b.get("created_at") or ""
    try:
        time = datetime.fromisoformat(at).astimezone(IST).strftime("%H:%M") if at else ""
    except ValueError:
        time = ""
    return [b.get("invoice_number", ""), b.get("date_key", ""), time, b.get("customer_name") or "",
            b.get("customer_phone") or "", b.get("payment_method") or "", money(b.get("subtotal") or 0),
            money(b.get("discount") or 0), money(b.get("gst_total") or 0), money(b.get("grand_total") or 0),
            b.get("status") or "Paid", (b.get("created_by") or {}).get("name", "")]


def _csv(rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(BILL_HEADERS)
    for r in rows:
        w.writerow([csv_text(v) if i in BILL_TEXT else v for i, v in enumerate(r)])
    return buf.getvalue().encode("utf-8-sig")  # a BOM, so Excel reads Hindi names


def _xlsx(shop: str, report: dict, rows: list[list]) -> bytes:
    """Summary, Bills and Items sheets. Text goes in with write_string (a name starting with = stays text)."""
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    bold, rupees = wb.add_format({"bold": True}), wb.add_format({"num_format": "#,##0.00"})

    def put(ws, r, c, v, fmt=None):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ws.write_number(r, c, v, fmt or rupees)
        else:
            ws.write_string(r, c, "" if v is None else str(v), fmt)

    summary = wb.add_worksheet("Summary")
    summary.set_column(0, 0, 22), summary.set_column(1, 1, 16)
    lines = [("Shop", shop), ("From", report["start"]), ("To", report["end"]), ("Orders", report["orders"]),
             ("Sales", report["sales"]), ("Estimated profit", report["profit"]),
             *((f"Paid by {m}", v) for m, v in report["by_method"].items())]
    for r, (k, v) in enumerate(lines):
        put(summary, r, 0, k, bold)
        put(summary, r, 1, v, None if k == "Orders" else rupees)
    bills = wb.add_worksheet("Bills")
    for c, h in enumerate(BILL_HEADERS):
        put(bills, 0, c, h, bold)
    for r, row in enumerate(rows, 1):
        for c, v in enumerate(row):
            put(bills, r, c, v)
    bills.set_column(0, 2, 12), bills.set_column(3, 3, 22), bills.set_column(4, 5, 14), bills.set_column(6, 9, 12)
    bills.set_column(10, 11, 14), bills.freeze_panes(1, 0)
    items = wb.add_worksheet("Items")
    for c, h in enumerate(["Item", "Quantity", "Amount"]):
        put(items, 0, c, h, bold)
    for r, it in enumerate(report["items"], 1):
        put(items, r, 0, it["name"])
        items.write_number(r, 1, it["quantity"])
        put(items, r, 2, it["amount"])
    items.set_column(0, 0, 28), items.set_column(1, 2, 12), items.freeze_panes(1, 0)
    wb.close()
    return buf.getvalue()


@router.get("/reports/sales/export")
async def sales_export(range: Literal["day", "week", "month"] = "day",  # noqa: A002
                       on: str = Query("", alias="date", pattern=f"^({DATE})?$"),
                       format: Literal["csv", "xlsx"] = "xlsx",  # noqa: A002
                       owner: dict = Depends(auth.require("owner"))):
    """The sales report of a day, week or month as a file: Excel (Summary, Bills, Items) or CSV (the bills, one row
    each, cancelled ones marked). Owner only, like the other exports (customers' phone numbers); audit-logged."""
    db = get_db()
    bid = owner["business_id"]
    anchor = datetime.strptime(on, "%Y-%m-%d").date() if on else now_ist().date()
    report = await _sales(db, bid, range, anchor)
    bills = await db.invoices.find({"business_id": bid, "date_key": {"$gte": report["start"], "$lte": report["end"]}},
                                   EXPORT_BILL_FIELDS).sort("created_at", 1).to_list(EXPORT_MAX_BILLS)
    rows = [_bill_row(b) for b in bills]
    await audit.log(db, owner, "export.sales", {"type": "period", "id": f"{range}:{report['start']}"},
                    {"format": format, "bills": len(rows)})
    name = f"Sales_{report['start']}" + ("" if report["start"] == report["end"] else f"_to_{report['end']}")
    if format == "csv":
        return _download(_csv(rows), f"{name}.csv", "text/csv; charset=utf-8")
    shop = (await db.businesses.find_one({"id": bid}, {"_id": 0, "name": 1}) or {}).get("name", "")
    return _download(_xlsx(shop, report, rows), f"{name}.xlsx",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

