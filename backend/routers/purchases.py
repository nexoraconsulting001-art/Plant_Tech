"""Supplier bills: add the items to stock (new items become new products, priced at cost plus the shop's profit %).

Each bill is linked to one supplier record (found or made from the details on the bill) and keeps the
photos of all its pages.

The stock, the new products, the stock history and the purchase are written in one transaction
(db.in_transaction): a bill whose third item fails adds nothing. The supplier record and the shared catalogue
entries for new names are found or made first, outside it (both are find-or-make, so a retry reuses them); the
catalogue look-ups run a few at a time, once per name.

Retries: the app may send an `Idempotency-Key` header with POST /purchases (idempotency.py). The same key again
answers with the supplier bill it saved (header `Idempotent-Replayed: true`) instead of adding the stock a second
time; the same key with changed items is refused (409 idempotency_mismatch, naming the bill that was saved). Without
the header (app 1.1.0) nothing changes.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import auth
import catalog_search
import idempotency
from common import new_id, now_iso
from db import get_db, in_transaction
from errors import AppError
from routers import library, products, suppliers
from routers.business import DEFAULT_PROFIT_PERCENT
from routers.carts import MAX_PRICE, MAX_QTY
from stock import history_row, notify_after, settle_stock, write_history

router = APIRouter(prefix="/api/purchases", tags=["purchases"])
MAX_PAGES = 5
MAX_ITEMS = 200
LOOKUPS_AT_ONCE = 8  # catalogue look-ups for new item names run side by side, this many at a time
ROUTE = "purchase"  # the kind of request an Idempotency-Key was used for (idempotency.previous)


class PurchaseItem(BaseModel):
    product_id: str | None = Field(None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=MAX_QTY, allow_inf_nan=False)
    purchase_price: float = Field(0, ge=0, le=MAX_PRICE, allow_inf_nan=False)
    selling_price: float | None = Field(None, ge=0, le=MAX_PRICE, allow_inf_nan=False)
    category_id: str | None = Field(None, max_length=64)


class PurchaseIn(BaseModel):
    supplier: str = Field("", max_length=120)
    supplier_id: str | None = Field(None, max_length=64)  # a saved supplier; else the details below find or make one
    supplier_phone: str = Field("", max_length=20)
    supplier_address: str = Field("", max_length=300)
    supplier_gstin: str = Field("", max_length=20)
    invoice_number: str = Field("", max_length=60)
    date: str = Field("", max_length=40)
    items: list[PurchaseItem] = Field(min_length=1, max_length=MAX_ITEMS)
    image_path: str | None = Field(None, max_length=300)  # older apps: one photo
    image_paths: list[str] = Field(default_factory=list, max_length=MAX_PAGES)
    total: float = Field(0, allow_inf_nan=False)


def shown(p: dict) -> dict:
    """A bill as the app gets it: always a list of page photos (older bills had one)."""
    paths = p.get("image_paths") or ([p["image_path"]] if p.get("image_path") else [])
    return {**{k: v for k, v in p.items() if k != "image_path"}, "image_paths": paths}


async def _replayed(db, user: dict, key: str | None, fp: str, response: Response) -> dict | None:
    """The supplier bill an earlier request with this key saved, or None (a new key, or no key)."""
    k = await idempotency.previous(db, user, key, ROUTE)
    if not k:
        return None
    doc = await db.purchases.find_one({"business_id": user["business_id"], "id": k["purchase_id"]}, {"_id": 0})
    if not doc:
        raise idempotency.conflict()
    if idempotency.changed(k, fp):
        raise AppError(409, "This supplier bill was already saved before your last changes. Those changes are not "
                            "in it.", "idempotency_mismatch", purchase_id=doc["id"])
    response.headers["Idempotent-Replayed"] = "true"
    return shown(doc)


async def _library_ids(db, items: list[PurchaseItem], business_id: str) -> list[str | None]:
    """The shared catalogue entry of each new item (None for items already in the shop), found or made once per
    name and LOOKUPS_AT_ONCE at a time: one at a time, a long bill spent seconds here before its transaction."""
    gate = asyncio.Semaphore(LOOKUPS_AT_ONCE)
    names = list(dict.fromkeys(it.name for it in items if not it.product_id))

    async def one(name: str) -> str | None:
        async with gate:
            return await library.entry_for_new_product(db, name, None, business_id)

    found = dict(zip(names, await asyncio.gather(*(one(n) for n in names))))
    return [None if it.product_id else found[it.name] for it in items]


@router.post("")
async def create_purchase(body: PurchaseIn, response: Response, user: dict = Depends(auth.require("stock")),
                          idempotency_key: str | None = idempotency.IdempotencyKey):
    db = get_db()
    bid = user["business_id"]
    fp = idempotency.fingerprint(body)
    if done := await _replayed(db, user, idempotency_key, fp, response):
        return done
    owner = auth.can(user, "owner")
    paths = list(dict.fromkeys([*body.image_paths, *([body.image_path] if body.image_path else [])]))[:MAX_PAGES]
    for path in paths:
        if not await db.uploads.find_one({"path": path, "business_id": bid}):
            raise HTTPException(400, "A page photo could not be found. Please add it again.")
    if body.supplier_id:
        supplier = await db.suppliers.find_one({"id": body.supplier_id, "business_id": bid, "deleted_at": None}, {"_id": 0})
        if not supplier:
            raise HTTPException(400, "Supplier not found.")
        supplier = await suppliers.fill_missing(db, supplier, body.supplier_phone, body.supplier_address, body.supplier_gstin)
    else:
        supplier = await suppliers.from_bill(db, bid, user["id"], body.supplier, body.supplier_phone,
                                             body.supplier_address, body.supplier_gstin)
    supplier_name = supplier["name"] if supplier else body.supplier.strip()

    shop = await db.businesses.find_one({"id": bid}, {"_id": 0, "profit_percent": 1}) or {}
    markup = 1 + shop.get("profit_percent", DEFAULT_PROFIT_PERCENT) / 100
    library_ids = await _library_ids(db, body.items, bid)
    entries = await catalog_search.library_images(db, [{"library_id": lid} for lid in library_ids])  # for search keys
    reason = f"Supplier bill {body.invoice_number}"
    purchase_id = new_id()

    async def add(session):
        rows, changes = [], []
        for it, library_id in zip(body.items, library_ids):
            if it.product_id:
                update: dict = {"$inc": {"stock": it.quantity}}
                if owner:
                    update["$set"] = {"cost_price": it.purchase_price, "supplier": supplier_name}
                p = await db.products.find_one_and_update({"id": it.product_id, "business_id": bid, "deleted_at": None},
                                                          update, projection={"_id": 0},
                                                          return_document=ReturnDocument.AFTER, session=session)
                if not p:
                    raise AppError(400, "{{name}} is no longer in your products.", "product_missing", name=it.name)
                p = await settle_stock(db, p, session)
                rows.append(history_row(user, p, it.quantity, p["stock"], "purchase", reason))
                changes.append((p, p["stock"] - it.quantity, p["stock"]))
            else:
                p = {"id": new_id(), "business_id": bid, "name": it.name.strip(),
                     "botanical_name": "", "category_id": it.category_id, "library_id": library_id, "photo_path": None,
                     "selling_price": it.selling_price if it.selling_price is not None else round(it.purchase_price * markup),
                     "cost_price": it.purchase_price if owner else 0, "stock": it.quantity, "min_stock": 5,
                     "supplier": supplier_name, "notes": "", "unit": "pcs", "deleted_at": None, "created_at": now_iso(),
                     "created_by": user["id"]}
                p = await products.insert_product(db, p, entries.get(library_id), session=session)  # code + search keys
                rows.append(history_row(user, p, it.quantity, it.quantity, "purchase", reason))
                changes.append((p, 0, it.quantity))
        await write_history(db, rows, session)
        doc = {"id": purchase_id, "business_id": bid,
               **body.model_dump(exclude={"supplier_phone", "supplier_address", "supplier_gstin", "image_path"}),
               "supplier": supplier_name, "supplier_id": supplier["id"] if supplier else "", "image_paths": paths,
               "created_by": user["id"], "created_at": now_iso()}
        await db.purchases.insert_one(doc, session=session)
        doc.pop("_id", None)
        return doc, changes

    try:
        doc, changes = await in_transaction(
            idempotency.keyed(db, user, idempotency_key, ROUTE, purchase_id, fp, add, id_field="purchase_id"), db)
    except DuplicateKeyError as e:  # the same key at the same moment: the first one's supplier bill
        if idempotency.is_key_clash(e) and (done := await _replayed(db, user, idempotency_key, fp, response)):
            return done
        raise
    await notify_after(db, bid, changes)
    return shown(doc)


@router.get("")
async def list_purchases(supplier_id: str = "", user: dict = Depends(auth.require("stock"))):
    q: dict = {"business_id": user["business_id"]}
    if supplier_id:
        q["supplier_id"] = supplier_id
    return [shown(p) for p in await get_db().purchases.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)]
