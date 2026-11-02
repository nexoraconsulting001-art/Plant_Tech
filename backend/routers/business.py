"""The shop (setup wizard and settings) and its categories, each with a GST %.

Prices are typed tax-inclusive. GST only applies when the shop is GST registered; then each
category's GST % decides how much of a line's price is tax.

UPI: a shop can have several UPI IDs (`upi_ids`, each {id, upi_id, label}); `default_upi` is the id of the one the
pay QR starts with. `upi_id` stays the default's UPI ID for app versions that know only one, and their PUT with
`upi_id` changes (or, when empty, removes) the default. Shops saved before the list get it on first read.
`whatsapp_bill` (on unless the owner turned it off): the app opens WhatsApp with the bill as soon as it is made.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
from common import name_key, new_id, now_iso
from db import get_db
from errors import AppError

router = APIRouter(prefix="/api", tags=["business"])

# Suggestions shown in setup; the owner confirms real rates with their accountant.
SUGGESTED_CATEGORIES = [
    ("Indoor Plants", 0), ("Outdoor Plants", 0), ("Flowering Plants", 0), ("Succulents", 0), ("Cacti", 0),
    ("Trees", 0), ("Shrubs", 0), ("Herbs", 0), ("Seeds", 0), ("Pots", 18), ("Soil", 5), ("Fertilizers", 5),
    ("Garden Tools", 5),
]
# Rates since GST 2.0 (22 Sep 2025) plus the older 12% / 28% for anything still billed at them.
GST_SLABS = (0, 0.25, 3, 5, 12, 18, 28, 40)
# New products from a supplier bill are priced at cost plus this, unless the owner set their own %.
DEFAULT_PROFIT_PERCENT = 60
# name@bank, e.g. greenhouse@okaxis or 9811122334@ybl
UPI_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$")
MAX_UPI_IDS = 10


class UpiIn(BaseModel):
    id: str | None = Field(None, max_length=40)  # the app may name a new one, so it can be chosen as default
    upi_id: str = Field(max_length=80)
    label: str = Field("", max_length=40)  # e.g. "Owner GPay"


class BusinessIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    address: str | None = Field(None, max_length=300)
    phone: str | None = Field(None, max_length=20)
    gst_registered: bool | None = None
    gstin: str | None = Field(None, max_length=15)
    upi_id: str | None = Field(None, max_length=80)
    logo_path: str | None = None
    footer_note: str | None = Field(None, max_length=200)
    onboarding_done: bool | None = None
    profit_percent: float | None = Field(None, ge=0, le=1000)  # markup on cost for new products
    upi_ids: list[UpiIn] | None = Field(None, max_length=MAX_UPI_IDS)  # the whole list; wins over upi_id
    default_upi: str | None = None  # id of one of upi_ids (the first when missing)
    whatsapp_bill: bool | None = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    gst_percent: float = 0


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    gst_percent: float | None = None


class CategoriesBulk(BaseModel):
    items: list[CategoryIn]


def _check_gst(value: float | None):
    if value is not None and value not in GST_SLABS:
        raise AppError(400, "GST must be one of {{rates}}.", "bad_gst", rates=", ".join(f"{s:g}%" for s in GST_SLABS))


def check_upi(value: str, saved: dict | None = None):
    """name@bank. One the shop already had passes as it is (saved before this check), so other edits still save."""
    if saved and any(e["upi_id"] == value for e in saved["upi_ids"]):
        return
    if not UPI_RE.match(value):
        raise AppError(400, "{{upi}} is not a valid UPI ID. It looks like name@bank.", "bad_upi", upi=value)


def upi_fields(entries: list[dict], default: str | None) -> dict:
    """The stored UPI fields: the list, the default's id (the first when unknown) and its UPI ID for old apps."""
    if not any(e["id"] == default for e in entries):
        default = entries[0]["id"] if entries else None
    return {"upi_ids": entries, "default_upi": default,
            "upi_id": next((e["upi_id"] for e in entries if e["id"] == default), "")}


def shop_upis(biz: dict) -> dict:
    """A shop's UPI fields, including shops saved when there was only one `upi_id`."""
    if "upi_ids" in biz:
        return upi_fields(biz.get("upi_ids") or [], biz.get("default_upi"))
    old = (biz.get("upi_id") or "").strip()
    return upi_fields([{"id": new_id(), "upi_id": old, "label": ""}] if old else [], None)


def _upi_list(items: list[UpiIn], default: str | None, old: dict) -> dict:
    """The owner's edited list (this app version): each UPI ID checked, no repeats, every entry with its own id."""
    entries, seen_upi, seen_id = [], set(), set()
    for item in items:
        value = item.upi_id.strip()
        check_upi(value, old)
        if value.lower() in seen_upi:
            raise AppError(400, "{{upi}} is in the list twice.", "duplicate_upi", upi=value)
        seen_upi.add(value.lower())
        eid = item.id if item.id and item.id not in seen_id else new_id()
        seen_id.add(eid)
        entries.append({"id": eid, "upi_id": value, "label": item.label.strip()})
    return upi_fields(entries, default)


