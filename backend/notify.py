"""Telling the owner about the shop: stock running low, and the day's sales once a day. Owners only.

Stock: every stock change goes through `stock_changed` (from stock.record_stock). It works out whether the
change crossed a line worth telling the owner about (down to the product's low-stock alert level, to zero, or
below zero) and hands that to `stock_alert`, which sends a phone notification at most once per product per day.

Daily summary: "Today's sales: ₹X from N bills (Cash / UPI / Card)" plus how many products are low. The worker
(worker.py) calls `send_due_summaries` once a minute; the web processes never do.
- Schedule: each shop with the summary on and an owner's phone signed up has one row in `summary_schedule`
  (_id = the shop, `due` = when its next summary goes, `day` = the day it is for). routers/notifications.py
  writes it when a phone signs up or the settings change; each run moves it on to the next day. One indexed
  query finds the shops that are due; they are done in batches, CONCURRENCY at a time.
- Spread: a shop that never picked a time gets its summary between 21:00 and 21:19 India time, at a minute
  fixed by its id (`summary_clock`), so 1000 shops are not all done in the same minute. A time the owner
  picked is kept to the minute.
- Claimed before it is worked out: the `push_marks` marker goes in first, so overlapping runs or a second
  worker never send twice. A marker left "claimed" by a run that died is taken over after STALE_CLAIM, so the
  summary still goes. A summary is only sent on its own day: a worker that was down at 9 pm does not send
  yesterday's the next morning.
- Totals come from rollups.read_day; while that has nothing, from one aggregation over the day's bills.
  Cancelled bills don't count. A day without bills sends nothing: a closed shop needs no ping.

Both kinds are idempotent through a marker in `push_marks` (_id = what was sent, for which shop/product and day;
`state` "claimed", then "sent"): inserting it is the claim, so restarts and several processes never send twice.
The sending itself is a queued job (push.py) that survives restarts.

The owner turns each one on or off, and picks the summary time, in More -> Notifications (`businesses.notify`).
Texts are in the language of the app on each phone (English or Hindi, kept with the phone's push token).
"""
import asyncio
import inspect
import logging
import zlib
from datetime import date, datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

import push
import rollups
from common import IST, date_key, money, now_ist
from invoice_html import inr
from units import short, unit_of

log = logging.getLogger(__name__)

DEFAULTS = {"low_stock": True, "summary": True, "summary_time": "21:00"}  # 9 pm India time
SPREAD_MINUTES = 20  # shops on the default time get theirs 21:00-21:19, by shop id
STALE_CLAIM = timedelta(minutes=10)  # longer than the worker's tick timeout (worker.py): its holder is gone by then
CONCURRENCY = 10  # shops worked on at the same time
BATCH = 100  # shops per batch
TICK_LIMIT = 5000  # due shops taken per run; any more wait for the next minute


def settings(biz: dict | None) -> dict:
    return {**DEFAULTS, **((biz or {}).get("notify") or {})}


async def shop_settings(db, business_id: str) -> dict:
    return settings(await db.businesses.find_one({"id": business_id}, {"_id": 0, "notify": 1}))


async def claim(db, key: str) -> bool:
    """True for the first caller only (this shop/product/day was not told yet), or for the one that takes over
    a claim whose holder died before it finished."""
    now = datetime.now(timezone.utc)
    try:
        await db.push_marks.insert_one({"_id": key, "at": now, "state": "claimed"})
        return True
    except DuplicateKeyError:
        taken = await db.push_marks.find_one_and_update(
            {"_id": key, "state": "claimed", "at": {"$lt": now - STALE_CLAIM}}, {"$set": {"at": now}})
        return taken is not None


async def mark_sent(db, key: str):
    await db.push_marks.update_one({"_id": key}, {"$set": {"state": "sent"}})


def _qty(n: float, unit: str | None) -> str:
    return f"{round(float(n), 3):g} {short(unit_of(unit))}"


# ---- stock

def crossed(before: float, after: float, min_stock: float) -> str | None:
    """Which line this change crossed going down, if any: below zero, then zero, then the alert level."""
    if after < 0 <= before:
        return "below_zero"
    if after <= 0 < before:
        return "out_of_stock"
    if after <= min_stock < before:
        return "low_stock"
    return None


STOCK_TEXT = {  # kind -> language -> (title, body)
    "low_stock": {"en": ("Low stock: {name}", "Only {qty} left. Your alert is at {min}."),
                  "hi": ("स्टॉक कम: {name}", "सिर्फ़ {qty} बचे हैं। अलर्ट {min} पर है।")},
    "out_of_stock": {"en": ("Out of stock: {name}", "{name} has run out. Add stock when it comes in."),
                     "hi": ("स्टॉक ख़त्म: {name}", "{name} का स्टॉक ख़त्म हो गया है। नया स्टॉक आने पर जोड़ें।")},
    "below_zero": {"en": ("Sold below stock: {name}", "Stock is now {qty}. Count it and update the stock."),
                   "hi": ("स्टॉक से ज़्यादा बिका: {name}", "स्टॉक अब {qty} है। गिनकर स्टॉक ठीक करें।")},
}
FILTER = {"low_stock": "low", "out_of_stock": "out", "below_zero": "below"}  # the Products tab filter to open


