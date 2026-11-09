"""AI: identify a plant from a photo, read a supplier bill. Limited per shop per day and per user per minute.

Photos that are not what the screen expects are refused with a clear message (and a `code` the app
uses to offer the right next step): a laptop in Add items, a bill in Add items, a plant on Supplier bill.

Allowances: one `ai_usage` document per shop, period and kind ({business_id, period, kind}, unique; migration
0601), counted before the call and given back when Claude was never reached. Today every shop gets
AI_DAILY_LIMIT scans a day, plant photos and bill pages together (kind "scan"), as before. Plan allowances
(decision D4: e.g. a trial's daily scans, paid tiers' monthly ones) only need `allowance` to answer
differently. A user may also start at most AI_USER_RATE scans (ratelimit.py, default 20 a minute).
"""
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import ai_client
import auth
import catalog_search
import ratelimit
import storage_client
from common import date_key, name_key
from db import get_db
from errors import AppError
from routers import suppliers
from routers.products import views
from routers.purchases import MAX_PAGES
from routers.suppliers import clean_gstin, supplier_key
from search import best_product, product_names
from settings import get_settings

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
SCAN = "scan"  # the allowance plant photos and bill pages share
NAME_FIELDS = {"_id": 0, "id": 1, "name": 1, "botanical_name": 1, "other_names": 1, "library_id": 1}
ENTRY_NAMES = {"_id": 0, "id": 1, "name": 1, "name_hi": 1, "botanical": 1, "aliases": 1}
RECOMMENDED = 4  # "in your stock" products shown with a scan's result


def photo_memory_hours() -> float:
    """How long a plant photo's answer is remembered (AI_PHOTO_CACHE_HOURS, default 24; 0 = off). The same photo
    sent again (a retry after no answer, the same picture picked again) gets the same answer and uses no scan."""
    try:
        return max(0.0, float(os.environ.get("AI_PHOTO_CACHE_HOURS", "24")))
    except ValueError:
        return 24.0