def _upi_single(value: str, old: dict) -> dict:
    """An app that knows one UPI ID sent it: it replaces the default (empty removes it); the others stay."""
    entries, default = [dict(e) for e in old["upi_ids"]], old["default_upi"]
    if not value:
        return upi_fields([e for e in entries if e["id"] != default], None)
    check_upi(value, old)
    if same := next((e for e in entries if e["upi_id"].lower() == value.lower()), None):
        return upi_fields(entries, same["id"])  # already in the list: it becomes the default
    for e in entries:
        if e["id"] == default:
            e["upi_id"] = value
    if not entries:
        entries = [{"id": new_id(), "upi_id": value, "label": ""}]
    return upi_fields(entries, default)


def _with_settings(biz: dict) -> dict:
    return {**biz, **shop_upis(biz), "whatsapp_bill": biz.get("whatsapp_bill", True)}


@router.get("/business")
async def get_business(user: dict = Depends(auth.current_user)):
    """Any logged-in user: the Pay, UPI and bill screens read the UPI IDs and the WhatsApp setting."""
    db = get_db()
    biz = await db.businesses.find_one({"id": user["business_id"]}, {"_id": 0})
    if biz is None:
        return None
    if "upi_ids" not in biz:  # saved before the list: store it once, so the default keeps its id
        upis = shop_upis(biz)
        await db.businesses.update_one({"id": biz["id"], "upi_ids": {"$exists": False}},
                                       {"$set": {"upi_ids": upis["upi_ids"], "default_upi": upis["default_upi"]}})
        biz = {**biz, **upis}
    return _with_settings(biz)


@router.put("/business")
async def update_business(body: BusinessIn, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    current = await get_business(owner)
    data = body.model_dump(exclude_none=True, exclude={"upi_ids", "default_upi"})
    update = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
    if body.upi_ids is not None:  # the list wins over a single upi_id
        update.pop("upi_id", None)
        update.update(_upi_list(body.upi_ids, body.default_upi, current))
    elif "upi_id" in update:
        update.update(_upi_single(update["upi_id"], current))
    elif body.default_upi is not None:
        update.update(upi_fields(current["upi_ids"], body.default_upi))
    if "gstin" in update:
        update["gstin"] = update["gstin"].upper()
    if update.get("onboarding_done") and not (update.get("name") or current.get("name")):
        raise HTTPException(400, "Please enter the shop name first.")
    update["updated_at"] = now_iso()
    await db.businesses.update_one({"id": owner["business_id"]}, {"$set": update})
    return _with_settings(await db.businesses.find_one({"id": owner["business_id"]}, {"_id": 0}))


@router.get("/categories/suggestions")
async def category_suggestions(user: dict = Depends(auth.current_user)):
    return [{"name": n, "gst_percent": g} for n, g in SUGGESTED_CATEGORIES]


@router.get("/categories")
async def list_categories(user: dict = Depends(auth.current_user)):
    db = get_db()
    cats = await db.categories.find({"business_id": user["business_id"]}, {"_id": 0}).sort("name", 1).to_list(500)
    for c in cats:
        c["product_count"] = await db.products.count_documents(
            {"business_id": user["business_id"], "category_id": c["id"], "deleted_at": None})
    return cats


async def _create_category(db, business_id: str, name: str, gst: float) -> dict:
    existing = await db.categories.find_one({"business_id": business_id, "name_key": name_key(name)}, {"_id": 0})
    if existing:
        return existing
    doc = {"id": new_id(), "business_id": business_id, "name": name.strip(), "name_key": name_key(name),
           "gst_percent": gst, "created_at": now_iso()}
    await db.categories.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.post("/categories")
async def create_category(body: CategoryIn, user: dict = Depends(auth.require("owner", "stock"))):
    _check_gst(body.gst_percent)
    gst = body.gst_percent if auth.can(user, "owner") else 0  # only the owner sets tax
    return await _create_category(get_db(), user["business_id"], body.name, gst)


@router.post("/categories/bulk")
async def create_categories(body: CategoriesBulk, owner: dict = Depends(auth.require("owner"))):
    """Setup wizard: add the chosen categories at once (existing names are kept as they are)."""
    for item in body.items:
        _check_gst(item.gst_percent)
    db = get_db()
    return [await _create_category(db, owner["business_id"], i.name, i.gst_percent) for i in body.items]


@router.patch("/categories/{cat_id}")
async def update_category(cat_id: str, body: CategoryPatch, owner: dict = Depends(auth.require("owner"))):
    _check_gst(body.gst_percent)
    db = get_db()
    cat = await db.categories.find_one({"id": cat_id, "business_id": owner["business_id"]}, {"_id": 0})
    if not cat:
        raise HTTPException(404, "Category not found.")
    update: dict = {}
    if body.name is not None:
        clash = await db.categories.find_one({"business_id": owner["business_id"], "name_key": name_key(body.name),
                                             "id": {"$ne": cat_id}})
        if clash:
            raise HTTPException(409, "A category with this name already exists.")
        update.update({"name": body.name.strip(), "name_key": name_key(body.name)})
    if body.gst_percent is not None:
        update["gst_percent"] = body.gst_percent
    if update:
        await db.categories.update_one({"id": cat_id}, {"$set": update})
    return {**cat, **update}


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    used = await db.products.count_documents({"business_id": owner["business_id"], "category_id": cat_id,
                                              "deleted_at": None})
    if used:
        if used == 1:
            raise AppError(400, "1 product uses this category. Move it to another category first.", "category_in_use")
        raise AppError(400, "{{n}} products use this category. Move them to another category first.", "category_in_use", n=used)
    await db.categories.delete_one({"id": cat_id, "business_id": owner["business_id"]})
    return {"ok": True}
