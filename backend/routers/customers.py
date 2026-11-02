"""Customers of a shop: name and phone are a must. Picked (or added) at the start of every order.

Search uses fields kept next to the name and phone, each searched from its start with an index
(migrations/0401), never a pattern over the whole collection:
- `name_key`: the name in lower case without punctuation ("Meena  Gupta." -> "meena gupta"); lists are sorted by it.
- `name_words`: the name from each word on ("meena gupta", "gupta"), so a surname finds the customer too.
- `phone10` and `phone10_rev`: the 10 digits, and reversed, so the first or the last digits find the number.
`search_fields(name, phone)` makes them; whoever writes a customer's name or phone sets them too. Customers saved
by code that doesn't yet (sales.py before its customer upsert, demo data) get them on the next search.
"""
import base64
import binascii
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import audit
import auth
from common import name_key, new_id, normalize_in_phone, now_iso
from db import get_db, in_transaction

router = APIRouter(prefix="/api/customers", tags=["customers"])

SEARCH_FIELDS = ("name_key", "name_words", "phone10", "phone10_rev")
HIDDEN = {"_id": 0, **{f: 0 for f in SEARCH_FIELDS}}  # the search fields stay out of responses
MAX_WORDS = 6  # name_words per customer
LIST_MAX = 5000  # the old, unpaged list (apps up to 1.2.0 load it whole)
PAGE = 50
FIND_MAX = 20
ERASED_NAME = "Erased customer"


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


class EraseIn(BaseModel):
    reason: str = Field("", max_length=200)


