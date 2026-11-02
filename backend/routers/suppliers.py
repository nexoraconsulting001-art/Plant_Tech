"""Suppliers: one record per supplier per shop, never duplicated.

A supplier is the same one when the GSTIN matches, else the phone, else the name ("M/s Green Farms
Pvt. Ltd." is "Green Farms"). A supplier bill only fills in details the record is missing; it never
overwrites what the owner typed. Bill count, total and last bill are counted from the purchases by the
database (`_stats`), so they can't drift. Notes hold anything else (contact person, bank or UPI details, payment terms).
"""
import inspect
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
from common import name_key, new_id, now_iso
from db import get_db
from errors import AppError

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])
GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "llp", "co", "company", "inc"}


def supplier_key(name: str | None) -> str:
    """'M/s Green Farms Pvt. Ltd.' -> 'green farms'."""
    words = name_key(name or "").split()
    if words[:2] == ["m", "s"]:
        words = words[2:]
    elif words[:1] == ["ms"]:
        words = words[1:]
    while words and words[-1] in LEGAL_WORDS:
        words.pop()
    return " ".join(words)


def phone_key(raw: str | None) -> str:
    """Last 10 digits without +91 or a leading 0; landlines allowed. Empty if too short to be a number."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    digits = digits.lstrip("0")[-10:]
    return digits if len(digits) >= 6 else ""


def clean_gstin(raw: str | None) -> str:
    g = re.sub(r"\s", "", raw or "").upper()
    return g if GSTIN.match(g) else ""


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field("", max_length=20)
    gstin: str = Field("", max_length=20)
    address: str = Field("", max_length=300)
    notes: str = Field("", max_length=500)


class SupplierPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    phone: str | None = Field(None, max_length=20)
    gstin: str | None = Field(None, max_length=20)
    address: str | None = Field(None, max_length=300)
    notes: str | None = Field(None, max_length=500)


def _keys(fields: dict) -> dict:
    out = dict(fields)
    if "name" in fields:
        out["name"] = fields["name"].strip()
        out["name_key"] = supplier_key(fields["name"])
    if "phone" in fields:
        out["phone"] = fields["phone"].strip()
        out["phone_key"] = phone_key(fields["phone"])
    if "gstin" in fields:
        out["gstin"] = clean_gstin(fields["gstin"]) or fields["gstin"].strip().upper()
    for k in ("address", "notes"):
        if k in fields:
            out[k] = fields[k].strip()
    return out


async def find_match(db, business_id: str, name: str = "", phone: str = "", gstin: str = "",
                     exclude_id: str | None = None) -> dict | None:
    base: dict = {"business_id": business_id, "deleted_at": None}
    if exclude_id:
        base["id"] = {"$ne": exclude_id}
    for field, value in (("gstin", clean_gstin(gstin)), ("phone_key", phone_key(phone)), ("name_key", supplier_key(name))):
        if value:
            found = await db.suppliers.find_one({**base, field: value}, {"_id": 0})
            if found:
                return found
    return None


async def fill_missing(db, supplier: dict, phone: str = "", address: str = "", gstin: str = "") -> dict:
    """Add details the record doesn't have yet; never replace ones it has."""
    add: dict = {}
    if phone and not supplier.get("phone") and phone_key(phone):
        add.update(_keys({"phone": phone}))
    if address and not supplier.get("address"):
        add["address"] = address.strip()
    if clean_gstin(gstin) and not supplier.get("gstin"):
        add["gstin"] = clean_gstin(gstin)
    if add:
        add["updated_at"] = now_iso()
        await db.suppliers.update_one({"id": supplier["id"]}, {"$set": add})
        supplier = {**supplier, **add}
    return supplier


async def from_bill(db, business_id: str, created_by: str, name: str = "", phone: str = "", address: str = "",
                    gstin: str = "") -> dict | None:
    """The supplier on a bill: the existing record (with missing details filled in), or a new one."""
    match = await find_match(db, business_id, name, phone, gstin)
    if match:
        return await fill_missing(db, match, phone, address, gstin)
    if not supplier_key(name):
        return None
    doc = {"id": new_id(), "business_id": business_id, **_keys({"name": name, "phone": phone, "gstin": gstin,
                                                                 "address": address, "notes": ""}),
           "created_by": created_by, "created_at": now_iso(), "updated_at": now_iso(), "deleted_at": None}
    if not clean_gstin(gstin):
        doc["gstin"] = ""
    await db.suppliers.insert_one(doc)
    doc.pop("_id", None)
    return doc


def view(s: dict, stats: dict | None = None) -> dict:
    stats = stats or {}
    return {**{k: s.get(k, "") for k in ("id", "name", "phone", "gstin", "address", "notes", "created_at")},
            "bills": stats.get("bills", 0), "total": round(stats.get("total", 0), 2), "last_bill_at": stats.get("last")}