def stock_message(kind: str, product: dict, stock: float, lang: str) -> tuple[str, str]:
    title, body = STOCK_TEXT[kind].get(lang) or STOCK_TEXT[kind]["en"]
    fill = {"name": product["name"], "qty": _qty(stock, product.get("unit")),
            "min": _qty(product.get("min_stock", 0) or 0, product.get("unit"))}
    return title.format(**fill), body.format(**fill)


async def stock_alert(db, business_id: str, product: dict, kind: str, stock: float):
    """kind: "low_stock", "out_of_stock" or "below_zero". Sends to the owner's phones, once per product a day."""
    if not (await shop_settings(db, business_id))["low_stock"]:
        return None
    tokens = await push.owner_tokens(db, business_id)
    key = f"stock:{business_id}:{product['id']}:{date_key()}"
    if not tokens or not await claim(db, key):
        return None
    try:
        await push.send_to_owners(db, business_id, lambda lang: stock_message(kind, product, stock, lang),
                                  {"type": "stock", "stock": FILTER[kind], "product_id": product["id"]}, tokens, key=key)
    except Exception:
        await db.push_marks.delete_one({"_id": key, "state": "claimed"})  # not queued: the next change tries again
        raise
    await mark_sent(db, key)
    return kind


async def stock_changed(db, business_id: str, product: dict, before: float, after: float):
    kind = crossed(before, after, product.get("min_stock", 0) or 0)
    if not kind:
        return None
    try:
        await stock_alert(db, business_id, product, kind, after)
    except Exception:  # an alert must never fail a sale or a stock change
        log.exception("stock alert failed for product %s", product.get("id"))
    return kind


# ---- daily summary

async def _bill_totals(db, business_id: str, day: str) -> dict:
    """The day's bills (not cancelled ones) by payment type, added up by MongoDB (no bills loaded here)."""
    method = {"$let": {"vars": {"m": {"$ifNull": ["$payment_method", ""]}},
                       "in": {"$cond": [{"$eq": ["$$m", ""]}, "Cash", "$$m"]}}}
    cursor = db.invoices.aggregate([
        {"$match": {"business_id": business_id, "date_key": day, "status": {"$ne": "Cancelled"}}},
        {"$group": {"_id": method, "sales": {"$sum": "$grand_total"}, "bills": {"$sum": 1}}},
    ])
    if inspect.isawaitable(cursor):  # PyMongo's async client (the in-memory test database returns the cursor)
        cursor = await cursor
    rows = await cursor.to_list(None)
    return {"bills": sum(r["bills"] for r in rows), "sales": sum(r["sales"] for r in rows),
            "by_method": {r["_id"]: r["sales"] for r in rows}}


async def day_summary(db, business_id: str, day: str) -> dict:
    """The day's bills (not cancelled ones), by payment type, and how many products are at or below their alert."""
    totals = await rollups.read_day(db, business_id, day)
    if totals is None:
        totals = await _bill_totals(db, business_id, day)
    by_method = {"Cash": 0.0, "UPI": 0.0, "Card": 0.0}
    for m, v in (totals.get("by_method") or {}).items():
        by_method[m] = by_method.get(m, 0) + ((v.get("sales") or 0) if isinstance(v, dict) else (v or 0))
    low = await db.products.count_documents({
        "business_id": business_id, "deleted_at": None,
        "$expr": {"$lte": [{"$ifNull": ["$stock", 0]}, {"$ifNull": ["$min_stock", 0]}]}})
    return {"date": day, "sales": money(totals.get("sales") or 0),
            "bills": int(totals.get("orders", totals.get("bills")) or 0),
            "by_method": {k: money(v) for k, v in by_method.items()}, "low_stock": low}


METHOD_HI = {"Cash": "नकद", "UPI": "UPI", "Card": "कार्ड"}


def summary_message(s: dict, lang: str) -> tuple[str, str]:
    """English: "₹12,450 from 18 bills (Cash ₹6,000 / UPI ₹6,450). 3 products are low on stock." """
    hi = lang == "hi"
    parts = " / ".join(f"{METHOD_HI.get(m, m) if hi else m} {inr(v)}" for m, v in s["by_method"].items() if v)
    n, low = s["bills"], s["low_stock"]
    if hi:
        body = f"{n} बिल से {inr(s['sales'])}" + (f" ({parts})" if parts else "") + "।"
        if low:
            body += f" {low} प्रोडक्ट का स्टॉक कम है।"
        return "आज की बिक्री", body
    body = f"{inr(s['sales'])} from {n} bill{'' if n == 1 else 's'}" + (f" ({parts})" if parts else "") + "."
    if low:
        body += " 1 product is low on stock." if low == 1 else f" {low} products are low on stock."
    return "Today's sales", body


