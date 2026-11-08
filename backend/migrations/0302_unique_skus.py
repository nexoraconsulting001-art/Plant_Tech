"""Product codes: a counter per shop and no code twice in a shop (unique {business_id, sku}).

Codes used to be the shop's product count plus one, so two products added at the same moment could share one.
Each shop's counter starts after its highest NP- code and its number of products; then the newer of two
products with the same code gets the next code from the counter (the old one is kept in `sku_was`) and loses
its search keys until 0303 (or the next search) gives it new ones.
"""
import logging

from pymongo import UpdateOne

from rollups import aggregate
from routers.products import next_sku

log = logging.getLogger("migrations")

_NUMBER = {"$let": {  # 12 for "NP-0012", 0 for any other code
    "vars": {"s": {"$cond": [{"$eq": [{"$type": "$sku"}, "string"]}, "$sku", ""]}},
    "in": {"$cond": [{"$regexMatch": {"input": "$$s", "regex": "^NP-[0-9]{1,15}$"}},
                     {"$toLong": {"$substrCP": ["$$s", 3, 20]}}, 0]}}}


async def up(db):
    shops = await (await aggregate(db.products, [
        {"$group": {"_id": "$business_id", "n": {"$sum": 1}, "top": {"$max": _NUMBER}}},
    ], allowDiskUse=True)).to_list(None)
    ops = [UpdateOne({"_id": f"sku:{s['_id']}"}, {"$max": {"seq": max(s["n"], s["top"] or 0)}}, upsert=True)
           for s in shops if s["_id"]]
    for i in range(0, len(ops), 1000):
        await db.counters.bulk_write(ops[i:i + 1000], ordered=False)

    twins = await (await aggregate(db.products, [
        {"$match": {"sku": {"$type": "string"}}},
        {"$group": {"_id": {"b": "$business_id", "s": "$sku"}, "n": {"$sum": 1},
                    "products": {"$push": {"id": "$id", "at": "$created_at"}}}},
        {"$match": {"n": {"$gt": 1}}},
    ], allowDiskUse=True)).to_list(None)
    for t in twins:
        _, *later = sorted(t["products"], key=lambda p: p.get("at") or "")
        for p in later:
            code = await next_sku(db, t["_id"]["b"])
            await db.products.update_one({"id": p["id"]}, {"$set": {"sku": code, "sku_was": t["_id"]["s"]},
                                                           "$unset": {"search_keys": "", "own_keys": ""}})
            log.info("product %s: code %s was used twice, now %s", p["id"], t["_id"]["s"], code)
    await db.products.create_index([("business_id", 1), ("sku", 1)], unique=True,
                                   partialFilterExpression={"sku": {"$type": "string"}}, name="business_sku_unique")
