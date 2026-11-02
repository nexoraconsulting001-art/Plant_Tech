"""Audit log indexes: a shop's entries newest first, and one actor's entries (audit.py)."""


async def up(db):
    await db.audit_log.create_index([("business_id", 1), ("at", -1)])
    await db.audit_log.create_index([("actor.id", 1), ("at", -1)])
