"""Deleting an account (Play Store and App Store rules; DPDP Act erasure).

DELETE /api/account, confirmed with the password (or a fresh Google sign-in for a Google-only owner):
- An owner: the shop closes now and is erased after ACCOUNT_DELETE_GRACE_DAYS (30). Everyone in the shop is
  logged out at once and employees cannot log in. The owner logging in again within that time keeps the shop:
  the deletion is cancelled and the login answer says so (`deletion_cancelled: true`).
- An employee: only their own login is erased, at once. The owner can do the same (DELETE /api/staff/{id}).
  The shop's bills keep the name they were made under.

Erasing a shop (erase_due: daily in the worker, or `python -m manage_users erase-due`): the records GST law
requires (bills, supplier bills, stock records and the shop's tax identity) are first copied into
`gst_archive`, which no route reads and which MongoDB empties itself after ARCHIVE_YEARS (8, decision D9). Then
the shop's photos and every document carrying its business_id are deleted; the shop's own record goes last.
Every step can run again, so a run that stops half way is finished by the next one.
"""
import os
import re
from datetime import datetime, timedelta, timezone

import pymongo
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from pymongo import ReplaceOne, ReturnDocument

import audit
import auth
import db as db_module
import ratelimit
import storage_client
from common import now_iso
from db import get_db
from routers import files, notifications

router = APIRouter(prefix="/api/account", tags=["account"])

GRACE_DAYS = int(os.environ.get("ACCOUNT_DELETE_GRACE_DAYS", "30"))
ARCHIVE_YEARS = 8
CONFIRM_RATE = "10/hour"  # password guesses through this route, per login
GST_RECORDS = ("invoices", "purchases", "stock_history")
BUSINESS_TAX_FIELDS = ("id", "name", "address", "phone", "gst_registered", "gstin", "created_at")
NOT_ERASED = {"audit_log", "gst_archive", "migrations", "migration_lock", "library", "businesses"}
ERASE_TIMEOUT = 3600  # seconds: a big shop's archive copy takes longer than the app's 10 s per operation
FORMER_EMPLOYEE = "Former employee"


class DeleteIn(BaseModel):
    password: str = Field("", max_length=128)
    id_token: str = Field("", max_length=4096)  # Google-only owners: a fresh Google sign-in
    reason: str = Field("", max_length=500)


async def _confirm(user: dict, body: DeleteIn):
    await ratelimit.consume(f"account-confirm:{user['id']}", CONFIRM_RATE)
    if user.get("password_hash"):
        if not await auth.check_password_async(body.password, user["password_hash"]):
            raise HTTPException(401, "The current password is incorrect.")
    elif user.get("google_sub"):
        if not body.id_token:
            raise HTTPException(400, "Please sign in with Google again to confirm.")
        if (await auth.google_claims(body.id_token)).get("sub") != user["google_sub"]:
            raise HTTPException(401, "This is not the Google account you log in with.")
    else:
        raise HTTPException(400, "Please sign in with Google again to confirm.")


@router.delete("")
async def delete_account(body: DeleteIn | None = None, user: dict = Depends(auth.current_user)):
    body = body or DeleteIn()
    await _confirm(user, body)
    db = get_db()
    if user["role"] != "owner":
        await erase_login(db, user, by=user)
        return {"status": "erased"}
    bid = user["business_id"]
    now = datetime.now(timezone.utc)
    erase_after = now + timedelta(days=GRACE_DAYS)

    async def schedule(session):
        await db.businesses.update_one({"id": bid}, {"$set": {"deletion": {
            "requested_at": now, "erase_after": erase_after, "by": user["id"], "reason": body.reason.strip()}}},
            session=session)
        await db.users.update_many({"business_id": bid}, {"$inc": {"token_version": 1}}, session=session)
        await audit.log(db, user, "account.delete_requested", {"type": "business", "id": bid},
                        {"erase_after": erase_after.isoformat(), "reason": body.reason.strip()}, session=session)

    await db_module.in_transaction(schedule, db)
    async for u in db.users.find({"business_id": bid}, {"_id": 0, "id": 1}):
        auth.forget_user(u["id"])
    await db.push_tokens.delete_many({"business_id": bid})
    return {"status": "scheduled", "erase_after": erase_after.isoformat(), "grace_days": GRACE_DAYS}


async def on_login(db, user: dict, business: dict | None) -> bool:
    """After a right password or Google sign-in. A shop waiting to be erased: employees are refused, the owner
    keeps the shop (returns True: this login cancelled the deletion)."""
    if not business or not business.get("deletion"):
        return False
    if user["role"] != "owner":
        raise HTTPException(403, "This shop is being deleted. Please contact the shop owner.")
    r = await db.businesses.update_one({"id": business["id"], "deletion.erasing": {"$ne": True}},
                                       {"$unset": {"deletion": ""}})
    if not r.modified_count:
        raise HTTPException(403, "This shop has been deleted.")
    business.pop("deletion", None)
    await audit.log(db, user, "account.delete_cancelled", {"type": "business", "id": business["id"]})
    return True


