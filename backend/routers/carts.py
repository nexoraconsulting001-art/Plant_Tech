"""Saved orders, one per customer: a worker can start Kavita's order, bill Rahul meanwhile, and Kavita's
items are still there when anyone in the shop enters her number again. Not listed anywhere; making her
bill or discarding her order removes it, and one untouched for 7 days is dropped."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import auth
from common import now_iso
from db import get_db
from units import check_quantity, unit_of

router = APIRouter(prefix="/api/orders/cart", tags=["orders"])
KEEP_DAYS = 7


class CartLine(BaseModel):
    product_id: str = Field(min_length=1, max_length=64)  # "loose-…" = billed without stock
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=100000)
    price: float = Field(ge=0)  # the price for this order (may differ from the shop price)
    list_price: float | None = Field(None, ge=0)  # the shop price when it was added
    gst_percent: float = 0
    image_path: str | None = Field(None, max_length=300)
    stock: float | None = None
    unit: str | None = Field(None, max_length=12)  # the product's unit when it was added (none = pieces)


class CartIn(BaseModel):
    customer_id: str = Field(min_length=1)
    items: list[CartLine] = Field(default_factory=list, max_length=200)
    discount: float = Field(0, ge=0)
    gst_removed: bool = False


async def clear(db, business_id: str, customer_id: str | None):
    if customer_id:
        await db.carts.delete_many({"business_id": business_id, "customer_id": customer_id})


async def _own_customer(db, business_id: str, customer_id: str):
    if not await db.customers.find_one({"id": customer_id, "business_id": business_id}, {"_id": 1}):
        raise HTTPException(400, "Customer not found.")


@router.get("")
async def get_cart(customer_id: str = Query(min_length=1), user: dict = Depends(auth.require("sell"))):
    db = get_db()
    c = await db.carts.find_one({"business_id": user["business_id"], "customer_id": customer_id}, {"_id": 0})
    if not c:
        return None
    if datetime.fromisoformat(c["updated_at"]) < datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS):
        await clear(db, user["business_id"], customer_id)
        return None
    return {"items": c.get("items", []), "discount": c.get("discount", 0), "gst_removed": c.get("gst_removed", False),
            "updated_at": c["updated_at"]}


@router.put("")
async def save_cart(body: CartIn, user: dict = Depends(auth.require("sell"))):
    db = get_db()
    await _own_customer(db, user["business_id"], body.customer_id)
    if not body.items:
        await clear(db, user["business_id"], body.customer_id)
        return None
    for i in body.items:
        check_quantity(i.unit, i.quantity, i.name)
        i.unit = unit_of(i.unit)
    doc = {"business_id": user["business_id"], "customer_id": body.customer_id,
           "items": [i.model_dump() for i in body.items], "discount": body.discount, "gst_removed": body.gst_removed,
           "updated_at": now_iso(),
           "updated_by": {"id": user["id"], "name": user.get("name", "")}}
    await db.carts.replace_one({"business_id": user["business_id"], "customer_id": body.customer_id}, doc, upsert=True)
    return {"items": doc["items"], "discount": body.discount, "gst_removed": body.gst_removed, "updated_at": doc["updated_at"]}


@router.delete("")
async def discard_cart(customer_id: str = Query(min_length=1), user: dict = Depends(auth.require("sell"))):
    await clear(get_db(), user["business_id"], customer_id)
    return {"ok": True}
