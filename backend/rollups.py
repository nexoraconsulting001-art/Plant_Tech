"""Daily sales rollups: one small document per shop and day, kept in step with the bills, so the Orders tab's
totals, Home's today card and the sales report read a few documents instead of every bill.

    await rollups.apply(session, db, invoice, +1)   # in sales.py, inside the transaction that saves the bill
    await rollups.apply(session, db, invoice, -1)   # ... and the one that cancels it (a correction does both)
    day = await rollups.read_day(db, business_id, "2026-11-02")   # None: rollups are off, read the bills

Two collections:
- `daily_sales`, _id "<business_id>:<date_key>": orders, paise, profit, by_method.<Cash|UPI|Card>,
  by_user.<user id> (with its own by_method) and by_hour.h<00-23> (IST hour of the sale). Each part has
  {orders, paise}. Money is whole paise (bills are rounded to the paisa), so adding and taking away a bill
  always comes back to exactly 0; profit is in rupees (it comes from purchase costs, which are not rounded).
- `daily_item_sales`, _id "<business_id>:<date_key>:<item name>": lines, qty_milli (quantity x 1000), paise
  (the line amounts after the bill's discount). Names are kept exactly as on the bill, like the old report.
Both _ids sort by shop and then day, so a date range is one range scan on _id; no other index is needed.

What counts is what the reports always counted: every bill except cancelled ones, on the day of the sale
(date_key: a corrected bill keeps the first bill's day, time and maker, see sales.py). apply() looks only at
the bill's own fields, never at its status: the caller decides the sign.

Reads come from the rollups only when REPORTS_FROM_ROLLUPS is on; with it off reports.py reads the bills. Every
place that saves, cancels or corrects a bill calls apply() (sales.py, WP-1). A version without this module never
does, so its bills (the old version billing while a deploy migrates, or a rollback) are missing until the days
are rebuilt: deploy/deploy.sh does that after the swap (`python rollups.py rebuild --touched-since <day>`, the
days from <day> minus CORRECT_DAYS on) whenever such a version served bills, and keeps trying on later deploys
until it worked. By hand: `python rollups.py rebuild` (every day) or `--since <date_key>`. Anything that writes bills without apply() must rebuild the shop afterwards
(`python rollups.py rebuild --business <id>` or `await rollups.rebuild(db, id)`): demo_data.py's seed, refresh and
remove do.
"""
import inspect
import os
from datetime import date, datetime, timedelta

from pymongo import UpdateOne
from pymongo.asynchronous.database import AsyncDatabase

from common import IST
from db import in_transaction

SALES, ITEMS = "daily_sales", "daily_item_sales"
LIVE = {"status": {"$ne": "Cancelled"}}  # the same as sales.LIVE (not imported: sales.py imports this module)
METHODS = ("Cash", "UPI", "Card")
REBUILD_DAY_TIMEOUT = 60  # seconds for one shop-day's rebuild transaction (a request's transaction gets 5 s)
# The bill fields changes() reads
BILL_FIELDS = {"_id": 0, "business_id": 1, "date_key": 1, "grand_total": 1, "payment_method": 1, "created_by.id": 1,
               "created_at": 1, "first_billed_at": 1, "items.name": 1, "items.quantity": 1, "items.price": 1,
               "items.net": 1, "items.tax": 1, "items.cost_price": 1}


def enabled() -> bool:
    """Whether reports read the rollups (REPORTS_FROM_ROLLUPS=1). Read on every call, so tests can switch it."""
    return os.environ.get("REPORTS_FROM_ROLLUPS", "").strip().lower() in ("1", "true", "yes", "on")


def day_id(business_id: str, day: str) -> str:
    return f"{business_id}:{day}"


def item_id(business_id: str, day: str, name: str) -> str:
    return f"{business_id}:{day}:{name}"


