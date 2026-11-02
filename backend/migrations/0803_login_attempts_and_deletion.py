"""Indexes for wrong-password waits (login_attempts) and account deletion (gst_archive, shops due for erasing).

login_attempts: one document per phone number, forgotten a day after the last wrong password (auth.py).
gst_archive: the GST records of erased shops, emptied by MongoDB itself when their purge_after date comes
(routers/account.py). businesses: finding the shops whose grace period is over.
"""
LOGIN_ATTEMPTS_KEEP = 24 * 3600


async def up(db):
    await db.login_attempts.create_index("last_failed_at", expireAfterSeconds=LOGIN_ATTEMPTS_KEEP)
    await db.gst_archive.create_index([("business_id", 1), ("kind", 1)])
    await db.gst_archive.create_index("purge_after", expireAfterSeconds=0)
    await db.businesses.create_index("deletion.erase_after",
                                     partialFilterExpression={"deletion.erase_after": {"$exists": True}})
