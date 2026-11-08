"""Uploads: path unique (duplicate records of one path merged first), direct-upload intents expire (WP-7).

- uploads.path: the baseline index was not unique. Records sharing a path describe the same stored photo, so
  all but the oldest are removed (the photo itself is not touched), then the unique index replaces the old one.
- upload_intents: one per /api/uploads/presign; looked up by id, removed a day after its link expires (TTL).
- storage_usage is keyed by _id (the shop), so it needs no index. Photos stored before WP-7 have no recorded
  size and are not counted against the quota.
"""
import logging

from rollups import aggregate

log = logging.getLogger("migrations")


async def up(db):
    dupes = await aggregate(db.uploads, [
        {"$sort": {"created_at": 1, "_id": 1}},
        {"$group": {"_id": "$path", "keep": {"$first": "$_id"}, "all": {"$push": "$_id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ], allowDiskUse=True)
    removed = 0
    async for d in dupes:
        res = await db.uploads.delete_many({"_id": {"$in": [i for i in d["all"] if i != d["keep"]]}})
        removed += res.deleted_count
    if removed:
        log.info("uploads: merged %d duplicate record(s)", removed)
    indexes = await db.uploads.index_information()
    for name, spec in indexes.items():  # the baseline's plain path_1, replaced by the unique one
        if spec.get("key") == [("path", 1)] and not spec.get("unique"):
            await db.uploads.drop_index(name)
    await db.uploads.create_index("path", unique=True)
    await db.upload_intents.create_index("id", unique=True)
    await db.upload_intents.create_index("expires_at", expireAfterSeconds=0)