class ImagePayload(BaseModel):
    image_base64: str = Field(max_length=MAX_IMAGE_BYTES * 4 // 3 + 64)


async def allowance(db, business_id: str, kind: str = SCAN) -> tuple[str, int]:
    """(period, limit) of a shop's allowance of `kind`. Decision D4 is open: one daily limit for every shop."""
    return date_key(), get_settings().ai_daily_limit


async def use_quota(db, business_id: str, n: int = 1, kind: str = SCAN) -> str:
    """Count n scans; the period they were counted in (to give them back), or 429 when the allowance is used up."""
    period, limit = await allowance(db, business_id, kind)
    key = {"business_id": business_id, "period": period, "kind": kind}
    try:
        doc = await db.ai_usage.find_one_and_update(key, {"$inc": {"count": n}}, upsert=True,
                                                    return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:  # another scan created the day's document at the same moment
        doc = await db.ai_usage.find_one_and_update(key, {"$inc": {"count": n}},
                                                    return_document=ReturnDocument.AFTER)
    if doc["count"] > limit:
        await db.ai_usage.update_one(key, {"$inc": {"count": -n}})  # the count stays what was really used
        raise HTTPException(429, "The daily photo scan limit has been reached. Please search by name, or try again tomorrow.")
    return period


async def _limit_user(user: dict):
    await ratelimit.consume(f"ai:{user['id']}", os.environ.get("AI_USER_RATE") or "20/minute")


async def _run(db, business_id: str, n: int, period: str, call):
    """Run an AI call; when Claude was never reached (unreadable photo, outage, busy, paused) the scans are given back."""
    try:
        return await call()
    except (ai_client.BadImage, ai_client.AIUnavailable) as e:
        await db.ai_usage.update_one({"business_id": business_id, "period": period, "kind": SCAN},
                                     {"$inc": {"count": -n}})
        if isinstance(e, ai_client.BadImage):
            raise HTTPException(400, str(e))
        err = AppError(503, str(e), e.code)
        if e.retry_after:
            err.headers = {"Retry-After": str(int(e.retry_after))}
        raise err


async def _stock(db, bid: str):
    """The shop's products with every name they have (search.product_names: own name, botanical name, the owner's
    other names, the shared entry's English, Hindi, botanical and other names), for matching what Claude or a bill
    read, and for the stock list Claude sees (each product's `also`). Returns (products, names_of)."""
    stock = await db.products.find({"business_id": bid, "deleted_at": None}, NAME_FIELDS).to_list(5000)
    ids = list({p["library_id"] for p in stock if p.get("library_id")})
    entries = {e["id"]: e for e in await db.library.find({"id": {"$in": ids}, "removed": False},
                                                          ENTRY_NAMES).to_list(len(ids) or 1)} if ids else {}
    names = {p["id"]: product_names(p, entries.get(p.get("library_id"))) for p in stock}
    for p in stock:
        entry = entries.get(p.get("library_id")) or {}
        if not (p.get("botanical_name") or "").strip() and entry.get("botanical"):
            p["botanical_name"] = entry["botanical"]  # the list Claude sees names the plant botanically
        p["also"] = names[p["id"]][1:]
    return stock, lambda p: names[p["id"]]


def _name_index(stock: list[dict], names_of) -> dict[str, dict]:
    """name key -> product, for a name Claude copied from the list: every product's own name, and any other name
    that only one product has (two products of the same plant share their other names: those need the own name)."""
    index: dict[str, dict] = {}
    for p in stock:
        index.setdefault(name_key(p["name"]), p)
    others: dict[str, dict[str, dict]] = {}
    for p in stock:
        for n in names_of(p)[1:]:
            others.setdefault(name_key(n), {})[p["id"]] = p
    for k, ps in others.items():
        if k and k not in index and len(ps) == 1:
            index[k] = next(iter(ps.values()))
    return index


def _listed(by_key: dict[str, dict], name: str) -> dict | None:
    """The shop's product Claude copied from its list (name_key: case and spaces do not matter), or None for a
    name that is not one of them (Claude must never invent a product)."""
    p = by_key.get(name_key(name))
    if p is None and "(" in name:  # the botanical name in brackets copied along
        p = by_key.get(name_key(name.split("(")[0]))
    return p


async def _remembered(db, key: str) -> dict | None:
    doc = await db.ai_photo_answers.find_one({"_id": key, "expires_at": {"$gt": datetime.now(timezone.utc)}},
                                             {"_id": 0, "result": 1})
    return doc["result"] if doc else None


async def _remember(db, key: str, result: dict, hours: float):
    now = datetime.now(timezone.utc)
    await db.ai_photo_answers.replace_one({"_id": key}, {"result": result, "created_at": now,
                                                         "expires_at": now + timedelta(hours=hours)}, upsert=True)


@router.post("/identify-plant")
async def identify(body: ImagePayload, user: dict = Depends(auth.require("sell", "stock"))):
    """What the photo shows and which of the shop's products it is.

    Consistency: Claude chooses from the shop's own product list (sent with the photo), and that choice is only
    used when it is really one of the products. Its names are also matched through the shop's search, which knows
    Hindi, Hinglish and other names from the shared catalogue ("Pothos" finds Money Plant). The same photo is
    answered from memory for AI_PHOTO_CACHE_HOURS without using a scan.
    Answer: matched_product (or None), recommended_products (other close products in stock, best first; with a
    match they are the "not this one?" choices), and for older apps similar_products (the same list, only when
    nothing matched) and matched_catalog_name."""
    db = get_db()
    bid = user["business_id"]
    await _limit_user(user)
    try:
        image = ai_client.decode_b64(body.image_base64)
    except ai_client.BadImage as e:
        raise HTTPException(400, str(e))
    hours = photo_memory_hours()
    memory_key = f"{bid}:{hashlib.sha256(image).hexdigest()}"
    stock, names_of = await _stock(db, bid)
    cats = await db.categories.find({"business_id": bid}, {"_id": 0}).to_list(500)
    result = await _remembered(db, memory_key) if hours else None
    if result is None:
        period = await use_quota(db, bid)
        result = await _run(db, bid, 1, period, lambda: ai_client.identify_plant(
            image, [c["name"] for c in cats], stock))
        if hours and result.get("kind") != "unknown":
            await _remember(db, memory_key, dict(result), hours)
    result = dict(result)
    kind = result.pop("kind")
    logger.info("identify: kind=%s reason=%s", kind, result.pop("reason", ""))
    if kind == "bill":
        raise AppError(422, "This looks like a supplier bill. To add stock from a bill, use Supplier bill.",
                       "looks_like_bill")
    if kind == "other":
        raise AppError(422, "This photo does not appear to show a plant or nursery product. "
                            "Please photograph the item clearly, or search by name.", "not_nursery_item")
    if kind == "unknown" or not result["plant_name"]:
        raise AppError(422, "This item could not be identified. Please retake the photo in good light, "
                            "or search by name.", "not_identified")

    by_key = _name_index(stock, names_of)
    chosen = _listed(by_key, result.get("stock_match") or "")
    chosen_also = [p for n in result.get("stock_alternatives") or [] if (p := _listed(by_key, n))]
    names = [n for n in (result["plant_name"], result["scientific_name"], *result["alternatives"]) if n]
    matched, similar = best_product(stock, names, names_of)  # any name of a product: "Holy Basil" is "Tulsi"
    matched = chosen or matched  # Claude saw the photo and the list: its choice wins over a name comparison
    found: list[dict] = []
    for n in names[:4]:  # the shop's search, whole names only: Hindi, Hinglish and catalogue names too
        found += await catalog_search.find_products(db, bid, n, limit=RECOMMENDED, whole=True)
    ranked, seen = [], {matched["id"]} if matched else set()
    for p in [*chosen_also, *similar, *found]:
        if p["id"] not in seen:
            seen.add(p["id"])
            ranked.append(p["id"])
    wanted = ([matched["id"]] if matched else []) + ranked[:RECOMMENDED]
    full = {p["id"]: p for p in await db.products.find({"business_id": bid, "id": {"$in": wanted}, "deleted_at": None},
                                                         {"_id": 0}).to_list(None)}
    shown = await views(db, user, [full[i] for i in wanted if i in full])
    by_id = {p["id"]: p for p in shown}
    result["matched_product"] = by_id.get(matched["id"]) if matched else None
    result["recommended_products"] = [by_id[i] for i in ranked[:RECOMMENDED] if i in by_id]
    result["similar_products"] = [] if result["matched_product"] else result["recommended_products"]
    result["matched_catalog_name"] = result["matched_product"]["name"] if result["matched_product"] else ""
    cat = next((c for c in cats if c["name"] == result.get("suggested_category")), None)
    result["suggested_category_id"] = cat["id"] if cat else None
    result["kind"] = kind  # "plant" (always sold in pieces) or "nursery_product"
    return result


class InvoicePayload(BaseModel):
    image_base64: str | None = Field(None, max_length=MAX_IMAGE_BYTES * 4 // 3 + 64)  # older apps: one photo
    image_paths: list[str] = Field(default_factory=list, max_length=20)  # pages already uploaded, in order


def _same_supplier(names: list[str]) -> bool:
    """Supplier names read from different pages: the same unless clearly different (allows small misreads)."""
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            wa, wb = set(a.split()), set(b.split())
            if a != b and not (wa <= wb or wb <= wa) and SequenceMatcher(None, a, b).ratio() < 0.85:
                return False
    return True


def check_bill_pages(scan: dict, pages: int) -> AppError | None:
    """Why these photos can't be read as one bill, or None. Empty values on continuation pages don't count."""
    if not scan["is_bill"]:
        return AppError(422, "This photo does not appear to be a supplier bill. Please photograph the bill "
                             "clearly, or enter the items manually.", "not_a_bill")
    if pages < 2:
        return None
    for pg in scan["pages"]:
        if not pg["is_bill"]:
            return AppError(422, "Page {{page}} does not appear to be part of a supplier bill. Please remove it and "
                                 "try again.", "bill_page_not_bill", page=pg["page"])
    gstins = {clean_gstin(pg["supplier_gstin"]) for pg in scan["pages"]} - {""}
    names = [k for pg in scan["pages"] if (k := supplier_key(pg["supplier"]))]
    if len(gstins) > 1 or not _same_supplier(names) or not scan["same_supplier"]:
        return AppError(422, "These pages appear to be from different suppliers. Please scan one bill at a time, "
                             "with all pages from the same supplier.", "bill_suppliers_differ")
    numbers = {name_key(pg["invoice_number"]) for pg in scan["pages"]} - {""}
    if len(numbers) > 1 or not scan["same_bill"]:
        return AppError(422, "These pages appear to be from different bills. Please scan one bill at a time.",
                        "bill_numbers_differ")
    return None


@router.post("/scan-invoice")
async def scan_invoice(body: InvoicePayload, user: dict = Depends(auth.require("stock"))):
    db = get_db()
    bid = user["business_id"]
    if bool(body.image_base64) == bool(body.image_paths):
        raise HTTPException(400, "Please add a photo of the bill.")
    if len(body.image_paths) > MAX_PAGES:
        raise AppError(422, "You can add up to {{n}} pages for one bill.", "too_many_pages", n=MAX_PAGES)
    for path in body.image_paths:
        if not await db.uploads.find_one({"path": path, "business_id": bid}):
            raise HTTPException(400, "A page photo could not be found. Please add it again.")
    pages = max(1, len(body.image_paths))
    await _limit_user(user)
    period = await use_quota(db, bid, pages)
    shop = await db.businesses.find_one({"id": bid}, {"_id": 0, "name": 1}) or {}

    async def read():
        if body.image_base64:
            return await ai_client.scan_invoice([ai_client.decode_b64(body.image_base64)], shop.get("name", ""))
        images = [(await run_in_threadpool(storage_client.get_object, path))[0] for path in body.image_paths]
        return await ai_client.scan_invoice(images, shop.get("name", ""), normalised=True)

    try:
        result = await _run(db, bid, pages, period, read)
    except ai_client.Truncated:
        raise AppError(422, "This bill is too long to read in one go. Please scan fewer pages at a time, "
                            "or enter the items manually.", "bill_too_long")
    logger.info("scan-invoice: pages=%s is_bill=%s reason=%s", pages, result["is_bill"], result["reason"])
    if refusal := check_bill_pages(result, pages):
        raise refusal
    for k in ("is_bill", "reason", "pages", "same_supplier", "same_bill"):
        result.pop(k)
    products, names_of = await _stock(db, bid)
    for it in result["items"]:
        match, _ = best_product(products, [it["name"]], names_of)
        it["product_id"] = match["id"] if match else None
        it["matched_name"] = match["name"] if match else None
    found = await suppliers.find_match(db, bid, result["supplier"], result["supplier_phone"], result["supplier_gstin"])
    result["supplier_match"] = suppliers.view(found) if found else None
    result["pages"] = pages
    return result
