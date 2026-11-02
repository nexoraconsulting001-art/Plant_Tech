"""One login per phone number and per Google account: unique partial indexes on users.phone and google_sub.

Both can be empty (Google owners have no phone, phone logins no Google account), so the indexes only cover
documents where the field is a string. Duplicates left by sign-ups that raced each other are cleared first:
the oldest login keeps the number (the one logins found until now), the others keep their data but lose the
number, which moves to `phone_duplicate` / `google_sub_duplicate` and the audit log, for a person to look at.
The baseline's plain indexes of the same name are replaced.
"""
import inspect
import logging

import audit

log = logging.getLogger("migrations")

FIELDS = ("phone", "google_sub")


async def _dedupe(db, field: str) -> int:
    groups = db.users.aggregate([
        {"$match": {field: {"$type": "string"}}},
        {"$sort": {"created_at": 1, "_id": 1}},
        {"$group": {"_id": f"${field}", "users": {"$push": {"id": "$id", "business_id": "$business_id"}},
                    "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ])
    if inspect.isawaitable(groups):  # PyMongo's async client; the dev server's in-memory one returns the cursor
        groups = await groups
    moved = 0
    async for g in groups:
        keep, *others = g["users"]
        for u in others:
            await db.users.update_one({"id": u["id"]}, {"$set": {f"{field}_duplicate": g["_id"]},
                                                        "$unset": {field: ""}})
            await audit.log(db, None, "user.dedupe", {"type": "user", "id": u["id"]},
                            {"field": field, "kept_by": keep["id"]}, business_id=u.get("business_id"))
            moved += 1
    if moved:
        log.warning("0802: %d login(s) shared a %s with an older one; the number moved to %s_duplicate",
                    moved, field, field)
    return moved


async def up(db):
    for field in FIELDS:
        await _dedupe(db, field)
        name = f"{field}_1"
        existing = (await db.users.index_information()).get(name)
        if existing and not existing.get("unique"):
            await db.users.drop_index(name)
        await db.users.create_index(field, name=name, unique=True,
                                    partialFilterExpression={field: {"$type": "string"}})