def id_range(business_id: str, first: str = "", last: str = "", items: bool = False) -> dict:
    """_id filter for a shop's days first..last (empty: no limit) in daily_sales, or daily_item_sales with `items`.
    ';' is the character after ':', so "<shop>;" is past every _id of the shop."""
    lo = f"{business_id}:{first}"
    if not last:
        return {"$gte": lo, "$lt": f"{business_id};"}
    if items:
        return {"$gte": lo, "$lt": f"{business_id}:{last};"}
    return {"$gte": lo, "$lte": f"{business_id}:{last}"}


def field(value) -> str:
    """A user id or payment method as a field name: MongoDB paths cannot hold '.' or start with '$'."""
    return str(value or "?").replace(".", "_").replace("$", "_")


def _paise(x) -> int:
    return round(float(x or 0) * 100)


def _sold_hour(inv: dict) -> int:
    """IST hour of the sale; a corrected bill keeps the first bill's time (as in the day report)."""
    return datetime.fromisoformat(inv.get("first_billed_at") or inv["created_at"]).astimezone(IST).hour


def changes(inv: dict) -> tuple[dict, dict]:
    """What one bill adds: ({dotted daily_sales field: amount}, {item name: {lines, qty_milli, paise}})."""
    one = {"orders": 1, "paise": _paise(inv["grand_total"])}
    method = field(inv.get("payment_method", "Cash"))
    user = field((inv.get("created_by") or {}).get("id"))
    day = {}
    for prefix in ("", f"by_method.{method}.", f"by_user.{user}.", f"by_user.{user}.by_method.{method}.",
                   f"by_hour.h{_sold_hour(inv):02d}."):
        for k, v in one.items():
            day[prefix + k] = v
    profit = 0.0
    items: dict[str, dict] = {}
    for it in inv.get("items", []):
        net = it.get("net", it["price"] * it["quantity"])  # after the bill's discount
        row = items.setdefault(it["name"], {"lines": 0, "qty_milli": 0, "paise": 0})
        row["lines"] += 1
        row["qty_milli"] += round(it["quantity"] * 1000)
        row["paise"] += _paise(net)
        if it.get("cost_price"):  # profit only counts items with a known cost
            profit += (net - it.get("tax", 0)) - it["cost_price"] * it["quantity"]
    day["profit"] = profit
    return day, items


async def apply(session, db, invoice: dict, sign: int) -> None:
    """Add (sign=+1) or take away (sign=-1) one bill's totals. Call it in the same transaction as the bill's own
    write (pass that session), with the bill as saved. Every call writes the day's daily_sales document, so two
    transactions on the same shop and day conflict and one is retried: the totals never miss or double a bill.
    It is the busiest document of a shop, so call apply() late in the transaction."""
    if sign not in (1, -1):
        raise ValueError("sign must be +1 or -1")
    day, items = changes(invoice)
    bid, dk = invoice["business_id"], invoice["date_key"]
    await db[SALES].update_one({"_id": day_id(bid, dk)},
                               {"$inc": {k: v * sign for k, v in day.items()},
                                "$setOnInsert": {"business_id": bid, "date_key": dk}}, upsert=True, session=session)
    updates = [({"_id": item_id(bid, dk, name)}, {"$inc": {k: v * sign for k, v in row.items()},
                                                  "$setOnInsert": {"business_id": bid, "date_key": dk, "name": name}})
               for name, row in sorted(items.items())]
    if updates and isinstance(db, AsyncDatabase):  # one round trip for all the lines
        await db[ITEMS].bulk_write([UpdateOne(q, u, upsert=True) for q, u in updates], session=session)
    else:  # the in-memory test database has no bulk_write for these
        for q, u in updates:
            await db[ITEMS].update_one(q, u, upsert=True, session=session)


def _add(into: dict, inc: dict):
    for k, v in inc.items():
        into[k] = into.get(k, 0) + v


def _nest(flat: dict) -> dict:
    """{"by_method.Cash.orders": 2} -> {"by_method": {"Cash": {"orders": 2}}}"""
    out: dict = {}
    for path, v in flat.items():
        *parents, last = path.split(".")
        node = out
        for p in parents:
            node = node.setdefault(p, {})
        node[last] = v
    return out


