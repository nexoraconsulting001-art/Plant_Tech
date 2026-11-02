"""Baseline: what server start-up used to do on every start (drafts drop, indexes, starter library, supplier links).

Copied from server.py's lifespan as it was at 9ccb713, so a database that already ran that version is
unchanged by it. An index that cannot be built (e.g. duplicates under a unique one) is logged and skipped, as
start-up did, so this never blocks a deploy; later migrations add the stricter indexes after cleaning data.
"""
import logging

from routers import library, suppliers

log = logging.getLogger("migrations")

INDEXES = {
    "users": [("id", True), ("phone", False), ("google_sub", False), ("business_id", False)],
    "businesses": [("id", True)],
    "categories": [("id", True), ("business_id", False)],
    "products": [("id", True), ("business_id", False)],
    "customers": [("id", True), ("business_id", False), ("phone", False)],
    "invoices": [("id", True), ("business_id", False), ("date_key", False), ("public_token", False)],
    "stock_history": [("product_id", False)],
    "library": [("id", True), ("name_key", False)],
    "uploads": [("path", False)],
    "ai_usage": [("business_id", False)],
    "carts": [([("business_id", 1), ("customer_id", 1)], True)],  # one saved order per customer
    "suppliers": [("id", True), ("business_id", False)],
    "purchases": [("id", False), ("business_id", False), ("supplier_id", False)],
    "push_tokens": [("token", True), ("user_id", False), ("business_id", False)],  # owners' phones
}
MARKS_KEEP_DAYS = 45  # "already sent" markers for notifications (notify.py) are dropped after this


async def up(db):
    await db.drafts.drop()  # replaced by per-customer carts (nothing if already gone)
    for coll, specs in INDEXES.items():
        for field, unique in specs:
            try:
                await db[coll].create_index(field, unique=unique)
            except Exception as e:
                log.warning("index %s.%s: %s", coll, field, e)
    try:
        await db.push_marks.create_index("at", expireAfterSeconds=MARKS_KEEP_DAYS * 86400)
    except Exception as e:
        log.warning("index push_marks.at: %s", e)
    await library.seed_library(db)
    await suppliers.backfill(db)
