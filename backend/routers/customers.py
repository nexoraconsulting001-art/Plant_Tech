"""Customers of a shop: name and phone are a must. Picked (or added) at the start of every order."""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
from common import new_id, normalize_in_phone, now_iso
from db import get_db

router = APIRouter(prefix="/api/customers", tags=["customers"])


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=1, max_length=20)  # name and phone are a must; the rest is optional
    email: str = Field("", max_length=120)
    address: str = Field("", max_length=300)
    notes: str = Field("", max_length=500)


class CustomerPatch(BaseModel):
    """Only the fields sent are changed. Past bills keep the name, phone and address they were made with."""
    name: str | None = Field(None, max_length=80)
    phone: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=120)
    address: str | None = Field(None, max_length=300)
    notes: str | None = Field(None, max_length=500)


@router.get("")
async def list_customers(search: str = "", user: dict = Depends(auth.require("customers"))):
    q: dict = {"business_id": user["business_id"]}
    if search:
        # Typed text is matched as text, not as a pattern ("+91", "(" must not break the search).
        q["$or"] = [{"name": {"$regex": re.escape(search), "$options": "i"}},
                    {"phone": {"$regex": re.escape(search.replace(" ", ""))}}]
    return await get_db().customers.find(q, {"_id": 0}).sort("name", 1).to_list(5000)


@router.get("/lookup")
async def lookup(phone: str, user: dict = Depends(auth.require("sell", "customers"))):
    """While billing: is this number an existing customer? Returns just enough to greet them."""
    p = normalize_in_phone(phone)
    c = await get_db().customers.find_one({"business_id": user["business_id"], "phone": p}, {"_id": 0}) if p else None
    return {"found": bool(c), "name": c["name"] if c else "", "purchase_count": c["purchase_count"] if c else 0}


@router.get("/find")
async def find(q: str = "", user: dict = Depends(auth.require("sell", "customers"))):
    """New order: pick the customer. Name or phone (any part); empty = the most recent customers."""
    db = get_db()
    query: dict = {"business_id": user["business_id"]}
    text = q.strip()
    if text:
        digits = re.sub(r"\D", "", text)
        ors = [{"name": {"$regex": re.escape(text), "$options": "i"}}]
        if len(digits) >= 3:
            ors.append({"phone": {"$regex": re.escape(digits[-10:])}})
        query["$or"] = ors
    found = await db.customers.find(query, {"_id": 0, "id": 1, "name": 1, "phone": 1, "purchase_count": 1,
                                            "last_purchase_at": 1}).sort("last_purchase_at", -1).limit(20).to_list(20)
    return found


@router.post("")
async def create_customer(body: CustomerIn, user: dict = Depends(auth.require("sell", "customers"))):
    db = get_db()
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
    if await db.customers.find_one({"business_id": user["business_id"], "phone": phone}):
        raise HTTPException(409, "A customer with this phone number already exists.")
    doc = {"id": new_id(), "business_id": user["business_id"], "name": body.name.strip(), "phone": phone,
           "email": body.email.strip(), "address": body.address.strip(), "notes": body.notes.strip(),
           "total_purchases": 0.0, "purchase_count": 0, "created_at": now_iso()}
    await db.customers.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/{customer_id}")
async def update_customer(customer_id: str, body: CustomerPatch, user: dict = Depends(auth.require("customers"))):
    """Edit a customer (opened from the customer's page, which also needs "customers"). Staff who may only sell
    can add a new customer while billing, but not change an existing one's number, where its bills go on WhatsApp."""
    db = get_db()
    bid = user["business_id"]
    c = await db.customers.find_one({"id": customer_id, "business_id": bid}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Customer not found.")
    fields = {k: v.strip() for k, v in body.model_dump(exclude_none=True).items()}
    if "name" in fields and not fields["name"]:
        raise HTTPException(400, "Please enter the customer's name.")
    if "phone" in fields:
        phone = normalize_in_phone(fields["phone"])
        if not phone:
            raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
        if await db.customers.find_one({"business_id": bid, "phone": phone, "id": {"$ne": customer_id}}, {"_id": 1}):
            raise HTTPException(409, "A customer with this phone number already exists.")
        fields["phone"] = phone
    if fields:
        await db.customers.update_one({"id": customer_id, "business_id": bid}, {"$set": {**fields, "updated_at": now_iso()}})
    return {**c, **fields}


@router.get("/{customer_id}")
async def get_customer(customer_id: str, user: dict = Depends(auth.require("customers"))):
    db = get_db()
    c = await db.customers.find_one({"id": customer_id, "business_id": user["business_id"]}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Customer not found.")
    bills = await db.invoices.find({"business_id": user["business_id"], "customer_id": customer_id},
                                   {"_id": 0, "id": 1, "invoice_number": 1, "grand_total": 1, "created_at": 1,
                                    "payment_method": 1, "status": 1, "replaced_by": 1, "items.name": 1, "items.quantity": 1}
                                   ).sort("created_at", -1).to_list(200)
    return {**c, "invoices": bills}
