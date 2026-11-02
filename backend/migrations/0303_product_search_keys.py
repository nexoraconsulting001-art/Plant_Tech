"""Products: search keys (catalog_search.py) on every product, and the indexes for search, the paged list
({business_id, deleted_at, name, id}) and copying an entry's new names to its products ({library_id})."""
from pymongo import UpdateOne

from catalog_search import keys_for, library_images

BATCH = 1000
FIELDS = {"_id": 0, "id": 1, "name": 1, "botanical_name": 1, "sku": 1, "library_id": 1, "deleted_at": 1}


async def _write(db, batch: list[dict]):
    entries = await library_images(db, batch)
    await db.products.bulk_write([UpdateOne({"id": p["id"]}, {"$set": keys_for(p, entries.get(p.get("library_id")))})
                                  for p in batch], ordered=False)


async def up(db):
    batch = []
    async for p in db.products.find({}, FIELDS):
        batch.append(p)
        if len(batch) >= BATCH:
            await _write(db, batch)
            batch = []
    if batch:
        await _write(db, batch)
    await db.products.create_index([("business_id", 1), ("search_keys", 1)])
    await db.products.create_index([("business_id", 1), ("deleted_at", 1), ("name", 1), ("id", 1)])
    await db.products.create_index("library_id")
