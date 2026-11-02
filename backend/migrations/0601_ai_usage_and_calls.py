"""AI allowances per shop, period and kind (unique); the ai_calls cost log (indexes, expiry).

ai_usage documents used to be {business_id, date, count}, one per shop and day, upserted without a unique
index, so two first scans of a day at the same moment could make two documents. They become
{business_id, period, kind: "scan", count} (period = the old date), duplicates merged by adding their counts,
and then the unique index is built. It is partial (only documents with a kind), so an older server still
writing the old shape during a deploy cannot hit it.

ai_calls: a shop's calls newest first, and expiry after AI_CALLS_KEEP_DAYS (default 180, the log retention of
decision D9). Re-running this changes nothing. Plain finds and updates only, so the dev server's in-memory
database runs it too.
"""
import os

KEEP_DAYS = int(os.environ.get("AI_CALLS_KEEP_DAYS") or 180)


async def up(db):
    kept: dict[tuple, dict] = {}  # (business_id, period) -> the document that stays
    async for doc in db.ai_usage.find({"kind": {"$exists": False}, "date": {"$exists": True}}):
        key = (doc.get("business_id"), doc["date"])
        if key not in kept:
            kept[key] = await db.ai_usage.find_one({"business_id": key[0], "period": key[1], "kind": "scan"})
        if kept[key] is None:
            await db.ai_usage.update_one({"_id": doc["_id"]}, {"$set": {"period": doc["date"], "kind": "scan"}})
            kept[key] = doc
        else:  # a second document for the same shop and day: add its count to the first, then drop it
            await db.ai_usage.update_one({"_id": kept[key]["_id"]}, {"$inc": {"count": doc.get("count", 0)}})
            await db.ai_usage.delete_one({"_id": doc["_id"]})
    await db.ai_usage.create_index([("business_id", 1), ("period", 1), ("kind", 1)], unique=True,
                                   name="business_period_kind",
                                   partialFilterExpression={"kind": {"$exists": True}})
    await db.ai_calls.create_index([("business_id", 1), ("at", -1)])
    ttl = (await db.ai_calls.index_information()).get("at_1")
    if ttl is None:
        await db.ai_calls.create_index("at", expireAfterSeconds=KEEP_DAYS * 86400)
    elif ttl.get("expireAfterSeconds") != KEEP_DAYS * 86400:  # AI_CALLS_KEEP_DAYS changed
        await db.command("collMod", "ai_calls", index={"keyPattern": {"at": 1}, "expireAfterSeconds": KEEP_DAYS * 86400})