async def from_bills(db, business_id: str, first: str, last: str, session=None) -> tuple[dict, dict]:
    """The rollups of days first..last worked out from the bills (rebuild, and the reports while rollups are off):
    ({date_key: daily_sales fields, nested}, {date_key: {item name: row}}). Streams the bills."""
    days: dict[str, dict] = {}
    items: dict[str, dict] = {}
    q = {"business_id": business_id, **LIVE, "date_key": first if first == last else {"$gte": first, "$lte": last}}
    async for inv in db.invoices.find(q, BILL_FIELDS, session=session):
        day, its = changes(inv)
        _add(days.setdefault(inv["date_key"], {}), day)
        for name, row in its.items():
            _add(items.setdefault(inv["date_key"], {}).setdefault(name, {}), row)
    return {k: _nest(v) for k, v in days.items()}, items


async def rebuild_day(db, business_id: str, day: str) -> bool:
    """Work out one shop-day again from its bills and replace its rollups. It is one transaction that reads the
    bills and writes the day's daily_sales document, so a bill saved meanwhile (its apply() writes that document
    too) is either in what was read or makes this transaction retry. Returns whether the day has bills."""
    async def run(session):
        days, items = await from_bills(db, business_id, day, day, session=session)
        await db[ITEMS].delete_many({"_id": id_range(business_id, day, day, items=True)}, session=session)
        if day not in days:
            await db[SALES].delete_one({"_id": day_id(business_id, day)}, session=session)
            return False
        await db[SALES].replace_one({"_id": day_id(business_id, day)},
                                    {"business_id": business_id, "date_key": day, **days[day]}, upsert=True, session=session)
        if rows := items.get(day):
            await db[ITEMS].insert_many([{"_id": item_id(business_id, day, name), "business_id": business_id,
                                          "date_key": day, "name": name, **row} for name, row in sorted(rows.items())],
                                        session=session)
        return True
    return await in_transaction(run, db, timeout=REBUILD_DAY_TIMEOUT)


async def aggregate(coll, pipeline: list, **kw):
    """coll.aggregate(...) as a cursor, on PyMongo's async client (a coroutine) and on the in-memory test one."""
    cursor = coll.aggregate(pipeline, **kw)
    return await cursor if inspect.isawaitable(cursor) else cursor


async def _days_since(db, business_id: str, since: str) -> set[tuple[str, str]]:
    """A shop's days from `since` on that have bills or rollups ({business_id, date_key} index, one shop at a time)."""
    pairs = set()
    async for g in await aggregate(db.invoices, [{"$match": {"business_id": business_id, "date_key": {"$gte": since}}},
                                                 {"$group": {"_id": "$date_key"}}]):
        if g["_id"]:
            pairs.add((business_id, g["_id"]))
    async for doc in db[SALES].find({"_id": id_range(business_id, since)}, {"date_key": 1}):
        pairs.add((business_id, doc["date_key"]))
    return pairs


async def rebuild(db, business_id: str | None = None, since: str = "") -> int:
    """Rebuild every shop-day that has bills or rollups (one shop, or all; with `since`, only the days from that
    date_key on). Safe to run while the app is billing, and to repeat. Returns how many days were rebuilt."""
    pairs = set()
    if since:
        shops = [business_id] if business_id else [b["id"] async for b in db.businesses.find({}, {"_id": 0, "id": 1})]
        for bid in shops:
            pairs |= await _days_since(db, bid, since)
    else:
        async for g in await aggregate(db.invoices, [{"$match": {"business_id": business_id} if business_id else {}},
                                                     {"$group": {"_id": {"b": "$business_id", "d": "$date_key"}}}]):
            if g["_id"].get("b") and g["_id"].get("d"):
                pairs.add((g["_id"]["b"], g["_id"]["d"]))
        async for doc in db[SALES].find({"_id": id_range(business_id)} if business_id else {},
                                        {"business_id": 1, "date_key": 1}):
            pairs.add((doc["business_id"], doc["date_key"]))
    for bid, day in sorted(pairs):
        await rebuild_day(db, bid, day)
    return len(pairs)


