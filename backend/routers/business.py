"""The shop (setup wizard and settings) and its categories, each with a GST %.

Prices are typed tax-inclusive. GST only applies when the shop is GST registered; then each
category's GST % decides how much of a line's price is tax.

UPI: a shop can have several UPI IDs (`upi_ids`, each {id, upi_id, label}); `default_upi` is the id of the one the
pay QR starts with. `upi_id` stays the default's UPI ID for app versions that know only one, and their PUT with
`upi_id` changes (or, when empty, removes) the default. Shops saved before the list get it on first read.
`whatsapp_bill` (on unless the owner turned it off): the app opens WhatsApp with the bill as soon as it is made.

GST (rules in gst.py; the tax invoice in invoice_html.py):
- Tax profile: `gst_registered` plus `gst_scheme` ("regular" when missing, or "composition"). The state comes from
  the GSTIN. Turning GST on, or changing the GSTIN, needs a valid GSTIN (mod-36 check). A shop saved before this
  check, with a missing or invalid one, still saves its other settings; GET says so in `gstin_status` and
  `gst_warning`, so the app can ask the owner to fix it.
- Categories have an HSN/SAC code (`hsn`, a suggestion from the name when not set) and `tax_exempt`
  (exempt goods, e.g. live plants: always 0% GST). A product can have its own code: /products/{id}/tax.
- Every change to the tax profile, a category's tax or a product's HSN goes to the audit log.
- `logo_path` must be one of the shop's own uploads.
"""
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
import gst
from common import name_key, new_id, now_iso
from db import get_db
from errors import AppError

router = APIRouter(prefix="/api", tags=["business"])

# Suggestions shown in setup; the owner confirms real rates with their accountant. HSN codes: gst.suggest_hsn.
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
    gst_scheme: Literal["regular", "composition"] | None = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    gst_percent: float = 0
    hsn: str | None = Field(None, max_length=12)  # missing: the suggestion for the name; "": none
    tax_exempt: bool | None = None  # missing: the suggestion for the name (only when its GST is 0%)


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    gst_percent: float | None = None
    hsn: str | None = Field(None, max_length=12)
    tax_exempt: bool | None = None


class ProductTaxIn(BaseModel):
    hsn: str = Field("", max_length=12)  # "": use the category's code


class CategoriesBulk(BaseModel):
    items: list[CategoryIn]


def _check_gst(value: float | None):
    if value is not None and value not in GST_SLABS:
        raise AppError(400, "GST must be one of {{rates}}.", "bad_gst", rates=", ".join(f"{s:g}%" for s in GST_SLABS))


def _check_hsn(value: str | None) -> str | None:
    """A typed HSN/SAC code without spaces; "" clears it. None: not sent."""
    if value is None:
        return None
    code = re.sub(r"\s+", "", value)
    if code and not gst.check_hsn(code):
        raise AppError(400, "{{hsn}} is not a valid HSN or SAC code. It has 4, 6 or 8 digits.", "bad_hsn", hsn=code)
    return code


def _exempt(sent: bool | None, gst_percent: float, suggested: bool) -> bool:
    """Exempt goods carry no GST: exempt with a GST rate is refused; a GST rate above 0 ends the exemption."""
    if sent and gst_percent:
        raise HTTPException(400, "An exempt category has 0% GST. Set its GST to 0% or turn off Exempt.")
    if gst_percent:
        return False
    return suggested if sent is None else sent


# Shown by GET /business for a shop saved before the GSTIN check (the app shows it, translated).
GST_WARNINGS = {
    "missing": "Your shop is GST registered but has no GST number. Please add it in Shop details.",
    "invalid": "The GST number saved for your shop is not valid. Please correct it in Shop details.",
}


def gstin_status(biz: dict) -> str:
    """"not_registered", "ok", "missing" or "invalid"."""
    if not biz.get("gst_registered"):
        return "not_registered"
    problem = gst.gstin_problem(biz.get("gstin"))
    return "ok" if problem is None else ("missing" if problem == "missing" else "invalid")


