"""GST: suggested HSN code and exempt flag on categories saved before them; products by category index (WP-9).

- categories without `hsn` get the suggestion for their name (gst.suggest_hsn, "" when nothing fits) and
  `tax_exempt` when the suggestion is exempt and the category's GST is 0%. Categories that already have `hsn`
  (set by the owner, or by this migration before) are left alone, so a second run changes nothing.
- products {business_id, category_id}: the categories list counts each category's products in one aggregation,
  and deleting a category checks whether products still use it.
Tax rates (gst_percent) are not touched. Shops' GSTINs are not changed either: one that fails the new check only
gets a warning in the app (routers/business.py).
"""
from pymongo import UpdateOne

import gst

BATCH = 500


async def up(db):
    ops = []
    async for c in db.categories.find({"hsn": {"$exists": False}}, {"_id": 1, "name": 1, "gst_percent": 1}):
        code, exempt = gst.suggest_hsn(c.get("name"))
        ops.append(UpdateOne({"_id": c["_id"], "hsn": {"$exists": False}},
                             {"$set": {"hsn": code, "tax_exempt": exempt and not c.get("gst_percent")}}))
        if len(ops) >= BATCH:
            await db.categories.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        await db.categories.bulk_write(ops, ordered=False)
    await db.products.create_index([("business_id", 1), ("category_id", 1)])
