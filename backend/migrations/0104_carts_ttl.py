"""Saved orders expire 7 days after their last save: a TTL index on `saved_at`, a BSON date next to `updated_at`.

`updated_at` stays an ISO string, because the previous server version parses it (datetime.fromisoformat) and a
rollback runs that version on this data. Orders saved before get `saved_at` from their `updated_at`; one whose
updated_at is already a date (an earlier build of this migration) gets its ISO string back. routers/carts.py
writes both from now on and reads either kind.
"""
from datetime import datetime, timezone

from pymongo import UpdateOne

KEEP_DAYS = 7  # = routers.carts.KEEP_DAYS
BATCH = 500


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


async def up(db):
    ops = []
    async for c in db.carts.find({"saved_at": {"$exists": False}, "updated_at": {"$exists": True}},
                                 {"_id": 1, "updated_at": 1}):
        when = _when(c["updated_at"])
        if when is None:
            continue
        ops.append(UpdateOne({"_id": c["_id"], "updated_at": c["updated_at"]},
                             {"$set": {"saved_at": when, "updated_at": when.isoformat()}}))
        if len(ops) >= BATCH:
            await db.carts.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        await db.carts.bulk_write(ops, ordered=False)
    await db.carts.create_index("saved_at", expireAfterSeconds=KEEP_DAYS * 86400)