async def erase_login(db, person: dict, *, by: dict | None):
    """One employee's login: personal details removed, logged out everywhere, never able to log in again."""
    await db.users.update_one({"id": person["id"]}, {
        "$set": {"name": FORMER_EMPLOYEE, "active": False, "permissions": {}, "erased_at": now_iso()},
        "$unset": {"phone": "", "email": "", "google_sub": "", "password_hash": ""},
        "$inc": {"token_version": 1}})
    auth.forget_user(person["id"])
    await notifications.forget_phones(db, person["id"])
    await auth.clear_login_failures(db, person.get("phone"))
    await audit.log(db, by, "staff.erase", {"type": "user", "id": person["id"]},
                    {"phone": auth.masked_phone(person.get("phone"))}, business_id=person["business_id"])


# ---- erasing a shop after the grace period -------------------------------------------------------------------

def _delete_files(paths: list[str]):
    """The shop's photos and their thumbnails, from the private and the public store (WP-7: same keys in both)."""
    keys = [k for p in paths for k in (p, files.thumb_key(p))]
    stores = [storage_client.PRIVATE] + ([storage_client.PUBLIC] if storage_client.public_store_separate() else [])
    for store in stores:
        storage_client.delete_many(keys, store)


async def _archive(db, business: dict, now: datetime) -> dict:
    purge_after = now + timedelta(days=365 * ARCHIVE_YEARS + 2)
    bid = business["id"]

    def row(kind: str, key, record: dict) -> ReplaceOne:
        return ReplaceOne({"_id": f"{kind}:{key}"}, {"business_id": bid, "kind": kind, "record": record,
                                                     "archived_at": now, "purge_after": purge_after}, upsert=True)

    identity = {k: v for k, v in business.items() if k in BUSINESS_TAX_FIELDS or k.startswith(("gst", "tax"))}
    await db.gst_archive.bulk_write([row("business", bid, identity)])
    counts = {}
    for coll in GST_RECORDS:
        batch, n = [], 0
        async for doc in db[coll].find({"business_id": bid}):
            batch.append(row(coll, doc.pop("_id"), doc))
            if len(batch) == 500:
                await db.gst_archive.bulk_write(batch, ordered=False)
                n, batch = n + len(batch), []
        if batch:
            await db.gst_archive.bulk_write(batch, ordered=False)
            n += len(batch)
        counts[coll] = n
    return counts


async def erase_shop(db, business_id: str, now: datetime | None = None) -> dict | None:
    """Archive, then erase one shop whose grace period is over. None if it is not (or no longer) due."""
    now = now or datetime.now(timezone.utc)
    with pymongo.timeout(ERASE_TIMEOUT):
        business = await db.businesses.find_one_and_update(
            {"id": business_id, "deletion.erase_after": {"$lte": now}}, {"$set": {"deletion.erasing": True}},
            projection={"_id": 0}, return_document=ReturnDocument.AFTER)
        if not business:
            return None
        archived = await _archive(db, business, now)
        paths = [u["path"] async for u in db.uploads.find({"business_id": business_id}, {"_id": 0, "path": 1})]
        await run_in_threadpool(_delete_files, paths)
        deleted = {}
        for name in await db.list_collection_names():
            if name in NOT_ERASED or name.startswith("system."):
                continue
            r = await db[name].delete_many({"business_id": business_id})
            if r.deleted_count:
                deleted[name] = r.deleted_count
        # kept by the shop's id in _id, with no business_id field: the bill-number counter, the summary schedule,
        # the photo storage count and the "already sent" notification markers (stock:<shop>:..., summary:<shop>:...)
        await db.counters.delete_many({"_id": {"$regex": f"^[^:]+:{re.escape(business_id)}(:|$)"}})
        await db.summary_schedule.delete_one({"_id": business_id})
        await db.storage_usage.delete_one({"_id": business_id})
        await db.push_marks.delete_many({"_id": {"$regex": f"^[^:]+:{re.escape(business_id)}:"}})
        await db.businesses.delete_one({"id": business_id})
    summary = {"archived": archived, "deleted": deleted, "photos": len(paths)}
    await audit.log(db, None, "account.erased", {"type": "business", "id": business_id}, summary,
                    business_id=business_id)
    return summary


async def erase_due(db, now: datetime | None = None) -> list[str]:
    """Erase every shop whose grace period is over; returns their ids. For the worker's daily run."""
    now = now or datetime.now(timezone.utc)
    due = [b["id"] async for b in db.businesses.find({"deletion.erase_after": {"$lte": now}}, {"_id": 0, "id": 1})]
    return [bid for bid in due if await erase_shop(db, bid, now) is not None]