def phone10(phone: str | None) -> str:
    """'+919811122334' -> '9811122334' (empty when there is no number)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits[-10:]


def search_fields(name: str, phone: str | None) -> dict:
    """The search fields of a customer with this name and phone. Set them with every write of either."""
    key = name_key(name)
    words = key.split()
    p10 = phone10(phone)
    return {"name_key": key, "name_words": list(dict.fromkeys(" ".join(words[i:]) for i in range(min(len(words), MAX_WORDS)))),
            "phone10": p10, "phone10_rev": p10[::-1]}


def _prefix(text: str) -> dict:
    """Values starting with `text`: anchored, so the index is used (tests/test_customers.py checks every $regex)."""
    return {"$regex": "^" + re.escape(text)}


def _search(text: str) -> list[dict] | None:
    """What typed text matches: a name from the start of any word, or a phone number by its first or last digits
    (3 or more). None when nothing in it can be searched for ("(", "+91")."""
    raw = text.strip()
    digits = re.sub(r"\D", "", raw)
    if digits and not re.search(r"[^\d\s+()\-]", raw):  # only a number was typed
        if raw.startswith("+91") or (len(digits) > 10 and digits.startswith("91")):
            digits = digits[2:]
        elif len(digits) > 10:
            digits = digits.lstrip("0")
        if len(digits) >= 3:
            return [{"phone10": _prefix(digits[:10])}, {"phone10_rev": _prefix(digits[-10:][::-1])}]
    key = name_key(raw)
    return [{"name_words": _prefix(key)}] if key else None


async def _fill_search_fields(db, business_id: str):
    """Customers saved without search fields get them (one indexed look-up; nothing to do once every writer sets them)."""
    todo = await db.customers.find({"business_id": business_id, "name_key": None},
                                   {"_id": 0, "id": 1, "name": 1, "phone": 1}).to_list(1000)
    for c in todo:  # a few at most (the customers saved since the last search); the migration filled the rest
        await db.customers.update_one({"id": c["id"], "name_key": None},
                                      {"$set": search_fields(c.get("name", ""), c.get("phone"))})


def _encode_cursor(c: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps([c.get("name_key") or "", c["id"]]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        key, cid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if isinstance(key, str) and isinstance(cid, str):
            return key, cid
    except (ValueError, TypeError, binascii.Error):
        pass
    raise HTTPException(400, "This list has changed. Please refresh it.")


@router.get("")
async def list_customers(search: str = "", limit: int | None = Query(None, ge=1, le=200), cursor: str | None = None,
                         user: dict = Depends(auth.require("customers"))):
    """Sorted by name. Without `limit` and `cursor`: every customer (up to 5000) as a plain list, as apps up to 1.2.0
    expect. With either: one page, {"items", "next_cursor", "total"}; send next_cursor back for the next page (null on
    the last one). `total` (how many match) comes with the first page only."""
    db = get_db()
    bid = user["business_id"]
    paged = limit is not None or cursor is not None
    await _fill_search_fields(db, bid)
    query: dict = {"business_id": bid}
    if search.strip():
        ors = _search(search)
        if ors is None:
            return {"items": [], "next_cursor": None, "total": 0} if paged else []
        query["$or"] = ors
    order = [("name_key", 1), ("id", 1)]
    if not paged:
        return await db.customers.find(query, HIDDEN).sort(order).to_list(LIST_MAX)

    limit = limit or PAGE
    page_query = query
    if cursor:
        key, cid = _decode_cursor(cursor)
        after = {"$or": [{"name_key": {"$gt": key}}, {"name_key": key, "id": {"$gt": cid}}]}
        page_query = {"$and": [query, after]}
    with_key = {k: v for k, v in HIDDEN.items() if k != "name_key"}  # the cursor is made from the last name_key
    rows = await db.customers.find(page_query, with_key).sort(order).limit(limit + 1).to_list(limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    page = {"items": [{k: v for k, v in c.items() if k != "name_key"} for c in rows],
            "next_cursor": _encode_cursor(rows[-1]) if more else None}
    if not cursor:
        page["total"] = await db.customers.count_documents(query)
    return page


@router.get("/lookup")
async def lookup(phone: str, user: dict = Depends(auth.require("sell", "customers"))):
    """While billing: is this number an existing customer? Returns just enough to greet them."""
    p = normalize_in_phone(phone)
    c = await get_db().customers.find_one({"business_id": user["business_id"], "phone": p}, {"_id": 0}) if p else None
    return {"found": bool(c), "name": c["name"] if c else "", "purchase_count": c["purchase_count"] if c else 0}


@router.get("/find")
async def find(q: str = "", user: dict = Depends(auth.require("sell", "customers"))):
    """New order: pick the customer. A name from the start of any word, or a phone number's first or last digits;
    empty = the most recent customers. Up to 20 matches in index order, shown newest buyer first. Erased
    customers are left out."""
    db = get_db()
    bid = user["business_id"]
    fields = {"_id": 0, "id": 1, "name": 1, "phone": 1, "purchase_count": 1, "last_purchase_at": 1}
    live = {"business_id": bid, "erased_at": None}
    if not q.strip():
        return await db.customers.find(live, fields).sort("last_purchase_at", -1).limit(FIND_MAX).to_list(FIND_MAX)
    ors = _search(q)
    if ors is None:
        return []
    await _fill_search_fields(db, bid)
    found = await db.customers.find({**live, "$or": ors}, fields).limit(FIND_MAX).to_list(FIND_MAX)
    found.sort(key=lambda c: c.get("last_purchase_at") or "", reverse=True)
    return found


@router.post("")
async def create_customer(body: CustomerIn, user: dict = Depends(auth.require("sell", "customers"))):
    """A new customer. A number that is already saved returns that customer instead (with "existing": true, their
    saved name and details unchanged), so two phones adding the same person never make two."""
    db = get_db()
    bid = user["business_id"]
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
    name = body.name.strip()
    doc = {"id": new_id(), "business_id": bid, "name": name, "phone": phone, "email": body.email.strip(),
           "address": body.address.strip(), "notes": body.notes.strip(), "total_purchases": 0.0, "purchase_count": 0,
           "created_at": now_iso(), **search_fields(name, phone)}
    match = {"business_id": bid, "phone": phone}
    try:  # one write, so a second phone saving the same number at the same moment finds this one
        saved = await db.customers.find_one_and_update(match, {"$setOnInsert": doc}, projection=HIDDEN, upsert=True,
                                                       return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:  # lost that race under the unique {business_id, phone} index
        saved = await db.customers.find_one(match, HIDDEN)
    return {**saved, "existing": saved["id"] != doc["id"]}


@router.patch("/{customer_id}")
async def update_customer(customer_id: str, body: CustomerPatch, user: dict = Depends(auth.require("customers"))):
    """Edit a customer (opened from the customer's page, which also needs "customers"). Staff who may only sell
    can add a new customer while billing, but not change an existing one's number, where its bills go on WhatsApp."""
    db = get_db()
    bid = user["business_id"]
    c = await db.customers.find_one({"id": customer_id, "business_id": bid}, HIDDEN)
    if not c:
        raise HTTPException(404, "Customer not found.")
    if c.get("erased_at"):
        raise HTTPException(400, "This customer's details have been erased.")
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
        keys = search_fields(fields.get("name", c["name"]), fields.get("phone", c.get("phone")))
        await db.customers.update_one({"id": customer_id, "business_id": bid},
                                      {"$set": {**fields, **keys, "updated_at": now_iso()}})
        if changed := sorted(k for k in ("name", "phone") if k in fields and fields[k] != c.get(k)):
            # who changed where this customer's bills go (which fields only, no numbers)
            await audit.log(db, user, "customer.update", {"type": "customer", "id": customer_id}, {"fields": changed})
    return {**c, **fields}


@router.post("/{customer_id}/erase")
async def erase_customer(customer_id: str, body: EraseIn | None = None, owner: dict = Depends(auth.require("owner"))):
    """Owner only, when a customer asks to be forgotten. Their name, phone, email, address and notes are removed,
    and the phone and address on their past B2C bills are blanked (a bill with the buyer's GSTIN keeps them, as a
    tax invoice must). Bill numbers, items and totals stay as they were, and so do the customer's totals. All in
    one transaction with its audit log entry. Erasing again changes nothing."""
    db = get_db()
    bid = owner["business_id"]
    reason = body.reason.strip() if body else ""

    async def erase(session):
        c = await db.customers.find_one({"id": customer_id, "business_id": bid}, HIDDEN, session=session)
        if not c:
            raise HTTPException(404, "Customer not found.")
        if c.get("erased_at"):
            return c
        blank = {"name": ERASED_NAME, "phone": "", "email": "", "address": "", "notes": "", "erased_at": now_iso(),
                 "erased_by": {"id": owner["id"], "name": owner.get("name", "")}, "updated_at": now_iso()}
        await db.customers.update_one({"id": customer_id, "business_id": bid},
                                      {"$set": {**blank, **search_fields(ERASED_NAME, "")}}, session=session)
        bills = await db.invoices.update_many(
            {"business_id": bid, "customer_id": customer_id, "customer_gstin": {"$in": [None, ""]}},
            {"$set": {"customer_phone": "", "customer_address": "", "customer_erased": True}}, session=session)
        await audit.log(db, owner, "customer.erase", {"type": "customer", "id": customer_id},
                        {"bills": bills.modified_count, **({"reason": reason} if reason else {})}, session=session)
        return {**c, **blank}

    return await in_transaction(erase, db)


@router.get("/{customer_id}")
async def get_customer(customer_id: str, user: dict = Depends(auth.require("customers"))):
    db = get_db()
    c = await db.customers.find_one({"id": customer_id, "business_id": user["business_id"]}, HIDDEN)
    if not c:
        raise HTTPException(404, "Customer not found.")
    bills = await db.invoices.find({"business_id": user["business_id"], "customer_id": customer_id},
                                   {"_id": 0, "id": 1, "invoice_number": 1, "grand_total": 1, "created_at": 1,
                                    "payment_method": 1, "status": 1, "replaced_by": 1, "items.name": 1, "items.quantity": 1}
                                   ).sort("created_at", -1).to_list(200)
    return {**c, "invoices": bills}
