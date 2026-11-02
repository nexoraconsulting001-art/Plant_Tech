"""Saved orders expire 7 days after their last save: updated_at becomes a date, with a TTL index on it.

Orders saved by older server versions keep updated_at as an ISO string, which a TTL index ignores, so they are
converted first. routers/carts.py writes dates from now on and still reads either kind.
"""
from datetime import datetime

from pymongo import UpdateOne

KEEP_DAYS = 7  # = routers.carts.KEEP_DAYS
BATCH = 500


async def up(db):
    ops = []
    async for c in db.carts.find({"updated_at": {"$type": "string"}}, {"_id": 1, "updated_at": 1}):
        try:
            when = datetime.fromisoformat(c["updated_at"])
        except ValueError:
            continue
        ops.append(UpdateOne({"_id": c["_id"], "updated_at": c["updated_at"]}, {"$set": {"updated_at": when}}))
        if len(ops) >= BATCH:
            await db.carts.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        await db.carts.bulk_write(ops, ordered=False)
    await db.carts.create_index("updated_at", expireAfterSeconds=KEEP_DAYS * 86400)