def _check_tax_profile(update: dict, current: dict):
    """A valid GSTIN is needed to turn GST on or to save a new GSTIN. What a shop already saved is never refused,
    so a shop with a missing or invalid GSTIN from before this check can still save its other settings."""
    if "gstin" in update:
        update["gstin"] = gst.clean_gstin(update["gstin"])
    registered = update.get("gst_registered", current.get("gst_registered"))
    turning_on = bool(update.get("gst_registered")) and not current.get("gst_registered")
    new_gstin = "gstin" in update and update["gstin"] != (current.get("gstin") or "")
    if not registered or not (turning_on or new_gstin):
        return
    gstin = update.get("gstin", current.get("gstin") or "")
    if (problem := gst.gstin_problem(gstin)) == "missing":
        raise AppError(400, "Please enter your shop's GST number.", "gstin_missing")
    if problem:
        raise AppError(400, "{{gstin}} is not a valid GST number. Please check it on your GST certificate.", "bad_gstin",
                       gstin=gstin)


async def _check_logo(db, business_id: str, update: dict, current: dict):
    """The logo must be a photo this shop uploaded (an empty one removes it; the saved one stays allowed)."""
    path = update.get("logo_path")
    if not path or path == current.get("logo_path"):
        return
    if not await db.uploads.find_one({"path": path, "business_id": business_id}, {"_id": 1}):
        raise HTTPException(400, "Please upload the logo again.")


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
    status = gstin_status(biz)
    state = gst.state_of(biz.get("gstin")) if biz.get("gst_registered") else ""
    return {**biz, **shop_upis(biz), "whatsapp_bill": biz.get("whatsapp_bill", True),
            "gst_scheme": gst.scheme(biz) if biz.get("gst_registered") else "", "gstin_status": status,
            "gst_warning": GST_WARNINGS.get(status, ""), "gst_state_code": state, "gst_state": gst.STATES.get(state, "")}


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
    if current is None:  # the shop was erased (a login can stay cached for USER_CACHE_SECONDS after that)
        raise HTTPException(404, "Shop not found.")
    data = body.model_dump(exclude_none=True, exclude={"upi_ids", "default_upi"})
    update = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
    if body.upi_ids is not None:  # the list wins over a single upi_id
        update.pop("upi_id", None)
        update.update(_upi_list(body.upi_ids, body.default_upi, current))
    elif "upi_id" in update:
        update.update(_upi_single(update["upi_id"], current))
    elif body.default_upi is not None:
        update.update(upi_fields(current["upi_ids"], body.default_upi))
    _check_tax_profile(update, current)
    await _check_logo(db, owner["business_id"], update, current)
    if update.get("onboarding_done") and not (update.get("name") or current.get("name")):
        raise HTTPException(400, "Please enter the shop name first.")
    update["updated_at"] = now_iso()
    await db.businesses.update_one({"id": owner["business_id"]}, {"$set": update})
    changed = {k: update[k] for k in ("gst_registered", "gstin", "gst_scheme") if k in update and update[k] != current.get(k)}
    if changed:  # which document a bill must be depends on these, so keep when they changed
        await audit.log(db, owner, "business.tax_profile", {"type": "business", "id": owner["business_id"]},
                        {"from": {k: current.get(k) for k in changed}, "to": changed})
    return _with_settings(await db.businesses.find_one({"id": owner["business_id"]}, {"_id": 0}))


@router.get("/categories/suggestions")
async def category_suggestions(user: dict = Depends(auth.current_user)):
    out = []
    for n, g in SUGGESTED_CATEGORIES:
        hsn, exempt = gst.suggest_hsn(n)
        out.append({"name": n, "gst_percent": g, "hsn": hsn, "tax_exempt": exempt and not g})
    return out


def _category_view(c: dict) -> dict:
    """Categories saved before HSN codes show the suggested code (until the owner sets one)."""
    return {**c, "hsn": gst.category_hsn(c), "tax_exempt": bool(c.get("tax_exempt"))}


@router.get("/categories")
async def list_categories(user: dict = Depends(auth.current_user)):
    """With how many products each has, in one query (the shop's products that are not deleted)."""
    bid = user["business_id"]
    cursor = await get_db().categories.aggregate([
        {"$match": {"business_id": bid}},
        {"$sort": {"name": 1}},
        {"$limit": 500},
        {"$lookup": {"from": "products", "localField": "id", "foreignField": "category_id", "as": "n",
                     "pipeline": [{"$match": {"business_id": bid, "deleted_at": None}}, {"$count": "n"}]}},
        {"$addFields": {"product_count": {"$ifNull": [{"$first": "$n.n"}, 0]}}},
        {"$project": {"_id": 0, "n": 0}},
    ])
    cats = await cursor.to_list(500)
    return [_category_view(c) for c in cats]