def rupees(paise: int | float) -> float:
    return round(paise / 100 + 1e-9, 2)


def totals(node: dict | None) -> dict:
    """{orders, sales, by_method} in rupees from a daily_sales document or one of its by_user entries."""
    node = node or {}
    by_method = {m: 0 for m in METHODS}
    for m, v in (node.get("by_method") or {}).items():
        by_method[m] = by_method.get(m, 0) + v.get("paise", 0)
    return {"orders": node.get("orders", 0), "sales": rupees(node.get("paise", 0)),
            "by_method": {m: rupees(v) for m, v in by_method.items()}}


async def read_day(db, business_id: str, day: str, user_id: str | None = None) -> dict | None:
    """The shop's totals for one day, {orders, sales, by_method} in rupees (only `user_id`'s bills when given),
    or None when rollups are off (read the bills instead)."""
    if not enabled():
        return None
    user = field(user_id) if user_id else None
    doc = await db[SALES].find_one({"_id": day_id(business_id, day)},
                                   {f"by_user.{user}": 1} if user else {"orders": 1, "paise": 1, "by_method": 1}) or {}
    return totals(doc.get("by_user", {}).get(user) if user else doc)


async def read_days(db, business_id: str, first: str = "", last: str = "", projection: dict | None = None):
    """The shop's daily_sales documents for days first..last (empty: no limit), oldest first."""
    async for doc in db[SALES].find({"_id": id_range(business_id, first, last)}, projection).sort("_id", 1):
        yield doc


async def read_items(db, business_id: str, first: str, last: str) -> dict[str, dict]:
    """Items sold on days first..last: {name: {lines, qty_milli, paise}}, summed over the days."""
    out: dict[str, dict] = {}
    async for doc in db[ITEMS].find({"_id": id_range(business_id, first, last, items=True)},
                                    {"_id": 0, "name": 1, "lines": 1, "qty_milli": 1, "paise": 1}):
        _add(out.setdefault(doc["name"], {}), {k: doc.get(k, 0) for k in ("lines", "qty_milli", "paise")})
    return out


def touched_since(day: str) -> str:
    """The first date_key a version without rollups may have changed from `day` (YYYY-MM-DD, India) on: its own
    bills, and the bills it cancelled or corrected, which keep their sale day for up to CORRECT_DAYS."""
    from routers.sales import CORRECT_DAYS  # not at the top: sales.py imports this module

    return (date.fromisoformat(day) - timedelta(days=CORRECT_DAYS)).isoformat()


if __name__ == "__main__":  # python rollups.py rebuild [--business <id>] [--since <day> | --touched-since <day>]
    import argparse
    import asyncio

    import pymongo

    import db as db_module
    import observability

    async def main(args) -> int:
        db_module.set_appname("hara-hisaab-rollups")
        since = touched_since(args.touched_since) if args.touched_since else args.since
        try:
            with pymongo.timeout(3600):  # lifts the app's 5 s limit per operation
                n = await rebuild(db_module.get_db(), args.business or None, since)
            print(f"rebuilt {n} shop-day(s)" + (f" from {since}" if since else ""))
            return 0
        finally:
            await db_module.close()

    parser = argparse.ArgumentParser(prog="python rollups.py")
    parser.add_argument("command", choices=["rebuild"])
    parser.add_argument("--business", default="", help="one shop's id (default: every shop)")
    days = parser.add_mutually_exclusive_group()
    days.add_argument("--since", default="", type=lambda v: date.fromisoformat(v).isoformat(),
                      help="only the days from this date_key (YYYY-MM-DD) on")
    days.add_argument("--touched-since", default="", type=lambda v: date.fromisoformat(v).isoformat(),
                      help="the days a version without rollups may have changed from this day on (deploy.sh)")
    observability.setup_logging()
    raise SystemExit(asyncio.run(main(parser.parse_args())))
