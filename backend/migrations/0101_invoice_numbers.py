"""Bill numbers unique per shop: duplicate numbers renamed, counters moved past the highest number, then the index.

The counter has always been atomic, so duplicates should not exist; a counter reset by hand (or a restored
backup) could still have made some. The oldest bill keeps its number; each later one gets a suffix (INV-0042-2)
and keeps the old number in `invoice_number_was`, and the bills linked to it (replaces / replaced_by) follow.
Each shop's counter is then raised to its highest INV- number, so no new bill can take a number already used.
Finally the unique index {business_id, invoice_number} (routers/sales.py makes numbers in the bill's transaction).
The in-memory dev database starts empty, so the fixes run only on a real MongoDB.
"""
import logging

from pymongo.asynchronous.database import AsyncDatabase

log = logging.getLogger("migrations")


async def _rename_duplicates(db) -> int:
    renamed = 0
    groups = await db.invoices.aggregate([
        {"$group": {"_id": {"b": "$business_id", "n": "$invoice_number"}, "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}}], allowDiskUse=True)
    async for g in groups:
        bid, number = g["_id"]["b"], g["_id"]["n"]
        bills = await db.invoices.find({"business_id": bid, "invoice_number": number},
                                       {"_id": 0, "id": 1}).sort([("created_at", 1), ("id", 1)]).to_list(None)
        suffix = 2
        for bill in bills[1:]:
            while await db.invoices.find_one({"business_id": bid, "invoice_number": f"{number}-{suffix}"}, {"_id": 1}):
                suffix += 1
            new = f"{number}-{suffix}"
            await db.invoices.update_one({"id": bill["id"]}, {"$set": {"invoice_number": new, "invoice_number_was": number}})
            await db.invoices.update_many({"business_id": bid, "replaces.id": bill["id"]},
                                          {"$set": {"replaces.invoice_number": new}})
            await db.invoices.update_many({"business_id": bid, "replaced_by.id": bill["id"]},
                                          {"$set": {"replaced_by.invoice_number": new}})
            renamed += 1
            suffix += 1
    return renamed


async def _catch_up_counters(db):
    """Same-length numbers compare as text in number order, so the longest group's highest is the shop's highest."""
    highest: dict[str, int] = {}
    rows = await db.invoices.aggregate([
        {"$match": {"invoice_number": {"$regex": r"^INV-\d+$"}}},
        {"$group": {"_id": {"b": "$business_id", "len": {"$strLenCP": "$invoice_number"}},
                    "max": {"$max": "$invoice_number"}}}], allowDiskUse=True)
    async for r in rows:
        bid = r["_id"]["b"]
        highest[bid] = max(highest.get(bid, 0), int(r["max"][4:]))
    for bid, seq in highest.items():
        await db.counters.update_one({"_id": f"invoice:{bid}"}, {"$max": {"seq": seq}}, upsert=True)


async def up(db):
    if isinstance(db, AsyncDatabase):
        if renamed := await _rename_duplicates(db):
            log.warning("invoice numbers: %d duplicate bill number(s) renamed with a suffix", renamed)
        await _catch_up_counters(db)
    await db.invoices.create_index([("business_id", 1), ("invoice_number", 1)], unique=True)