async def _create_category(db, business_id: str, name: str, gst_percent: float, hsn: str | None = None,
                           tax_exempt: bool | None = None) -> dict:
    existing = await db.categories.find_one({"business_id": business_id, "name_key": name_key(name)}, {"_id": 0})
    if existing:
        return _category_view(existing)
    suggested_hsn, suggested_exempt = gst.suggest_hsn(name)
    doc = {"id": new_id(), "business_id": business_id, "name": name.strip(), "name_key": name_key(name),
           "gst_percent": gst_percent, "hsn": suggested_hsn if hsn is None else hsn,
           "tax_exempt": _exempt(tax_exempt, gst_percent, suggested_exempt), "created_at": now_iso()}
    await db.categories.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.post("/categories")
async def create_category(body: CategoryIn, user: dict = Depends(auth.require("owner", "stock"))):
    _check_gst(body.gst_percent)
    hsn = _check_hsn(body.hsn)
    if not auth.can(user, "owner"):  # only the owner sets tax; others get the suggestions
        return await _create_category(get_db(), user["business_id"], body.name, 0)
    return await _create_category(get_db(), user["business_id"], body.name, body.gst_percent, hsn, body.tax_exempt)


@router.post("/categories/bulk")
async def create_categories(body: CategoriesBulk, owner: dict = Depends(auth.require("owner"))):
    """Setup wizard: add the chosen categories at once (existing names are kept as they are)."""
    for item in body.items:
        _check_gst(item.gst_percent)
        item.hsn = _check_hsn(item.hsn)
    db = get_db()
    return [await _create_category(db, owner["business_id"], i.name, i.gst_percent, i.hsn, i.tax_exempt)
            for i in body.items]


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
    if (hsn := _check_hsn(body.hsn)) is not None:
        update["hsn"] = hsn
    if body.gst_percent is not None or body.tax_exempt is not None:
        rate = update.get("gst_percent", cat.get("gst_percent", 0))
        update["tax_exempt"] = _exempt(body.tax_exempt, rate, bool(cat.get("tax_exempt")))
    tax = {k: update[k] for k in ("gst_percent", "hsn", "tax_exempt") if k in update and update[k] != cat.get(k)}
    if update:
        await db.categories.update_one({"id": cat_id}, {"$set": update})
    if tax:  # later bills are taxed by these, so keep when they changed
        await audit.log(db, owner, "category.tax", {"type": "category", "id": cat_id},
                        {"from": {k: cat.get(k) for k in tax}, "to": tax})
    return _category_view({**cat, **update})


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


# ---- A product's own HSN code (when it differs from its category's) ----

async def _product_tax(db, business_id: str, product_id: str) -> tuple[dict, dict]:
    product = await db.products.find_one({"id": product_id, "business_id": business_id, "deleted_at": None},
                                         {"_id": 0, "id": 1, "hsn": 1, "category_id": 1})
    if not product:
        raise HTTPException(404, "Product not found.")
    cat = None
    if product.get("category_id"):
        cat = await db.categories.find_one({"id": product["category_id"], "business_id": business_id}, {"_id": 0})
    return product, cat or {}


def _tax_view(product: dict, cat: dict) -> dict:
    """`hsn`: the product's own code ("" when it uses its category's); `effective_hsn`: what its bills show."""
    return {"product_id": product["id"], "hsn": product.get("hsn") or "", "category_hsn": gst.category_hsn(cat),
            "effective_hsn": gst.line_hsn(product, cat), "tax_exempt": bool(cat.get("tax_exempt")),
            "gst_percent": cat.get("gst_percent", 0)}


@router.get("/products/{product_id}/tax")
async def product_tax(product_id: str, user: dict = Depends(auth.current_user)):
    return _tax_view(*await _product_tax(get_db(), user["business_id"], product_id))


@router.put("/products/{product_id}/tax")
async def set_product_tax(product_id: str, body: ProductTaxIn, owner: dict = Depends(auth.require("owner"))):
    """The owner sets a product's own HSN code; an empty one goes back to the category's."""
    db = get_db()
    product, cat = await _product_tax(db, owner["business_id"], product_id)
    hsn = _check_hsn(body.hsn) or ""
    if hsn != (product.get("hsn") or ""):
        await db.products.update_one({"id": product_id, "business_id": owner["business_id"]}, {"$set": {"hsn": hsn}})
        await audit.log(db, owner, "product.hsn", {"type": "product", "id": product_id},
                        {"from": product.get("hsn") or "", "to": hsn})
    return _tax_view({**product, "hsn": hsn}, cat)
