"""Demo shops are marked `demo: true` on the business: the demo refresh now runs only on a shop marked so (demo_data.py).

Marks the shops that already hold demo data (seeded before the mark existed), so the staging cron keeps working.
"""


async def up(db):
    shops = await db.products.distinct("business_id", {"demo": True})
    if shops:
        await db.businesses.update_many({"id": {"$in": shops}}, {"$set": {"demo": True}})
