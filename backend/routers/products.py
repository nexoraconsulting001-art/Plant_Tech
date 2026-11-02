"""Products (plants, pots, soil, tools) and their stock.

A product stays in the list at 0 stock; it is only removed when the owner deletes it. Stock can go below
zero when the owner bills more than was counted (see sales.py); such a product counts as out of stock.
Each product has a unit (pieces, kg, litre…, see units.py); plants are always pieces.
Pictures: the shop's own photo if it has one, else the shared library picture.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

import auth
import notify
from common import name_key, new_id, now_iso
from db import get_db
from routers import library
from search import product_terms, search_score
from units import Unit, is_plant_category, unit_of

router = APIRouter(prefix="/api/products", tags=["products"])


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    botanical_name: str = Field("", max_length=120)
    category_id: str | None = None
    library_id: str | None = None
    photo_path: str | None = None
    selling_price: float = Field(0, ge=0)
    cost_price: float = Field(0, ge=0)
    stock: float = Field(0, ge=0)
    min_stock: float = Field(5, ge=0)
    supplier: str = Field("", max_length=120)
    notes: str = Field("", max_length=500)
    unit: Unit = "pcs"


class ProductPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    botanical_name: str | None = Field(None, max_length=120)
    category_id: str | None = None
    photo_path: str | None = None
    selling_price: float | None = Field(None, ge=0)
    cost_price: float | None = Field(None, ge=0)
    min_stock: float | None = Field(None, ge=0)
    supplier: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=500)
    unit: Unit | None = None


class StockIn(BaseModel):
    change: float
    reason: str = Field("Added", max_length=80)


STOCK_EPS = 1e-9  # "enough stock" allows for float noise in stock saved before settle_stock existed


async def settle_stock(db, product: dict) -> dict:
    """Keep stock on 3 decimals after a change ($inc). 0.7 kg less 0.4 is 0.29999999999999993 in floating point:
    unrounded, the last 0.3 kg could not be sold and selling it would leave a tiny stock below zero. Rounds only
    if nothing changed the stock since (another sale settles its own result). Returns the product as rounded."""
    raw = product.get("stock", 0)
    exact = round(raw, 3) or 0.0  # no -0.0
    if exact != raw:
        await db.products.update_one({"id": product["id"], "stock": raw}, {"$set": {"stock": exact}})
    return {**product, "stock": exact}


async def record_stock(db, user: dict, product: dict, change: float, new_stock: float, kind: str, reason: str = "",
                       bill: str = ""):
    """kind: initial, sale, adjustment, purchase, bill_cancel or bill_correct (`bill` = the bill's number for these two)."""
    await db.stock_history.insert_one({
        "id": new_id(), "business_id": user["business_id"], "product_id": product["id"],
        "product_name": product["name"], "change": change, "new_stock": new_stock, "type": kind, "reason": reason,
        **({"bill": bill} if bill else {}), "by": {"id": user["id"], "name": user.get("name", "")}, "created_at": now_iso()})
    await notify.stock_changed(db, user["business_id"], product, new_stock - change, new_stock)


async def context(db, business_id: str):
    """Categories and shop GST setting needed to show products."""
    cats = {c["id"]: c for c in await db.categories.find({"business_id": business_id}, {"_id": 0}).to_list(1000)}
    biz = await db.businesses.find_one({"id": business_id}, {"_id": 0, "gst_registered": 1}) or {}
    return cats, bool(biz.get("gst_registered"))


async def library_images(db, products: list[dict]) -> dict:
    ids = list({p["library_id"] for p in products if p.get("library_id")})
    if not ids:
        return {}
    return {e["id"]: e for e in await db.library.find({"id": {"$in": ids}}, {"_id": 0}).to_list(len(ids))}


def view(p: dict, cats: dict, lib: dict, gst_registered: bool, user: dict) -> dict:
    cat = cats.get(p.get("category_id")) or {}
    entry = lib.get(p.get("library_id")) or {}
    library_image = library.picture_for(entry, user["business_id"])
    v = {
        "id": p["id"], "sku": p.get("sku", ""), "name": p["name"], "botanical_name": p.get("botanical_name", ""),
        "category_id": p.get("category_id"), "category": cat.get("name", ""),
        "gst_percent": cat.get("gst_percent", 0) if gst_registered else 0,
        "selling_price": p.get("selling_price", 0), "unit": unit_of(p.get("unit")),
        "stock": p.get("stock", 0), "min_stock": p.get("min_stock", 0),
        "low_stock": p.get("stock", 0) <= p.get("min_stock", 0), "out_of_stock": p.get("stock", 0) <= 0,
        "supplier": p.get("supplier", ""), "notes": p.get("notes", ""), "library_id": p.get("library_id"),
        "photo_path": p.get("photo_path"), "image_path": p.get("photo_path") or library_image,
        "image_source": "own" if p.get("photo_path") else ("library" if library_image else None),
        "created_at": p.get("created_at"),
    }
    if auth.can(user, "owner") or auth.can(user, "reports"):
        v["cost_price"] = p.get("cost_price", 0)
    return v


async def views(db, user: dict, products: list[dict]) -> list[dict]:
    cats, gst = await context(db, user["business_id"])
    lib = await library_images(db, products)
    return [view(p, cats, lib, gst, user) for p in products]


async def get_owned(db, user: dict, product_id: str) -> dict:
    p = await db.products.find_one({"id": product_id, "business_id": user["business_id"], "deleted_at": None},
                                   {"_id": 0})
    if not p:
        raise HTTPException(404, "Product not found.")
    return p


async def _check_category(db, user: dict, category_id: str | None) -> dict | None:
    if not category_id:
        return None
    cat = await db.categories.find_one({"id": category_id, "business_id": user["business_id"]}, {"_id": 0})
    if not cat:
        raise HTTPException(400, "This category no longer exists.")
    return cat


@router.get("")
async def list_products(search: str = "", category_id: str = "", low: bool = False, ids: str = "",
                        user: dict = Depends(auth.require("sell", "stock"))):
    """`ids` (comma separated) = just these products, e.g. the fresh stock of an order's items before paying."""
    db = get_db()
    q: dict = {"business_id": user["business_id"], "deleted_at": None}
    if ids:
        q["id"] = {"$in": [i for i in ids.split(",") if i][:200]}
    if category_id:
        q["category_id"] = category_id
    products = await db.products.find(q, {"_id": 0}).sort("name", 1).to_list(5000)
    if search.strip():
        products = await _search(db, products, search)
    if low:
        products = [p for p in products if p.get("stock", 0) <= p.get("min_stock", 0)]
    return await views(db, user, products)


async def _search(db, products: list[dict], query: str) -> list[dict]:
    """Products matching what was typed, in English, Hindi or Hinglish; best matches first."""
    lib = await library_images(db, products)
    scored = [(s, p) for p in products if (s := search_score(query, product_terms(p, lib.get(p.get("library_id")))))]
    scored.sort(key=lambda sp: (-sp[0], sp[1]["name"].lower()))
    return [p for _, p in scored]


@router.get("/suggest")
async def suggest(q: str, user: dict = Depends(auth.require("sell", "stock"))):
    """Typing a product name: the shop's own matches first, then the shared library."""
    db = get_db()
    if len(name_key(q)) < 2:
        return {"own": [], "library": []}
    own = await _search(db, await db.products.find({"business_id": user["business_id"], "deleted_at": None},
                                                   {"_id": 0}).to_list(5000), q)
    own = own[:5]
    taken_ids = {p.get("library_id") for p in own}
    taken_names = {name_key(p["name"]) for p in own}
    lib = [e for e in await library.search(db, q, user["business_id"], limit=8)
           if e["id"] not in taken_ids and name_key(e["name"]) not in taken_names][:5]
    return {"own": await views(db, user, own), "library": lib}


@router.post("")
async def create_product(body: ProductIn, user: dict = Depends(auth.require("stock"))):
    db = get_db()
    await _check_category(db, user, body.category_id)
    category_id = body.category_id
    library_id = body.library_id
    if library_id:
        entry = await db.library.find_one({"id": library_id, "removed": False}, {"_id": 0})
        if not entry:
            raise HTTPException(400, "This catalogue item no longer exists.")
        if not category_id and entry.get("default_category"):
            cat = await db.categories.find_one({"business_id": user["business_id"],
                                                "name_key": name_key(entry["default_category"])}, {"_id": 0})
            category_id = cat["id"] if cat else None
    cat = await db.categories.find_one({"id": category_id}, {"_id": 0}) if category_id else None
    if not library_id:
        library_id = await library.entry_for_new_product(db, body.name, cat["name"] if cat else None,
                                                         user["business_id"])
    unit = "pcs" if is_plant_category((cat or {}).get("name")) else body.unit
    count = await db.products.count_documents({"business_id": user["business_id"]})
    doc = {
        "id": new_id(), "business_id": user["business_id"], "sku": f"NP-{count + 1:04d}",
        "name": body.name.strip(), "botanical_name": body.botanical_name.strip(), "category_id": category_id,
        "library_id": library_id, "photo_path": body.photo_path, "selling_price": body.selling_price,
        "cost_price": body.cost_price if auth.can(user, "owner") else 0, "stock": body.stock,
        "min_stock": body.min_stock, "supplier": body.supplier.strip(), "notes": body.notes.strip(), "unit": unit,
        "deleted_at": None, "created_at": now_iso(), "created_by": user["id"],
    }
    await db.products.insert_one(doc)
    if body.stock > 0:
        await record_stock(db, user, doc, body.stock, body.stock, "initial", "Opening stock")
    await library.contribute_photo(db, library_id, body.photo_path, user["business_id"])
    return (await views(db, user, [doc]))[0]


@router.get("/{product_id}")
async def get_product(product_id: str, user: dict = Depends(auth.require("sell", "stock"))):
    db = get_db()
    p = await get_owned(db, user, product_id)
    history = await db.stock_history.find({"business_id": user["business_id"], "product_id": product_id},
                                          {"_id": 0}).sort("created_at", -1).to_list(50)
    return {**(await views(db, user, [p]))[0], "history": history}


@router.put("/{product_id}")
async def update_product(product_id: str, body: ProductPatch, user: dict = Depends(auth.require("stock"))):
    db = get_db()
    p = await get_owned(db, user, product_id)
    update = body.model_dump(exclude_none=True)
    if "cost_price" in update and not auth.can(user, "owner"):
        update.pop("cost_price")
    cat = await _check_category(db, user, update["category_id"]) if "category_id" in update else (
        await db.categories.find_one({"id": p.get("category_id")}, {"_id": 0}) if p.get("category_id") else None)
    if is_plant_category((cat or {}).get("name")) and ("unit" in update or unit_of(p.get("unit")) != "pcs"):
        update["unit"] = "pcs"  # plants are always counted in pieces
    for k in ("name", "botanical_name", "supplier", "notes"):
        if k in update:
            update[k] = update[k].strip()
    update["updated_at"] = now_iso()
    await db.products.update_one({"id": product_id}, {"$set": update})
    if update.get("photo_path"):
        await library.contribute_photo(db, p.get("library_id"), update["photo_path"], user["business_id"])
    return (await views(db, user, [{**p, **update}]))[0]


@router.post("/{product_id}/stock")
async def adjust_stock(product_id: str, body: StockIn, user: dict = Depends(auth.require("stock"))):
    if body.change == 0:
        raise HTTPException(400, "Please enter how many to add or remove.")
    db = get_db()
    await get_owned(db, user, product_id)
    q = {"id": product_id, "business_id": user["business_id"]}
    if body.change < 0:
        q["stock"] = {"$gte": -body.change - STOCK_EPS}
    # BEFORE: the filter is checked on the stock before the change (mongomock re-checks it on AFTER)
    was = await db.products.find_one_and_update(q, {"$inc": {"stock": body.change}},
                                                projection={"_id": 0}, return_document=ReturnDocument.BEFORE)
    if not was:
        raise HTTPException(400, "You cannot remove more than the stock on hand.")
    p = await settle_stock(db, {**was, "stock": was.get("stock", 0) + body.change})  # what $inc left, on 3 decimals
    await record_stock(db, user, p, body.change, p["stock"], "adjustment", body.reason)
    return (await views(db, user, [p]))[0]


@router.delete("/{product_id}")
async def delete_product(product_id: str, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    await get_owned(db, owner, product_id)
    await db.products.update_one({"id": product_id}, {"$set": {"deleted_at": now_iso()}})
    return {"ok": True}
