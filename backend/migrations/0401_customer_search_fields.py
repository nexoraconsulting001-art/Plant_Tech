"""Customer search fields (name_key, name_words, phone10, phone10_rev) filled in, and their indexes.

Every customer without the fields gets them (routers/customers.py `search_fields`), in batches, walking the
collection once in _id order (migrations.walk; it used to look for "the next 1000 without fields" from the start
every time, which read the done ones again for every batch: 48 minutes for 2 million customers). A customer saved
without the fields while it runs (an older version during the deploy) can get an _id below where the walk has got
to (ObjectIds made by other processes or machines are only roughly in order), so it would be passed by: a last
pass looks for any still without the fields on the name_key index made here, shop by shop (the index starts with
the shop; without it every key is read), so it reads only those. Safe to repeat:
only customers still without the fields are written. Indexes: {business_id, name_key, id} for the list by name
and its pages, {business_id, name_words} and {business_id, phone10} / {business_id, phone10_rev} for search from
the start of a word or of the number's first or last digits, and {business_id, last_purchase_at: -1} for "most
recent customers" when an order starts.
"""
from pymongo import UpdateOne
from pymongo.asynchronous.collection import AsyncCollection

from migrations import walk
from routers.customers import search_fields

BATCH = 1000
BY_NAME = [("business_id", 1), ("name_key", 1), ("id", 1)]  # the list by name; the last pass reads it


async def _fill(db, todo: list[dict]):
    await db.customers.bulk_write([UpdateOne({"_id": c["_id"], "name_key": None},
                                             {"$set": search_fields(c.get("name", ""), c.get("phone"))})
                                   for c in todo], ordered=False)


async def _still_without(db) -> list[dict]:
    shops = await db.customers.distinct("business_id")  # one index key per shop
    cursor = db.customers.find({"business_id": {"$in": shops}, "name_key": None}, {"name": 1, "phone": 1}).limit(BATCH)
    if isinstance(db.customers, AsyncCollection):  # the in-memory dev database has no hints
        cursor = cursor.hint(BY_NAME)
    return await cursor.to_list(BATCH)


async def up(db):
    async for todo in walk(db.customers, {"name_key": None}, {"name": 1, "phone": 1}, BATCH):
        await _fill(db, todo)
    await db.customers.create_index(BY_NAME)
    while todo := await _still_without(db):  # the ones saved meanwhile behind the walk (see above)
        await _fill(db, todo)
    await db.customers.create_index([("business_id", 1), ("name_words", 1)])
    await db.customers.create_index([("business_id", 1), ("phone10", 1)])
    await db.customers.create_index([("business_id", 1), ("phone10_rev", 1)])
    await db.customers.create_index([("business_id", 1), ("last_purchase_at", -1)])
