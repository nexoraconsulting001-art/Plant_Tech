"""Products (plants, pots, soil, tools) and their stock.

A product stays in the list at 0 stock; it is only removed when the owner deletes it. Stock can go below
zero when the owner bills more than was counted (see sales.py); such a product counts as out of stock.
Each product has a unit (pieces, kg, litre…, see units.py); plants are always pieces.
Pictures: the shop's own photo if it has one, else the shared library picture.
Codes (NP-0001…) come from the shop's counter ("sku:<business id>" in counters), unique per shop.
Search keys (catalog_search.py) are set on every insert and rename and never returned.
"""
import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import auth
import catalog_search
import rollups
from catalog_search import FIELDS, library_images
from common import name_key, new_id, now_iso
from db import get_db
from fast_json import respond
from routers import library
from stock import STOCK_EPS, record_stock, settle_stock  # noqa: F401 (also imported from here by older code)
from units import Unit, is_plant_category, unit_of

router = APIRouter(prefix="/api/products", tags=["products"])

PAGE_SIZE, PAGE_MAX = 50, 200
SEARCH_MAX = 200  # a search without `limit` (the 1.1.0 and 1.2.0 apps): the best 200 matches
SKU_TRIES = 5
LOW = {"$expr": {"$lte": [{"$ifNull": ["$stock", 0]}, {"$ifNull": ["$min_stock", 0]}]}}  # as view()'s low_stock


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


async def context(db, business_id: str):
    """Categories and shop GST setting needed to show products."""
    cats = {c["id"]: c for c in await db.categories.find({"business_id": business_id}, {"_id": 0}).to_list(1000)}
    biz = await db.businesses.find_one({"id": business_id}, {"_id": 0, "gst_registered": 1}) or {}
    return cats, bool(biz.get("gst_registered"))


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


async def views(db, user: dict, products: list[dict], lib: dict | None = None) -> list[dict]:
    """`lib`: the products' library entries when the caller already read them (catalog_search.find_products)."""
    cats, gst = await context(db, user["business_id"])
    if lib is None:
        lib = await library_images(db, products)
    return [view(p, cats, lib, gst, user) for p in products]


async def get_owned(db, user: dict, product_id: str) -> dict:
    p = await db.products.find_one({"id": product_id, "business_id": user["business_id"], "deleted_at": None}, FIELDS)
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


async def next_sku(db, business_id: str, session=None) -> str:
    """The shop's next product code from its counter: NP-0001, NP-0002…"""
    c = await db.counters.find_one_and_update({"_id": f"sku:{business_id}"}, {"$inc": {"seq": 1}}, upsert=True,
                                              return_document=ReturnDocument.AFTER, session=session)
    return f"NP-{c['seq']:04d}"


async def insert_product(db, doc: dict, entry: dict | None, session=None) -> dict:
    """Insert a new product (without `sku`) with the next free code and its search keys; returns it.
    Every new product goes through here (products, supplier bills, demo data). Inside a transaction (`session`)
    a taken code can't be retried (the error ends the transaction), so it is raised; migration 0302 has moved
    every counter past the codes that were given out by counting."""
    for _ in range(SKU_TRIES):
        doc = {**doc, "sku": await next_sku(db, doc["business_id"], session)}
        doc.update(catalog_search.keys_for(doc, entry))
        try:
            await db.products.insert_one(doc, session=session)
            doc.pop("_id", None)
            return doc
        except DuplicateKeyError:  # a code given out by counting products: move the counter past them
            if session is not None:
                raise
            doc.pop("_id", None)
            n = await db.products.count_documents({"business_id": doc["business_id"]})
            await db.counters.update_one({"_id": f"sku:{doc['business_id']}"}, {"$max": {"seq": n}})
    raise RuntimeError(f"no free product code for shop {doc['business_id']} after {SKU_TRIES} tries")


def _cursor(p: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps([p["name"], p["id"]]).encode()).decode()


def _after(cursor: str) -> dict:
    try:
        name, pid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not (isinstance(name, str) and isinstance(pid, str)):
            raise ValueError
    except (ValueError, TypeError):  # also bad base64 and bad JSON
        raise HTTPException(400, "Please refresh the list.")
    return {"$or": [{"name": {"$gt": name}}, {"name": name, "id": {"$gt": pid}}]}


