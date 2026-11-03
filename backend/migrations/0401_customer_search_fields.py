"""Customer search fields (name_key, name_words, phone10, phone10_rev) filled in, and their indexes.

Every customer without the fields gets them (routers/customers.py `search_fields`), in batches, walking the
collection once in _id order (migrations.walk; it used to look for "the next 1000 without fields" from the start
every time, which read the done ones again for every batch: 48 minutes for 2 million customers). Safe to repeat:
only customers still without the fields are written. Indexes: {business_id, name_key, id} for the list by name
and its pages, {business_id, name_words} and {business_id, phone10} / {business_id, phone10_rev} for search from
the start of a word or of the number's first or last digits, and {business_id, last_purchase_at: -1} for "most
recent customers" when an order starts.
"""
from pymongo import UpdateOne

from migrations import walk
from routers.customers import search_fields

BATCH = 1000


async def up(db):
    async for todo in walk(db.customers, {"name_key": None}, {"name": 1, "phone": 1}, BATCH):
        await db.customers.bulk_write([UpdateOne({"_id": c["_id"], "name_key": None},
                                                 {"$set": search_fields(c.get("name", ""), c.get("phone"))})
                                       for c in todo], ordered=False)
    await db.customers.create_index([("business_id", 1), ("name_key", 1), ("id", 1)])
    await db.customers.create_index([("business_id", 1), ("name_words", 1)])
    await db.customers.create_index([("business_id", 1), ("phone10", 1)])
    await db.customers.create_index([("business_id", 1), ("phone10_rev", 1)])
    await db.customers.create_index([("business_id", 1), ("last_purchase_at", -1)])
