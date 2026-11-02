"""Telling the owner about the shop: stock running low, and the day's sales once a day. Owners only.

Stock: every stock change goes through `stock_changed` (from products.record_stock). It works out whether the
change crossed a line worth telling the owner about (down to the product's low-stock alert level, to zero, or
below zero) and hands that to `stock_alert`, which sends a phone notification at most once per product per day.

Daily summary: `summary_loop` (started by server.py when PUSH_ENABLED is on) checks every minute for shops
whose summary time has come today and sends "Today's sales: ₹X from N bills (Cash / UPI / Card)" plus how many
products are low. Cancelled bills don't count. A day without bills sends nothing: a closed shop needs no ping.

Both are idempotent through a marker in `push_marks` (_id = what was sent, for which shop/product and day):
inserting it is the claim, so restarts and several server workers never send twice.

The owner turns each one on or off, and picks the summary time, in More -> Notifications (`businesses.notify`).
Texts are in the language of the app on each phone (English or Hindi, kept with the phone's push token).
"""
import asyncio
import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

import push
from common import IST, date_key, money, now_ist
from invoice_html import inr
from units import short, unit_of

log = logging.getLogger(__name__)

DEFAULTS = {"low_stock": True, "summary": True, "summary_time": "21:00"}  # 9 pm India time
CHECK_EVERY = 60  # seconds between looks at whose summary is due


def settings(biz: dict | None) -> dict:
    return {**DEFAULTS, **((biz or {}).get("notify") or {})}


async def shop_settings(db, business_id: str) -> dict:
    return settings(await db.businesses.find_one({"id": business_id}, {"_id": 0, "notify": 1}))


async def claim(db, key: str) -> bool:
    """True for the first caller only (this shop/product/day was not told yet)."""
    try:
        await db.push_marks.insert_one({"_id": key, "at": datetime.now(timezone.utc)})
        return True
    except DuplicateKeyError:
        return False


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
    if not tokens or not await claim(db, f"stock:{business_id}:{product['id']}:{date_key()}"):
        return None
    await push.send_to_owners(db, business_id, lambda lang: stock_message(kind, product, stock, lang),
                              {"type": "stock", "stock": FILTER[kind], "product_id": product["id"]}, tokens)
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

async def day_summary(db, business_id: str, day: str) -> dict:
    """The day's bills (not cancelled ones), by payment type, and how many products are at or below their alert."""
    invs = await db.invoices.find({"business_id": business_id, "status": {"$ne": "Cancelled"}, "date_key": day},
                                  {"_id": 0, "grand_total": 1, "payment_method": 1}).to_list(50000)
    by_method = {"Cash": 0.0, "UPI": 0.0, "Card": 0.0}
    for i in invs:
        m = i.get("payment_method") or "Cash"
        by_method[m] = by_method.get(m, 0) + i["grand_total"]
    products = await db.products.find({"business_id": business_id, "deleted_at": None},
                                      {"_id": 0, "stock": 1, "min_stock": 1}).to_list(10000)
    low = sum(1 for p in products if p.get("stock", 0) <= p.get("min_stock", 0))
    return {"date": day, "sales": money(sum(i["grand_total"] for i in invs)), "bills": len(invs),
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


async def send_due_summaries(db, now: datetime | None = None) -> int:
    """Send today's summary to every shop whose time has come and that was not sent one today. Returns how many."""
    now = (now or now_ist()).astimezone(IST)
    day, clock = date_key(now), now.strftime("%H:%M")
    sent = 0
    for bid in await db.push_tokens.distinct("business_id"):  # only shops with an owner's phone signed up
        s = await shop_settings(db, bid)
        key = f"summary:{bid}:{day}"
        if not s["summary"] or clock < s["summary_time"] or await db.push_marks.find_one({"_id": key}):
            continue
        tokens = await push.owner_tokens(db, bid)
        if not tokens:
            continue
        summary = await day_summary(db, bid, day)
        if not await claim(db, key) or not summary["bills"]:
            continue  # sent already by another worker, or a day without bills
        await push.send_to_owners(db, bid, lambda lang, s=summary: summary_message(s, lang),
                                  {"type": "summary", "date": day}, tokens)
        sent += 1
    return sent


def test_message(lang: str) -> tuple[str, str]:
    if lang == "hi":
        return "नोटिफ़िकेशन चालू हैं", "कम स्टॉक और रोज़ की बिक्री की जानकारी इस फ़ोन पर आएगी।"
    return "Notifications are on", "Low-stock alerts and the daily sales summary will come to this phone."


async def summary_loop(get_db):
    """Runs for the life of the server process (server.py lifespan)."""
    while True:
        try:
            await send_due_summaries(get_db())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("daily summary check failed")
        await asyncio.sleep(CHECK_EVERY)
