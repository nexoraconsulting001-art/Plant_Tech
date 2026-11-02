"""One customer per phone in a shop: duplicates merged into the oldest, then a unique index {business_id, phone}.

Bills used to make a new customer after a separate "is this phone known?" read, so two phones billing the same new
number at once could make two. For each duplicate the oldest record is kept: it gets the others' bills and saved
order, their totals, the latest purchase time and any email, address or notes it lacked. The merged records are
copied to `customers_merged` (with `merged_into`) before they leave `customers`, so nothing is lost. Each phone's
merge is one transaction. Customers without a phone (none today; walk-in bills, decision D10) are not in the index.
The in-memory dev database ignores partial indexes and starts empty, so this runs only on a real MongoDB.
"""
import logging
from datetime import datetime, timezone

from pymongo.asynchronous.database import AsyncDatabase

from db import in_transaction

log = logging.getLogger("migrations")
HAS_PHONE = {"phone": {"$gt": ""}}  # strings only, not empty


async def _merge(db, bid: str, phone: str) -> int:
    async def merge(session):
        docs = await db.customers.find({"business_id": bid, "phone": phone}, {"_id": 0},
                                       session=session).sort([("created_at", 1), ("id", 1)]).to_list(None)
        if len(docs) < 2:
            return 0
        keep, others = docs[0], docs[1:]
        ids = [o["id"] for o in others]
        fill = {f: v for f in ("email", "address", "notes") if not keep.get(f)
                and (v := next((o[f] for o in others if o.get(f)), ""))}
        last = max((d["last_purchase_at"] for d in docs if d.get("last_purchase_at")), default=None)
        await db.invoices.update_many({"business_id": bid, "customer_id": {"$in": ids}},
                                      {"$set": {"customer_id": keep["id"]}}, session=session)
        carts = await db.carts.find({"business_id": bid, "customer_id": {"$in": [keep["id"], *ids]}},
                                    {"_id": 1, "customer_id": 1}, session=session).sort("updated_at", -1).to_list(None)
        if carts and not any(c["customer_id"] == keep["id"] for c in carts):  # keep the newest saved order
            await db.carts.update_one({"_id": carts[0]["_id"]}, {"$set": {"customer_id": keep["id"]}}, session=session)
            carts = carts[1:]
        if drop := [c["_id"] for c in carts if c["customer_id"] != keep["id"]]:
            await db.carts.delete_many({"_id": {"$in": drop}}, session=session)
        now = datetime.now(timezone.utc)
        for o in others:
            await db.customers_merged.replace_one({"id": o["id"]}, {**o, "merged_into": keep["id"], "merged_at": now},
                                                  upsert=True, session=session)
        await db.customers.delete_many({"business_id": bid, "id": {"$in": ids}}, session=session)
        update: dict = {"$inc": {"total_purchases": sum(o.get("total_purchases", 0) for o in others),
                                 "purchase_count": sum(o.get("purchase_count", 0) for o in others)}}
        if fill or last:
            update["$set"] = {**fill, **({"last_purchase_at": last} if last else {})}
        await db.customers.update_one({"business_id": bid, "id": keep["id"]}, update, session=session)
        return len(others)

    return await in_transaction(merge, db)


async def up(db):
    if not isinstance(db, AsyncDatabase):
        return
    merged = 0
    groups = await db.customers.aggregate([
        {"$match": HAS_PHONE},
        {"$group": {"_id": {"b": "$business_id", "p": "$phone"}, "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}}], allowDiskUse=True)
    async for g in groups:
        merged += await _merge(db, g["_id"]["b"], g["_id"]["p"])
    if merged:
        log.warning("customers: %d duplicate record(s) merged (copies in customers_merged)", merged)
    await db.customers.create_index([("business_id", 1), ("phone", 1)], unique=True, partialFilterExpression=HAS_PHONE)
