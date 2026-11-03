"""Saved orders, one per customer: a worker can start Kavita's order, bill Rahul meanwhile, and Kavita's
items are still there when anyone in the shop enters her number again. Not listed anywhere; making her
bill or discarding her order removes it, and one untouched for 7 days is dropped.

A product line's unit is always the product's own (as on the bill), whatever the app sent: older apps send none,
and the owner may change a product's unit while an order waits. Loose lines keep the unit they were added with.

`saved_at` is a BSON date, so MongoDB's TTL index on it (migration 0104) drops an order 7 days after its last
save; the TTL monitor runs about once a minute, so reading an order also checks its age. `updated_at` stays an ISO
string, as the previous server version reads and writes it: a rollback runs that version on this data. Drop it
(read saved_at instead) a release after that version is gone. Orders saved by older versions get saved_at from
the same migration."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import auth
from db import get_db
from fast_json import respond
from units import check_quantity, unit_of

router = APIRouter(prefix="/api/orders/cart", tags=["orders"])
KEEP_DAYS = 7
MAX_QTY = 100_000
MAX_PRICE = 10_000_000  # a unit price; sales.py and purchases.py use the same caps


class CartLine(BaseModel):
    product_id: str = Field(min_length=1, max_length=64)  # "loose-…" = billed without stock
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=MAX_QTY, allow_inf_nan=False)
    price: float = Field(ge=0, le=MAX_PRICE, allow_inf_nan=False)  # the price for this order (may differ from the shop price)
    list_price: float | None = Field(None, ge=0, le=MAX_PRICE, allow_inf_nan=False)  # the shop price when it was added
    gst_percent: float = Field(0, ge=0, le=100, allow_inf_nan=False)
    image_path: str | None = Field(None, max_length=300)
    stock: float | None = Field(None, allow_inf_nan=False)
    unit: str | None = Field(None, max_length=12)  # the product's unit when it was added (none = pieces)


class CartIn(BaseModel):
    customer_id: str = Field(min_length=1, max_length=64)
    items: list[CartLine] = Field(default_factory=list, max_length=200)
    discount: float = Field(0, ge=0, le=MAX_PRICE * 100, allow_inf_nan=False)
    gst_removed: bool = False


def _when(value) -> datetime:
    """updated_at as an aware UTC datetime: an ISO string, or a BSON date (PyMongo returns it naive, in UTC) that
    an earlier build of this release wrote."""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def clear(db, business_id: str, customer_id: str | None, session=None):
    if customer_id:
        await db.carts.delete_many({"business_id": business_id, "customer_id": customer_id}, session=session)


async def _product_units(db, business_id: str, product_ids: list[str]) -> dict[str, str]:
    """product id -> its unit now, for the product lines (not loose-…) of a saved order."""
    ids = list({pid for pid in product_ids if pid and not pid.startswith("loose-")})
    if not ids:
        return {}
    rows = await db.products.find({"business_id": business_id, "id": {"$in": ids}},
                                  {"_id": 0, "id": 1, "unit": 1}).to_list(len(ids))
    return {p["id"]: unit_of(p.get("unit")) for p in rows}


async def _own_customer(db, business_id: str, customer_id: str):
    if not await db.customers.find_one({"id": customer_id, "business_id": business_id}, {"_id": 1}):
        raise HTTPException(400, "Customer not found.")


@router.get("")
async def get_cart(customer_id: str = Query(min_length=1, max_length=64), user: dict = Depends(auth.require("sell"))):
    db = get_db()
    c = await db.carts.find_one({"business_id": user["business_id"], "customer_id": customer_id}, {"_id": 0})
    if not c:
        return respond(None)
    updated = _when(c["updated_at"])
    if updated < datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS):
        await clear(db, user["business_id"], customer_id)
        return respond(None)
    items = c.get("items", [])
    units = await _product_units(db, user["business_id"], [i.get("product_id") or "" for i in items])
    items = [{**i, "unit": units[i["product_id"]]} if i.get("product_id") in units else i for i in items]
    return respond({"items": items, "discount": c.get("discount", 0), "gst_removed": c.get("gst_removed", False),
                    "updated_at": updated.isoformat()})


@router.put("")
async def save_cart(body: CartIn, user: dict = Depends(auth.require("sell"))):
    db = get_db()
    await _own_customer(db, user["business_id"], body.customer_id)
    if not body.items:
        await clear(db, user["business_id"], body.customer_id)
        return respond(None)
    units = await _product_units(db, user["business_id"], [i.product_id for i in body.items])
    for i in body.items:
        i.unit = units.get(i.product_id) or unit_of(i.unit)  # a deleted product keeps what the app sent
        check_quantity(i.unit, i.quantity, i.name)
    now = datetime.now(timezone.utc)
    doc = {"business_id": user["business_id"], "customer_id": body.customer_id,
           "items": [i.model_dump() for i in body.items], "discount": body.discount, "gst_removed": body.gst_removed,
           "updated_at": now.isoformat(), "saved_at": now, "updated_by": {"id": user["id"], "name": user.get("name", "")}}
    await db.carts.replace_one({"business_id": user["business_id"], "customer_id": body.customer_id}, doc, upsert=True)
    return respond({"items": doc["items"], "discount": body.discount, "gst_removed": body.gst_removed,
                    "updated_at": now.isoformat()})


@router.delete("")
async def discard_cart(customer_id: str = Query(min_length=1, max_length=64), user: dict = Depends(auth.require("sell"))):
    await clear(get_db(), user["business_id"], customer_id)
    return {"ok": True}
