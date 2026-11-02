"""Indexes for bills and stock history (newest first per shop, customer and employee), unique bill links, retry keys.

- invoices: the Orders list and its filters, one day, a customer's bills, an employee's bills (all, and one day's).
- invoices.public_token: unique among bills that have a link. Replaces the plain index from 0001; a duplicate token
  (never expected: 128 random bits) is replaced by a new one first.
- stock_history: one product's history, newest first.
- idempotency_keys: one per shop and key (routers/sales.py), forgotten after KEY_KEEP_DAYS.
"""
import secrets

from pymongo.asynchronous.database import AsyncDatabase

KEY_KEEP_DAYS = 7  # = routers.sales.KEY_KEEP_DAYS

INVOICES = [
    [("business_id", 1), ("created_at", -1)],
    [("business_id", 1), ("date_key", 1), ("created_at", -1)],
    [("business_id", 1), ("customer_id", 1), ("created_at", -1)],
    [("business_id", 1), ("created_by.id", 1), ("created_at", -1)],
    [("business_id", 1), ("created_by.id", 1), ("date_key", 1), ("created_at", -1)],
]
HAS_TOKEN = {"public_token": {"$exists": True}}  # equality on a token implies it, so lookups can use the index


async def _unique_public_token(db):
    real = isinstance(db, AsyncDatabase)
    if real:
        dupes = await db.invoices.aggregate([
            {"$match": HAS_TOKEN}, {"$group": {"_id": "$public_token", "ids": {"$push": "$id"}}},
            {"$match": {"ids.1": {"$exists": True}}}], allowDiskUse=True)
        async for d in dupes:
            for inv_id in d["ids"][1:]:
                await db.invoices.update_one({"id": inv_id}, {"$set": {"public_token": secrets.token_urlsafe(16)}})
    old = (await db.invoices.index_information()).get("public_token_1")
    if old and old.get("unique"):
        return
    if old:
        await db.invoices.drop_index("public_token_1")
    # The in-memory dev database ignores partial filters (bills without a link would clash there): plain index.
    await db.invoices.create_index("public_token", unique=real, **({"partialFilterExpression": HAS_TOKEN} if real else {}))


async def up(db):
    for keys in INVOICES:
        await db.invoices.create_index(keys)
    await _unique_public_token(db)
    await db.stock_history.create_index([("business_id", 1), ("product_id", 1), ("created_at", -1)])
    await db.idempotency_keys.create_index([("business_id", 1), ("key", 1)], unique=True)
    await db.idempotency_keys.create_index("created_at", expireAfterSeconds=KEY_KEEP_DAYS * 86400)
