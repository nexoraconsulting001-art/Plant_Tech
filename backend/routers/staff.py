"""Owner manages employees (role "worker" in the data): add, change permissions or password, switch off."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
from common import new_id, normalize_in_phone, now_iso
from db import get_db
from routers import notifications

router = APIRouter(prefix="/api/staff", tags=["staff"])


class Permissions(BaseModel):
    sell: bool = True
    stock: bool = True
    customers: bool = True
    reports: bool = False


class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str
    password: str = Field(min_length=4, max_length=128)  # a 4-digit PIN is fine for workers
    permissions: Permissions = Permissions()


class StaffPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    password: str | None = Field(None, min_length=4, max_length=128)
    permissions: Permissions | None = None
    active: bool | None = None


def _view(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "phone": u.get("phone"), "active": u.get("active", True),
            "permissions": {p: bool((u.get("permissions") or {}).get(p)) for p in auth.PERMS},
            "last_login_at": u.get("last_login_at"), "created_at": u.get("created_at")}


@router.get("")
async def list_staff(owner: dict = Depends(auth.require("owner"))):
    workers = await get_db().users.find({"business_id": owner["business_id"], "role": "worker"},
                                        {"_id": 0}).sort("name", 1).to_list(200)
    return [_view(w) for w in workers]


@router.post("")
async def add_staff(body: StaffIn, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
    if await db.users.find_one({"phone": phone}):
        raise HTTPException(409, "A login with this phone number already exists.")
    worker = {"id": new_id(), "business_id": owner["business_id"], "role": "worker", "name": body.name.strip(),
              "phone": phone, "email": None, "google_sub": None, "password_hash": auth.hash_password(body.password),
              "permissions": body.permissions.model_dump(), "active": True, "token_version": 0,
              "failed_logins": 0, "locked_until": 0, "created_at": now_iso(), "last_login_at": None,
              "created_by": owner["id"]}
    await db.users.insert_one(worker)
    return _view(worker)


@router.patch("/{worker_id}")
async def update_staff(worker_id: str, body: StaffPatch, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    worker = await db.users.find_one({"id": worker_id, "business_id": owner["business_id"], "role": "worker"},
                                     {"_id": 0})
    if not worker:
        raise HTTPException(404, "Employee not found.")
    update: dict = {}
    if body.name is not None:
        update["name"] = body.name.strip()
    if body.permissions is not None:
        update["permissions"] = body.permissions.model_dump()
    logout = False
    if body.password is not None:
        update.update({"password_hash": auth.hash_password(body.password), "failed_logins": 0, "locked_until": 0})
        logout = True
    if body.active is not None:
        update["active"] = body.active
        logout = logout or not body.active
    if logout:  # a new password or switching off logs the worker out on every phone
        update["token_version"] = worker.get("token_version", 0) + 1
    if update:
        await db.users.update_one({"id": worker_id}, {"$set": update})
    if logout:
        await notifications.forget_phones(db, worker_id)
    return _view({**worker, **update})
