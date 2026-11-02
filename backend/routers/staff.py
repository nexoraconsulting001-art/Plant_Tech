"""Owner manages employees (role "worker" in the data): add, change permissions or PIN, switch off, remove.

New or changed PINs need at least 6 digits and not an obvious one (auth.check_new_pin); older 4-digit PINs keep
working until they are changed. Every change goes to the audit log (phone numbers masked).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

import audit
import auth
from common import new_id, normalize_in_phone, now_iso
from db import get_db
from routers import account, notifications

router = APIRouter(prefix="/api/staff", tags=["staff"])


class Permissions(BaseModel):
    sell: bool = True
    stock: bool = True
    customers: bool = True
    reports: bool = False


class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str
    password: str = Field(min_length=1, max_length=128)  # a PIN of 6+ digits, checked below
    permissions: Permissions = Permissions()


class StaffPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    password: str | None = Field(None, min_length=1, max_length=128)
    permissions: Permissions | None = None
    active: bool | None = None


def _view(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "phone": u.get("phone"), "active": u.get("active", True),
            "permissions": {p: bool((u.get("permissions") or {}).get(p)) for p in auth.PERMS},
            "last_login_at": u.get("last_login_at"), "created_at": u.get("created_at")}


async def _worker(db, owner: dict, worker_id: str) -> dict:
    worker = await db.users.find_one({"id": worker_id, "business_id": owner["business_id"], "role": "worker",
                                      "erased_at": {"$exists": False}}, {"_id": 0})
    if not worker:
        raise HTTPException(404, "Employee not found.")
    return worker


@router.get("")
async def list_staff(owner: dict = Depends(auth.require("owner"))):
    workers = await get_db().users.find({"business_id": owner["business_id"], "role": "worker",
                                         "erased_at": {"$exists": False}}, {"_id": 0}).sort("name", 1).to_list(200)
    return [_view(w) for w in workers]


@router.post("")
async def add_staff(body: StaffIn, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
    taken = HTTPException(409, "A login with this phone number already exists.")
    if await db.users.find_one({"phone": phone}, {"_id": 1}):
        raise taken
    auth.check_new_pin(body.password)
    worker = {"id": new_id(), "business_id": owner["business_id"], "role": "worker", "name": body.name.strip(),
              "phone": phone, "email": None, "google_sub": None,
              "password_hash": await auth.hash_password_async(body.password),
              "permissions": body.permissions.model_dump(), "active": True, "token_version": 0,
              "created_at": now_iso(), "last_login_at": None, "created_by": owner["id"]}
    try:
        await db.users.insert_one(dict(worker))
    except DuplicateKeyError:  # the same number added twice at once
        raise taken from None
    await auth.clear_login_failures(db, phone)
    await audit.log(db, owner, "staff.add", {"type": "user", "id": worker["id"]},
                    {"phone": auth.masked_phone(phone), "permissions": worker["permissions"]})
    return _view(worker)


@router.patch("/{worker_id}")
async def update_staff(worker_id: str, body: StaffPatch, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    worker = await _worker(db, owner, worker_id)
    update: dict = {}
    if body.name is not None:
        update["name"] = body.name.strip()
    if body.permissions is not None:
        update["permissions"] = body.permissions.model_dump()
    logout = False
    if body.password is not None:
        auth.check_new_pin(body.password)
        update["password_hash"] = await auth.hash_password_async(body.password)
        logout = True
    if body.active is not None:
        update["active"] = body.active
        logout = logout or not body.active
    if logout:  # a new password or switching off logs the worker out on every phone
        update["token_version"] = worker.get("token_version", 0) + 1
    if update:
        await db.users.update_one({"id": worker_id}, {"$set": update})
        auth.forget_user(worker_id)
    if logout:
        await notifications.forget_phones(db, worker_id)
    if body.password is not None:  # the owner setting a new PIN ends any wait from wrong ones
        await auth.clear_login_failures(db, worker.get("phone"))
    if update:
        changes = {k: v for k, v in update.items() if k in ("name", "permissions", "active")}
        if body.password is not None:
            changes["pin_changed"] = True
        if body.permissions is not None:
            changes["permissions_before"] = {p: bool((worker.get("permissions") or {}).get(p)) for p in auth.PERMS}
        await audit.log(db, owner, "staff.update", {"type": "user", "id": worker_id}, changes)
    return _view({**worker, **update})


@router.delete("/{worker_id}")
async def remove_staff(worker_id: str, owner: dict = Depends(auth.require("owner"))):
    """Remove an employee for good: their name, phone and PIN are erased and they can never log in again.
    Bills they made keep the name they were made under."""
    db = get_db()
    worker = await _worker(db, owner, worker_id)
    await account.erase_login(db, worker, by=owner)
    return {"ok": True}