def summary_clock(business_id: str, notify_settings: dict | None) -> str:
    """When the shop's summary goes, "HH:MM" India time: the time the owner picked, else 21:00 plus 0-19
    minutes fixed by the shop id (crc32, the same in every process and after restarts)."""
    picked = (notify_settings or {}).get("summary_time")
    if picked:
        return picked
    hour, minute = map(int, DEFAULTS["summary_time"].split(":"))
    minute += zlib.crc32(business_id.encode()) % SPREAD_MINUTES
    return f"{hour + minute // 60:02d}:{minute % 60:02d}"


def summary_due(business_id: str, notify_settings: dict | None, day: str) -> datetime:
    """The moment (UTC) the summary for `day` ("2026-11-02") is due."""
    hour, minute = map(int, summary_clock(business_id, notify_settings).split(":"))
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


async def schedule(db, business_id: str, *, keep: bool = False, now: datetime | None = None) -> dict | None:
    """Put today's summary for the shop in `summary_schedule` (summary on), or take the shop out (off).
    keep=True leaves a row that is already there as it is (a phone signing up again)."""
    biz = await db.businesses.find_one({"id": business_id}, {"_id": 0, "notify": 1})
    if biz is None or not settings(biz)["summary"]:
        await db.summary_schedule.delete_one({"_id": business_id})
        return None
    day = date_key(now)
    row = {"due": summary_due(business_id, biz.get("notify"), day), "day": day}
    await db.summary_schedule.update_one({"_id": business_id}, {"$setOnInsert" if keep else "$set": row}, upsert=True)
    return row


async def _move(db, row: dict, biz: dict, day: str):
    """On to `day`'s summary, unless the row changed meanwhile (the owner picked another time)."""
    await db.summary_schedule.update_one(
        {"_id": row["_id"], "due": row["due"]},
        {"$set": {"due": summary_due(row["_id"], biz.get("notify"), day), "day": day}})


async def _summary_for(db, row: dict, biz: dict | None, now: datetime) -> bool:
    """One due shop: claim, work out, queue the push, move on to tomorrow. True when a summary was sent."""
    bid, day = row["_id"], row.get("day")
    if biz is None or not settings(biz)["summary"]:
        await db.summary_schedule.delete_one({"_id": bid, "due": row["due"]})
        return False
    today = date_key(now)
    if day != today:  # missed (the worker was down then): not sent late; today's goes at its time
        await _move(db, row, biz, today)
        return False
    tokens = await push.owner_tokens(db, bid)
    if not tokens:  # no owner's phone any more; signing one up puts the shop back
        await db.summary_schedule.delete_one({"_id": bid, "due": row["due"]})
        return False
    key = f"summary:{bid}:{day}"
    if not await claim(db, key):
        mark = await db.push_marks.find_one({"_id": key}, {"state": 1})
        if not mark or mark.get("state") != "claimed":  # sent already
            await _move(db, row, biz, _next_day(day))
        return False  # otherwise another run is on it right now, and moves it on
    summary = await day_summary(db, bid, day)
    if summary["bills"]:
        await push.send_to_owners(db, bid, lambda lang: summary_message(summary, lang),
                                  {"type": "summary", "date": day}, tokens, key=key)
    await mark_sent(db, key)
    await _move(db, row, biz, _next_day(day))
    return bool(summary["bills"])


async def send_due_summaries(db, now: datetime | None = None) -> int:
    """Send the summary to every shop whose time has come (worker.py, once a minute). Returns how many were sent."""
    now = (now or now_ist()).astimezone(timezone.utc)
    due = await db.summary_schedule.find({"due": {"$lte": now}}).sort("due", 1).to_list(TICK_LIMIT)
    if not due:
        return 0
    ids = [r["_id"] for r in due]
    shops = {b["id"]: b for b in await db.businesses.find({"id": {"$in": ids}}, {"_id": 0, "id": 1, "notify": 1})
             .to_list(len(ids))}
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict) -> bool:
        async with gate:
            try:
                return await _summary_for(db, row, shops.get(row["_id"]), now)
            except Exception:  # one shop's failure must not stop the others; its claim is retried once stale
                log.exception("daily summary for shop %s failed", row["_id"])
                return False

    sent = 0
    for i in range(0, len(due), BATCH):
        sent += sum(await asyncio.gather(*(one(r) for r in due[i:i + BATCH])))
    return sent


def test_message(lang: str) -> tuple[str, str]:
    if lang == "hi":
        return "नोटिफ़िकेशन चालू हैं", "कम स्टॉक और रोज़ की बिक्री की जानकारी इस फ़ोन पर आएगी।"
    return "Notifications are on", "Low-stock alerts and the daily sales summary will come to this phone."