@router.get("")
async def list_products(search: str = "", category_id: str = "", low: bool = False, ids: str = "",
                        category: str = "", limit: int | None = Query(None, ge=1, le=PAGE_MAX), cursor: str = "",
                        user: dict = Depends(auth.require("sell", "stock"))):
    """Without `limit` or `cursor`: an array as always (every product by name; a search: the best matches).
    With them: one page, {"items": [...], "next_cursor": "…" or null}, by name (a search: the best `limit`
    matches, no next page). `ids` (comma separated) = just these products, e.g. the fresh stock of an order's
    items before paying. `category` is the same as `category_id`; `low` = at or below the alert level.
    Answered through fast_json (the busiest route: every keystroke of a search)."""
    db = get_db()
    where: dict = {}
    if ids:
        where["id"] = {"$in": [i for i in ids.split(",") if i][:200]}
    if category_id or category:
        where["category_id"] = category_id or category
    if low:
        where.update(LOW)
    paged = limit is not None or bool(cursor)
    if search.strip():
        lib: dict = {}
        found = await catalog_search.find_products(db, user["business_id"], search, limit or SEARCH_MAX, where, lib)
        items = await views(db, user, found, lib)
        return respond({"items": items, "next_cursor": None} if paged else items)
    q = {**where, "business_id": user["business_id"], "deleted_at": None}
    if not paged:
        return respond(await views(db, user, await db.products.find(q, FIELDS).sort("name", 1).to_list(5000)))
    size = limit or PAGE_SIZE
    if cursor:
        q = {"$and": [q, _after(cursor)]}
    page = await db.products.find(q, FIELDS).sort([("name", 1), ("id", 1)]).limit(size + 1).to_list(size + 1)
    more = len(page) > size
    page = page[:size]
    return respond({"items": await views(db, user, page), "next_cursor": _cursor(page[-1]) if more else None})


_search = catalog_search.search_products  # moved; the old name stays for older imports


@router.get("/suggest")
async def suggest(q: str, user: dict = Depends(auth.require("sell", "stock"))):
    """Typing a product name: the shop's own matches first, then the shared library."""
    db = get_db()
    own, lib = await catalog_search.suggest(db, user["business_id"], q)
    return {"own": await views(db, user, own), "library": lib}


@router.get("/counts")
async def product_counts(category_id: str = "", user: dict = Depends(auth.require("sell", "stock"))):
    """For the stock filter: all products, low (at or below the alert level, not out), out of stock (0 or
    less) and below zero, counted in the database."""
    q = {"business_id": user["business_id"], "deleted_at": None}
    if category_id:
        q["category_id"] = category_id
    s, m = "$s", "$m"
    rows = await (await rollups.aggregate(get_db().products, [
        {"$match": q},
        {"$project": {"_id": 0, "s": {"$ifNull": ["$stock", 0]}, "m": {"$ifNull": ["$min_stock", 0]}}},
        {"$group": {"_id": None, "all": {"$sum": 1},
                    "low": {"$sum": {"$cond": [{"$and": [{"$lte": [s, m]}, {"$gt": [s, 0]}]}, 1, 0]}},
                    "out": {"$sum": {"$cond": [{"$lte": [s, 0]}, 1, 0]}},
                    "below": {"$sum": {"$cond": [{"$lt": [s, 0]}, 1, 0]}}}},
    ])).to_list(1)
    row = rows[0] if rows else {}
    return {k: row.get(k, 0) for k in ("all", "low", "out", "below")}


@router.post("")
async def create_product(body: ProductIn, user: dict = Depends(auth.require("stock"))):
    db = get_db()
    await _check_category(db, user, body.category_id)
    category_id = body.category_id
    library_id = body.library_id
    entry = None
    if library_id:
        entry = await db.library.find_one({"id": library_id, "removed": False}, library.FIELDS)
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
        if library_id:
            entry = await db.library.find_one({"id": library_id}, library.FIELDS)
    unit = "pcs" if is_plant_category((cat or {}).get("name")) else body.unit
    doc = await insert_product(db, {
        "id": new_id(), "business_id": user["business_id"],
        "name": body.name.strip(), "botanical_name": body.botanical_name.strip(), "category_id": category_id,
        "library_id": library_id, "photo_path": body.photo_path, "selling_price": body.selling_price,
        "cost_price": body.cost_price if auth.can(user, "owner") else 0, "stock": body.stock,
        "min_stock": body.min_stock, "supplier": body.supplier.strip(), "notes": body.notes.strip(), "unit": unit,
        "deleted_at": None, "created_at": now_iso(), "created_by": user["id"],
    }, entry)
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
    if "name" in update or "botanical_name" in update:  # found by its new name from now on
        keyed = await catalog_search.with_keys(db, {**p, **update})
        update.update(own_keys=keyed["own_keys"], search_keys=keyed["search_keys"])
    update["updated_at"] = now_iso()
    await db.products.update_one({"id": product_id, "business_id": user["business_id"]}, {"$set": update})
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
                                                projection=FIELDS, return_document=ReturnDocument.BEFORE)
    if not was:
        raise HTTPException(400, "You cannot remove more than the stock on hand.")
    p = await settle_stock(db, {**was, "stock": was.get("stock", 0) + body.change})  # what $inc left, on 3 decimals
    await record_stock(db, user, p, body.change, p["stock"], "adjustment", body.reason)
    return (await views(db, user, [p]))[0]


@router.delete("/{product_id}")
async def delete_product(product_id: str, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    await get_owned(db, owner, product_id)
    await db.products.update_one({"id": product_id, "business_id": owner["business_id"]},
                                 {"$set": {"deleted_at": now_iso(), "search_keys": []}})
    return {"ok": True}
