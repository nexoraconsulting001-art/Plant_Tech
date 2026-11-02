"""AI: identify a plant from a photo, read a supplier bill. Limited per shop per day.

Photos that are not what the screen expects are refused with a clear message (and a `code` the app
uses to offer the right next step): a laptop in Add items, a bill in Add items, a plant on Supplier bill.
"""
import logging
import os
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

import ai_client
import auth
import storage_client
from common import date_key, name_key
from db import get_db
from errors import AppError
from routers import suppliers
from routers.products import views
from routers.purchases import MAX_PAGES
from routers.suppliers import clean_gstin, supplier_key
from search import best_product

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ImagePayload(BaseModel):
    image_base64: str = Field(max_length=MAX_IMAGE_BYTES * 4 // 3 + 64)


async def use_quota(db, business_id: str, n: int = 1):
    limit = int(os.environ.get("AI_DAILY_LIMIT", "300"))
    doc = await db.ai_usage.find_one_and_update({"business_id": business_id, "date": date_key()},
                                                {"$inc": {"count": n}}, upsert=True,
                                                return_document=ReturnDocument.AFTER)
    if doc["count"] > limit:
        raise HTTPException(429, "The daily photo scan limit has been reached. Please search by name, or try again tomorrow.")


async def _run(db, business_id: str, n: int, call):
    """Run an AI call; when Claude was never reached (unreadable photo, outage) the scans are given back."""
    try:
        return await call()
    except (ai_client.BadImage, ai_client.AIUnavailable) as e:
        await db.ai_usage.update_one({"business_id": business_id, "date": date_key()}, {"$inc": {"count": -n}})
        raise HTTPException(400 if isinstance(e, ai_client.BadImage) else 503, str(e))


@router.post("/identify-plant")
async def identify(body: ImagePayload, user: dict = Depends(auth.require("sell", "stock"))):
    db = get_db()
    bid = user["business_id"]
    await use_quota(db, bid)
    products = await db.products.find({"business_id": bid, "deleted_at": None}, {"_id": 0}).to_list(5000)
    cats = await db.categories.find({"business_id": bid}, {"_id": 0}).to_list(500)
    result = await _run(db, bid, 1, lambda: ai_client.identify_plant(
        ai_client.decode_b64(body.image_base64), [p["name"] for p in products], [c["name"] for c in cats]))
    kind = result.pop("kind")
    logger.info("identify: kind=%s reason=%s", kind, result.pop("reason"))
    if kind == "bill":
        raise AppError(422, "This looks like a supplier bill. To add stock from a bill, use Supplier bill.",
                       "looks_like_bill")
    if kind == "other":
        raise AppError(422, "This photo does not appear to show a plant or nursery product. "
                            "Please photograph the item clearly, or search by name.", "not_nursery_item")
    if kind == "unknown" or not result["plant_name"]:
        raise AppError(422, "This item could not be identified. Please retake the photo in good light, "
                            "or search by name.", "not_identified")
    names = [result["matched_catalog_name"], result["plant_name"], result["scientific_name"], *result["alternatives"]]
    matched, similar = best_product(products, names)
    result["matched_product"] = (await views(db, user, [matched]))[0] if matched else None
    result["similar_products"] = await views(db, user, similar) if not matched else []
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
    await use_quota(db, bid, pages)
    shop = await db.businesses.find_one({"id": bid}, {"_id": 0, "name": 1}) or {}

    async def read():
        if body.image_base64:
            images = [ai_client.decode_b64(body.image_base64)]
        else:
            images = [(await run_in_threadpool(storage_client.get_object, path))[0] for path in body.image_paths]
        return await ai_client.scan_invoice(images, shop.get("name", ""))

    result = await _run(db, bid, pages, read)
    logger.info("scan-invoice: pages=%s is_bill=%s reason=%s", pages, result["is_bill"], result["reason"])
    if refusal := check_bill_pages(result, pages):
        raise refusal
    for k in ("is_bill", "reason", "pages", "same_supplier", "same_bill"):
        result.pop(k)
    products = await db.products.find({"business_id": bid, "deleted_at": None}, {"_id": 0}).to_list(5000)
    for it in result["items"]:
        match, _ = best_product(products, [it["name"]])
        it["product_id"] = match["id"] if match else None
        it["matched_name"] = match["name"] if match else None
    found = await suppliers.find_match(db, bid, result["supplier"], result["supplier_phone"], result["supplier_gstin"])
    result["supplier_match"] = suppliers.view(found) if found else None
    result["pages"] = pages
    return result
