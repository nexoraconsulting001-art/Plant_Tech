"""Library: one live entry per name, search keys, public and private entries, the cache's version stamp.

- Live entries sharing a name (the old find-then-insert could race) are merged into the oldest one (a starter
  entry first): products linked to the others move to it and the others are marked removed (`merged_into`).
- Every entry gets `search_keys` (search.py). An entry without `public` keeps what other shops saw before:
  public unless hidden. Only entries shops start from now on are private until an admin approves them
  (KEEP_EXISTING_VISIBILITY is the hook to make the existing shop-made ones private too).
- Indexes: name_key unique among live entries (replaces the plain one), {source_business_id, search_keys} for
  a shop's private entries, {public, removed} (public ones only) to load them into memory.
"""
import logging

from pymongo import UpdateOne
from pymongo.errors import OperationFailure

from routers import library
from search import library_keys

log = logging.getLogger("migrations")

BATCH = 1000
KEEP_EXISTING_VISIBILITY = True


async def _merge_duplicates(db):
    groups = await (await db.library.aggregate([
        {"$match": {"removed": False}},
        {"$group": {"_id": "$name_key", "n": {"$sum": 1},
                    "entries": {"$push": {"id": "$id", "source": "$source", "at": "$created_at"}}}},
        {"$match": {"n": {"$gt": 1}}},
    ], allowDiskUse=True)).to_list(None)
    for g in groups:
        keep, *others = sorted(g["entries"], key=lambda e: (e.get("source") != "starter", e.get("at") or ""))
        ids = [e["id"] for e in others]
        await db.products.update_many({"library_id": {"$in": ids}}, {"$set": {"library_id": keep["id"]}})
        await db.library.update_many({"id": {"$in": ids}}, {"$set": {"removed": True, "merged_into": keep["id"]}})
        log.info("library: merged %d entries named %r into %s", len(ids), g["_id"], keep["id"])


async def up(db):
    await _merge_duplicates(db)
    ops = []
    async for e in db.library.find({}, {"_id": 0, "id": 1, "name": 1, "name_hi": 1, "botanical": 1, "aliases": 1,
                                        "public": 1, "hidden": 1, "source": 1}):
        update = {"search_keys": library_keys(e)}
        if "public" not in e:
            update["public"] = not e.get("hidden") and (KEEP_EXISTING_VISIBILITY or e.get("source") != "shop")
        ops.append(UpdateOne({"id": e["id"]}, {"$set": update}))
        if len(ops) >= BATCH:
            await db.library.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        await db.library.bulk_write(ops, ordered=False)
    try:
        await db.library.drop_index("name_key_1")  # the plain index from 0001
    except OperationFailure:
        pass  # already gone
    await db.library.create_index("name_key", unique=True, partialFilterExpression={"removed": False},
                                  name="name_key_live_unique")
    await db.library.create_index([("source_business_id", 1), ("search_keys", 1)])
    # Partial, so a query for a shop's private entries can never pick it (it would read every private entry).
    await db.library.create_index([("public", 1), ("removed", 1)], partialFilterExpression={"public": True},
                                  name="public_entries")
    await library.changed(db)
