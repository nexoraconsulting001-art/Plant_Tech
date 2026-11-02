"""Admins become a flag on the user (`is_admin`), set for the logins whose phone is in ADMIN_PHONES today.

From now on admin rights come only from `python -m manage_users admin grant <phone>`. ADMIN_PHONES is read here
and nowhere else, and can be removed from the server's .env once this has run.
"""
import logging

import audit
from common import normalize_in_phone
from settings import get_settings

log = logging.getLogger("migrations")


async def up(db):
    phones = sorted({p for p in (normalize_in_phone(x) for x in get_settings().admin_phones) if p})
    if not phones:
        return
    n = 0
    async for u in db.users.find({"phone": {"$in": phones}, "is_admin": {"$ne": True}},
                                 {"_id": 0, "id": 1, "business_id": 1}):
        await db.users.update_one({"id": u["id"]}, {"$set": {"is_admin": True}})
        await audit.log(db, None, "admin.grant", {"type": "user", "id": u["id"]},
                        {"via": "ADMIN_PHONES (migration 0801)"}, business_id=u["business_id"])
        n += 1
    log.info("0801: %d admin login(s) flagged from ADMIN_PHONES (%d number(s) listed)", n, len(phones))