async def _stats(db, business_id: str, supplier_id: str | None = None) -> dict:
    """supplier id -> {bills, total, last}, counted by the database (index {business_id, supplier_id, created_at});
    one supplier's only when `supplier_id` is given."""
    match = {"business_id": business_id, "supplier_id": supplier_id if supplier_id else {"$ne": None}}
    rows = db.purchases.aggregate([
        {"$match": match},
        {"$group": {"_id": "$supplier_id", "bills": {"$sum": 1}, "total": {"$sum": {"$ifNull": ["$total", 0]}},
                    "last": {"$max": "$created_at"}}},
    ])
    if inspect.isawaitable(rows):  # PyMongo's async client; the in-memory dev database hands back the cursor itself
        rows = await rows
    return {r["_id"]: {"bills": r["bills"], "total": r["total"], "last": r["last"] or None} async for r in rows}


async def _get(db, business_id: str, supplier_id: str) -> dict:
    s = await db.suppliers.find_one({"id": supplier_id, "business_id": business_id, "deleted_at": None}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Supplier not found.")
    return s


def _exists(other: dict) -> AppError:
    return AppError(409, "This supplier already exists as {{name}}.", "supplier_exists", name=other["name"], id=other["id"])


@router.get("")
async def list_suppliers(search: str = "", user: dict = Depends(auth.require("stock"))):
    db = get_db()
    bid = user["business_id"]
    items = await db.suppliers.find({"business_id": bid, "deleted_at": None}, {"_id": 0}).sort("name", 1).to_list(2000)
    if search.strip():
        key, digits = supplier_key(search), re.sub(r"\D", "", search)
        items = [s for s in items if (key and key in s.get("name_key", "")) or (len(digits) >= 3 and digits in s.get("phone_key", ""))
                 or search.strip().upper() in (s.get("gstin") or "")]
    stats = await _stats(db, bid)
    return [view(s, stats.get(s["id"])) for s in items]


@router.post("")
async def create_supplier(body: SupplierIn, user: dict = Depends(auth.require("stock"))):
    db = get_db()
    bid = user["business_id"]
    if not supplier_key(body.name):
        raise HTTPException(400, "Please enter the supplier's name.")
    if other := await find_match(db, bid, body.name, body.phone, body.gstin):
        raise _exists(other)
    doc = {"id": new_id(), "business_id": bid, **_keys(body.model_dump()), "created_by": user["id"],
           "created_at": now_iso(), "updated_at": now_iso(), "deleted_at": None}
    await db.suppliers.insert_one(doc)
    return view(doc)


@router.get("/{supplier_id}")
async def get_supplier(supplier_id: str, user: dict = Depends(auth.require("stock"))):
    db = get_db()
    s = await _get(db, user["business_id"], supplier_id)
    bills = await db.purchases.find({"business_id": user["business_id"], "supplier_id": supplier_id}, {"_id": 0}
                                    ).sort("created_at", -1).to_list(500)
    stats = (await _stats(db, user["business_id"], supplier_id)).get(supplier_id)
    from routers.purchases import shown  # one shape for a bill everywhere
    return {**view(s, stats), "purchases": [shown(b) for b in bills]}


@router.patch("/{supplier_id}")
async def update_supplier(supplier_id: str, body: SupplierPatch, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    bid = owner["business_id"]
    s = await _get(db, bid, supplier_id)
    fields = body.model_dump(exclude_none=True)
    if "name" in fields and not supplier_key(fields["name"]):
        raise HTTPException(400, "Please enter the supplier's name.")
    if other := await find_match(db, bid, fields.get("name", ""), fields.get("phone", ""), fields.get("gstin", ""),
                                 exclude_id=supplier_id):
        raise _exists(other)
    update = {**_keys(fields), "updated_at": now_iso()}
    await db.suppliers.update_one({"id": supplier_id}, {"$set": update})
    if "name" in update and update["name"] != s["name"]:
        await db.purchases.update_many({"business_id": bid, "supplier_id": supplier_id}, {"$set": {"supplier": update["name"]}})
    return view({**s, **update}, (await _stats(db, bid, supplier_id)).get(supplier_id))


@router.delete("/{supplier_id}")
async def delete_supplier(supplier_id: str, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    await _get(db, owner["business_id"], supplier_id)
    if await db.purchases.find_one({"business_id": owner["business_id"], "supplier_id": supplier_id}):
        raise HTTPException(400, "This supplier has bills, so it cannot be deleted.")
    await db.suppliers.update_one({"id": supplier_id}, {"$set": {"deleted_at": now_iso()}})
    return {"ok": True}


async def backfill(db):
    """Link supplier bills saved before the supplier list existed (safe to run on every start)."""
    async for p in db.purchases.find({"supplier_id": None, "supplier": {"$nin": [None, ""]}}, {"_id": 0}):
        s = await from_bill(db, p["business_id"], p.get("created_by", ""), p["supplier"])
        await db.purchases.update_one({"id": p["id"]}, {"$set": {"supplier_id": s["id"] if s else ""}})
