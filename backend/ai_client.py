"""Claude-powered plant identification and purchase-invoice OCR.

Both first decide what the photo shows, so a laptop is never offered as a product and a plant photo
is never read as a bill; the routes turn that verdict into a clear message for the user.

Cost and safety (docs/PRODUCTION-PLAN.md, WP-6). Every call goes through `_ask`, which:
- fails at once while the circuit breaker is open or this month's AI budget is spent (ai_guard, ai_usage);
- prepares the photos in a worker thread: JPEG, PNG or WebP only, at most MAX_IMAGE_PIXELS (a 170 MP "image
  bomb" is refused from its header, before anything is decoded), JPEGs decoded at reduced size (draft), and
  bill pages already normalised at upload sent as they are instead of being re-encoded;
- waits for a slot (a few calls at a time per process; with REDIS_URL, a limit for all processes together);
- uses one client with a 60 s timeout (5 s to connect) and 2 retries; each try may take 30 s for a plant or
  a name look-up and 120 s for a bill, and the whole call, slot wait and retries included, at most TOTAL_TIMEOUT
  (50 s for a plant, 150 s for a bill: below the app's own limits in frontend/src/api.ts). A call that runs out
  is "unavailable", so the scan is given back instead of being charged for an answer nobody waits for;
- writes one ai_calls document per call (model, tokens, cost, latency) and adds the cost to the month.

Models and effort per kind of call come from settings (CLAUDE_MODEL_PLANT / _INVOICE / _NAMES, all defaulting
to CLAUDE_MODEL, Sonnet: decision D5 keeps it until the eval set says Haiku is good enough). The server-side
refusal fallback is asked for only on models that support it (not Haiku). The fixed instructions are the
system prompt, the same bytes on every call so they can be cached; what changes per call (the photos, the
shop's categories, its name) comes after them, in the user turn.
"""
import asyncio
import base64
import io
import logging
import os
import threading
import time
import warnings
from typing import List, Literal

import anthropic
import sentry_sdk
from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps
from pydantic import BaseModel, ValidationError

import ai_guard
import ai_usage
from common import name_key
from settings import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
MODEL_NAME = _settings.claude_model  # every kind of call uses it unless CLAUDE_MODEL_<KIND> is set
EFFORT_PLANT = _settings.claude_effort_plant
EFFORT_INVOICE = _settings.claude_effort_invoice
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that take `fallbacks: "default"` on the Claude API (Haiku 5.5 has no server-side fallback).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
MAX_IMAGE_EDGE = 1568  # longest edge Claude uses without further downscaling
# Plant photos may be sent smaller (1024 is planned with Haiku, decision D5); bills keep every pixel Claude uses.
IMAGE_EDGE = {"plant": int(os.environ.get("AI_IMAGE_EDGE_PLANT") or MAX_IMAGE_EDGE), "invoice": MAX_IMAGE_EDGE}
MAX_IMAGE_PIXELS = 40_000_000  # phones send about 2-12 MP; the app shrinks photos before upload
MAX_STOCK_NAMES = 600  # products listed in a plant scan's prompt (about 6 tokens each); a nursery has 50-400
IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")
TIMEOUT = anthropic.Timeout(60.0, connect=5.0)
CALL_TIMEOUT = {"plant": 30.0, "names": 30.0, "invoice": 120.0}  # seconds per try
# Seconds for the whole call (slot wait, tries, retries). The app waits 75 s for /ai/ and 180 s for a bill
# (frontend/src/api.ts): keep these well below, so the app always gets the answer or the error.
TOTAL_TIMEOUT = {"plant": 50.0, "names": 50.0, "invoice": 150.0}
CACHE_MIN_TOKENS = 512  # a shorter system prompt is never cached (Sonnet 5.5 / Haiku 5.5 minimum)

# Pillow refuses images over twice this when opening them and warns above it; the warning is an error here.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)
# Photos are decoded a couple at a time per process: a 40 MP PNG takes about 120 MB while it is shrunk.
_DECODE_SLOTS = threading.BoundedSemaphore(max(1, int(os.environ.get("AI_DECODE_CONCURRENCY") or 2)))

_client = None


class AIUnavailable(Exception):
    """Claude could not be reached, is busy, is paused or refused our key; the scan is given back."""

    def __init__(self, message: str, code: str = "ai_unavailable", retry_after: int | None = None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class Truncated(Exception):
    """Claude's answer was cut off at max_tokens (a very long bill)."""


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=TIMEOUT, max_retries=2)  # reads ANTHROPIC_API_KEY
    return _client


NURSERY_GOODS = (
    "pots, planters, grow bags, hanging baskets, trays, soil, potting mix, compost, cocopeat, manure, "
    "fertiliser, pesticide, seeds, bulbs, pebbles, garden decor, plant stands, garden tools, watering cans "
    "and sprayers"
)
SYSTEM_PLANT = (
    "You are an expert botanist and plant nursery assistant in India. You identify plants and the "
    "products a plant nursery sells from photos taken at a nursery counter, and you say so plainly when "
    "a photo shows something else.\n\n"
    "First decide what the photo mainly shows:\n"
    "- kind: 'plant' for a living plant, sapling, cutting, seedling or bulb (potted or not); "
    f"'nursery_product' for something a plant nursery sells: {NURSERY_GOODS}; "
    "'bill' for a bill, invoice, receipt, challan or any other printed or handwritten document; "
    "'other' for anything else (people, animals, electronics, screens, food, furniture, vehicles, rooms).\n"
    "- reason: a few words explaining the kind\n"
    "If kind is 'bill' or 'other', leave every text field below empty, confidence 0 and alternatives empty.\n"
    "Otherwise identify the plant or product:\n"
    "- plant_name: common commercial name as used in Indian nurseries (for a product, a short name "
    "such as 'Terracotta Pot 8 inch')\n"
    "- scientific_name: botanical name, or empty string\n"
    "- confidence: integer 0-100\n"
    "- alternatives: up to 3 alternative common names\n"
    "- suggested_category: the best fitting category from the nursery's categories (listed after the photo), "
    "copied exactly, or empty string\n"
    "The shop's own products are listed after these instructions. Match the photo against them:\n"
    "- stock_match: if the photo shows one of the shop's products, its name copied exactly from that list (only "
    "the name, without anything in brackets); otherwise empty string. Choose a listed product only when it is the "
    "same plant (species or variety) or the same kind of product; when sizes are listed and the size is not clear, "
    "choose the most likely one and put the other sizes in stock_alternatives.\n"
    "- stock_alternatives: up to 3 other names copied exactly from that list that the photo could also show, most "
    "likely first; empty when none.\n"
    "Always prefer a listed product's name for the same plant over another name for it."
)
STOCK_HEAD = ("The shop's own products, one per line (in brackets where known: the botanical name, and after "
              "'also:' other names of the same plant or product):")
STOCK_OTHER_NAMES = 6  # other names per product line
STOCK_NAME_CHARS = 40
STOCK_NONE = "The shop has no products yet: leave stock_match and stock_alternatives empty."
SYSTEM_INVOICE = (
    "You are an OCR and data extraction assistant for a plant nursery in India. You extract supplier "
    "purchase invoice details from photos of bills. Bills may be printed or handwritten and may mix "
    "English and Hindi. You say so plainly when a photo is not a bill.\n\n"
    "The photos should be the page(s) of one supplier bill, invoice, cash memo or delivery challan.\n"
    "- is_bill: true only if they show such a document; false for plants, products, people, screens or anything else\n"
    "- reason: a few words explaining is_bill\n"
    "- pages: for each photo in order, page (1-based), is_bill, and the supplier name, supplier GSTIN and "
    "invoice number printed on that page (empty strings when that page does not show them, e.g. a continuation page)\n"
    "- same_supplier: false if the pages clearly come from different suppliers\n"
    "- same_bill: false if the pages clearly come from different bills (different invoice numbers or dates)\n"
    "If is_bill is false, leave every field below empty or 0 and items empty.\n"
    "Otherwise extract the purchase invoice details:\n"
    "- supplier (the seller's business name), supplier_phone, supplier_address, supplier_gstin, "
    "invoice_number, date (as printed): empty string if missing\n"
    "- total: grand total as a number, 0 if missing\n"
    "- items: every line item exactly once across all pages (skip subtotal, carried-forward and tax lines); "
    "purchase_price is the per-unit cost (divide the line amount by quantity if only the amount is printed)"
)
SYSTEM_NAMES = (
    "You are an expert on plants and garden products sold in Indian nurseries. You list every name people use "
    "for an item, so a shop can find it, and recognise it in a photo, under any of them.\n\n"
    f"- is_nursery_item: true if it is a plant or something a plant nursery sells ({NURSERY_GOODS}); false otherwise\n"
    "- english_name: its common English name\n"
    "- hindi_name: its common Hindi name in Devanagari, or empty string if it has none\n"
    "- hinglish: up to 5 ways people write its Hindi or regional names in Latin letters (e.g. genda, gainda)\n"
    "- aliases: up to 10 other names used in India for the same plant or product: English, trade and nursery "
    "names, and names in other Indian languages written in Latin letters; never the name of a different plant\n"
    "- botanical_name: the accepted botanical name of a plant (genus and species where known); empty for a product\n"
    "- botanical_synonyms: up to 3 older or other botanical names of the same plant (e.g. Ocimum sanctum for "
    "Ocimum tenuiflorum); empty when none"
)


class PlantId(BaseModel):
    kind: Literal["plant", "nursery_product", "bill", "other"]
    reason: str
    plant_name: str
    scientific_name: str
    confidence: int
    alternatives: List[str]
    suggested_category: str
    stock_match: str  # a name copied from the shop's product list, or "" (the route checks it is really there)
    stock_alternatives: List[str]


class NameTerms(BaseModel):
    is_nursery_item: bool
    english_name: str
    hindi_name: str
    hinglish: List[str]
    aliases: List[str]
    botanical_name: str
    botanical_synonyms: List[str]


class InvoiceItem(BaseModel):
    name: str
    quantity: float
    purchase_price: float


class BillPage(BaseModel):
    page: int
    is_bill: bool
    supplier: str
    supplier_gstin: str
    invoice_number: str


class InvoiceScan(BaseModel):
    is_bill: bool
    reason: str
    pages: List[BillPage]
    same_supplier: bool
    same_bill: bool
    supplier: str
    supplier_phone: str
    supplier_address: str
    supplier_gstin: str
    invoice_number: str
    date: str
    total: float
    items: List[InvoiceItem]


class BadImage(Exception):
    """The uploaded payload is not a readable image."""


def _open(data: bytes) -> Image.Image:
    """The image with only its header read, after the format and size checks."""
    try:
        img = Image.open(io.BytesIO(data), formats=IMAGE_FORMATS)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as e:
        raise BadImage("This photo is too large. Please take it again with the phone camera.") from e
    except Exception as e:
        raise BadImage("This photo could not be read. Please take it again.") from e
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise BadImage("This photo is too large. Please take it again with the phone camera.")
    return img


def _shrink(data: bytes, edge: int) -> bytes:
    img = _open(data)
    with _DECODE_SLOTS:
        try:
            if img.format == "JPEG":
                img.draft("RGB", (edge, edge))  # decode at 1/2, 1/4 or 1/8 size while that is still big enough
            img.load()
        except Exception as e:
            raise BadImage("This photo could not be read. Please take it again.") from e
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((edge, edge))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def normalize_photo(data: bytes) -> bytes:
    """Check the bytes are a real image; fix EXIF rotation, downscale, re-encode as JPEG.
    Blocking: call it in a thread (run_in_threadpool)."""
    return _shrink(data, MAX_IMAGE_EDGE)


def decode_b64(image_base64: str) -> bytes:
    """Base64 (a data: URL prefix is allowed) to bytes."""
    raw = image_base64.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw)
    except Exception as e:
        raise BadImage("This photo could not be read. Please take it again.") from e


def normalize_photo_b64(image_base64: str) -> bytes:
    """Like normalize_photo, for base64 input."""
    return normalize_photo(decode_b64(image_base64))


def square_thumbnail(jpeg: bytes, size: int = 800) -> bytes:
    """Centre-crop to a square and shrink, for the shared picture library."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def fit_thumbnail(jpeg: bytes, size: int = 160) -> bytes:
    """Shrink to fit inside size x size, keeping the shape (shop logos are often wide)."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def prepare_image(data: bytes, edge: int = MAX_IMAGE_EDGE, normalised: bool = False) -> bytes:
    """JPEG bytes for Claude (blocking). `normalised`: the photo went through normalize_photo at upload, so a
    JPEG that already fits is sent unchanged, not re-encoded; anything else is checked and shrunk."""
    if normalised:
        img = _open(data)
        if img.format == "JPEG" and img.mode == "RGB" and max(img.size) <= edge:
            return data
    return _shrink(data, edge)


def _image_block(jpeg: bytes) -> dict:
    data = base64.standard_b64encode(jpeg).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _system(text: str, context: str | None = None):
    """The system prompt, marked for caching once it is long enough to be cached (about 4 characters a token);
    below the model's minimum the mark would do nothing. `context` (a shop's product list) is a second block after
    the fixed instructions, so the instructions stay the same bytes for every shop and the mark at the end caches
    instructions + list for that shop's next scans (5 minutes)."""
    min_tokens = int(os.environ.get("AI_CACHE_MIN_TOKENS") or CACHE_MIN_TOKENS)
    if context is None:
        if len(text) // 4 >= min_tokens:
            return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        return text
    blocks = [{"type": "text", "text": text}, {"type": "text", "text": context}]
    if (len(text) + len(context)) // 4 >= min_tokens:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


_FORMATS: dict[type, dict] = {}


def _output_format(schema: type[BaseModel]) -> dict:
    """output_config.format for `schema`: what messages.parse() sends. We parse the answer ourselves so that
    the usage of an answer that does not parse is still recorded."""
    if schema not in _FORMATS:
        _FORMATS[schema] = {"type": "json_schema", "schema": anthropic.transform_schema(schema.model_json_schema())}
    return _FORMATS[schema]


def _alert(e: Exception):
    """To Sentry, loudly: our key, billing, permissions or model setting is wrong, so scans fail until fixed."""
    with sentry_sdk.new_scope() as scope:
        scope.set_level("fatal")
        scope.set_tag("ai_error", getattr(e, "type", None) or getattr(e, "status_code", "unknown"))
        sentry_sdk.capture_exception(e)


async def _ask(kind: str, system: str, prompt: str, images: list[bytes], schema, effort: str, max_tokens: int,
               normalised: bool = False, context: str | None = None):
    """Send photos (pages of one bill are labelled) + prompt; the parsed schema object, or None on a refusal or
    an answer that does not fit the schema. Raises Truncated at max_tokens, AIUnavailable when Claude could not
    be used (the scan is given back) and BadImage for a photo that cannot be read."""
    breaker = ai_guard.breaker()
    try:
        breaker.check()
    except ai_guard.Open:
        raise AIUnavailable("Photo recognition is unavailable right now. Please try again.",
                            retry_after=breaker.reset_after) from None
    if await ai_usage.over_budget():
        logger.error("AI_MONTHLY_BUDGET_USD reached: refusing a %s call", kind)
        raise AIUnavailable("Photo recognition is paused for now. Please search by name, or enter the items manually.",
                            code="ai_budget_reached")
    edge = IMAGE_EDGE.get(kind, MAX_IMAGE_EDGE)
    content: list[dict] = []
    for i, data in enumerate(images, 1):
        if len(images) > 1:
            content.append({"type": "text", "text": f"Page {i} of {len(images)}"})
        content.append(_image_block(await run_in_threadpool(prepare_image, data, edge, normalised)))
    content.append({"type": "text", "text": prompt})

    model = _settings.model_for(kind)
    params = dict(model=model, max_tokens=max_tokens, system=_system(system, context),
                  messages=[{"role": "user", "content": content}],
                  output_config={"effort": effort, "format": _output_format(schema)},
                  timeout=anthropic.Timeout(CALL_TIMEOUT.get(kind, 60.0), connect=5.0))
    if model in FALLBACK_MODELS:
        params.update(betas=[FALLBACK_BETA], fallbacks="default")
    started = time.perf_counter()
    resp, worked = None, None  # worked: True / False = Claude answered / failed; None = no verdict (busy, cancelled)

    async def note(outcome: str, error: str | None = None):
        await ai_usage.record(kind, model, resp, outcome, (time.perf_counter() - started) * 1000, len(images), error)

    try:
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT.get(kind, 90.0)):
                async with ai_guard.slot():
                    resp = await _get_client().beta.messages.create(**params)
        except TimeoutError:  # the whole call: the app would have given up (and the user tapped again)
            logger.error("Claude %s call took over %ss", kind, TOTAL_TIMEOUT.get(kind, 90.0))
            worked = False
            await note("timeout", "total_timeout")
            raise AIUnavailable("Photo recognition is taking too long. Please try again.", code="ai_timeout") from None
        except ai_guard.Busy:
            raise AIUnavailable("Photo recognition is busy. Please try again in a moment.", code="ai_busy",
                                retry_after=5) from None
        except anthropic.RateLimitError as e:
            logger.warning("Claude rate limited: %s", e)
            await note("rate_limited", "rate_limit_error")
            raise AIUnavailable("Photo recognition is busy. Please try again in a moment.", code="ai_busy",
                                retry_after=10) from e
        except anthropic.APIConnectionError as e:  # timeouts too
            logger.error("Claude connection error: %s", e)
            worked = False
            await note("unreachable", type(e).__name__)
            raise AIUnavailable("Could not reach the photo recognition service. Please check your internet connection.") from e
        except anthropic.APIStatusError as e:
            logger.error("Claude API error %s: %s", e.status_code, e)
            worked = False
            await note("error", getattr(e, "type", None) or str(e.status_code))
            if e.status_code < 500:  # our key, billing, permissions, model name or request: retrying will not help
                _alert(e)
            raise AIUnavailable("Photo recognition is unavailable right now. Please try again.") from e
        worked = True
    finally:
        if worked is True:
            breaker.success()
        elif worked is False:
            breaker.failure()
        else:
            breaker.release()

    if resp.stop_reason == "refusal":
        logger.warning("Claude declined: %s", resp.stop_details)
        await note("refusal")
        return None
    if resp.stop_reason == "max_tokens":
        logger.warning("Claude answer cut off at max_tokens=%s (%s)", max_tokens, kind)
        await note("max_tokens")
        raise Truncated()
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        parsed = schema.model_validate_json(text)
    except ValidationError as e:  # e.g. refusal text without the refusal stop reason
        logger.warning("Claude output not parseable: %s", e)
        await note("unparseable")
        return None
    await note("ok")
    return parsed


def _stock_line(p: dict) -> str:
    """"- Tulsi (Ocimum tenuiflorum; also: Holy Basil, तुलसी, tulasi)": the name, the botanical name, and a few other
    names of the same plant (`also`: the product's own other names first, then its shared entry's)."""
    name = p["name"].strip()
    botanical = (p.get("botanical_name") or "").strip()
    seen = {name_key(name), name_key(botanical)}
    also = []
    for n in p.get("also") or []:
        n = (n or "").strip()[:STOCK_NAME_CHARS].strip()
        if n and name_key(n) not in seen:
            seen.add(name_key(n))
            also.append(n)
    inside = "; ".join(x for x in (botanical, ("also: " + ", ".join(also[:STOCK_OTHER_NAMES])) if also else "") if x)
    return f"- {name}" + (f" ({inside})" if inside else "")


def stock_context(stock: list[dict] | None) -> str:
    """The shop's products for the prompt: one line each (_stock_line), sorted, so the same stock gives the same
    bytes (cached) and the same list order for Claude on every scan."""
    lines = sorted({_stock_line(p) for p in stock or [] if (p.get("name") or "").strip()}, key=str.lower)
    if not lines:
        return STOCK_NONE
    return STOCK_HEAD + "\n" + "\n".join(lines[:MAX_STOCK_NAMES])


async def identify_plant(image: bytes, categories: list[str] | None = None, stock: list[dict] | None = None) -> dict:
    """What the photo shows; for a plant or nursery product, its name and category, and which of the shop's own
    products (`stock`: dicts with name and botanical_name) it is.

    kind is "plant", "nursery_product", "bill", "other", or "unknown" when Claude gave no usable answer.
    Claude cannot be made fully repeatable (Sonnet 5.5 takes no temperature), so it chooses from the shop's own
    product list instead of naming freely: the same plant then comes back as the same product. The route checks
    that what it chose is really one of the products.
    """
    categories = categories or []
    category_text = ", ".join(categories) if categories else "none"
    prompt = f"The nursery's product categories are: {category_text}."
    try:
        data = await _ask("plant", SYSTEM_PLANT, prompt, [image], PlantId, EFFORT_PLANT, 2000,
                          context=stock_context(stock))
    except Truncated:
        data = None
    if data is None:
        return {"kind": "unknown", "reason": "no usable answer", "plant_name": "", "scientific_name": "",
                "confidence": 0, "confidence_label": "Low", "alternatives": [], "matched_catalog_name": "",
                "suggested_category": "", "stock_match": "", "stock_alternatives": []}
    category_by_lower = {c.lower(): c for c in categories}
    conf = max(0, min(100, data.confidence))
    label = "High" if conf >= 80 else ("Medium" if conf >= 50 else "Low")
    return {
        "kind": data.kind,
        "reason": data.reason,
        "plant_name": data.plant_name.strip(),
        "scientific_name": data.scientific_name.strip(),
        "confidence": conf,
        "confidence_label": label,
        "alternatives": [a.strip() for a in data.alternatives[:3] if a.strip()],
        "matched_catalog_name": "",  # the route fills in the matched product's name (kept for older apps)
        "suggested_category": category_by_lower.get(data.suggested_category.strip().lower(), ""),
        "stock_match": data.stock_match.strip(),
        "stock_alternatives": [a.strip() for a in data.stock_alternatives[:3] if a.strip()],
    }


async def scan_invoice(images: list[bytes], shop_name: str = "", normalised: bool = False) -> dict:
    """Read a supplier bill from one or more photos of its pages (`normalised`: pages uploaded through
    /api/upload, already shrunk to JPEG).

    is_bill is False when the photos are not a bill (or Claude gave no usable answer); per page, what
    each page shows, so the route can refuse pages of different suppliers or different bills.
    Raises Truncated when the answer did not fit in max_tokens.
    """
    n = len(images)
    buyer = f"The buyer is the nursery {shop_name!r}; never report it as the supplier." if shop_name.strip() else ""
    prompt = f"These {n} photo(s) should be the page(s) of one supplier bill.\n{buyer}".strip()
    data = await _ask("invoice", SYSTEM_INVOICE, prompt, images, InvoiceScan, EFFORT_INVOICE,
                      16000 if n > 1 else 8000, normalised)
    if data is None:
        data = InvoiceScan(is_bill=False, reason="no usable answer", pages=[], same_supplier=True, same_bill=True,
                           supplier="", supplier_phone="", supplier_address="", supplier_gstin="", invoice_number="",
                           date="", total=0, items=[])
    return {
        "is_bill": data.is_bill,
        "reason": data.reason,
        "pages": [pg.model_dump() for pg in data.pages],
        "same_supplier": data.same_supplier,
        "same_bill": data.same_bill,
        "supplier": data.supplier.strip(),
        "supplier_phone": data.supplier_phone.strip(),
        "supplier_address": data.supplier_address.strip(),
        "supplier_gstin": data.supplier_gstin.strip(),
        "invoice_number": data.invoice_number.strip(),
        "date": data.date.strip(),
        "total": data.total,
        "items": [
            {"name": it.name.strip(), "quantity": it.quantity, "purchase_price": it.purchase_price}
            for it in data.items if it.name.strip()
        ],
    }


async def name_terms(name: str, category: str = "", botanical: str = "") -> NameTerms | None:
    """Every name of an item a nursery sells (search in any language, and the plant scan's stock list), with its
    botanical names; None if Claude gave no usable answer."""
    known = f" (botanical name {botanical!r})" if botanical.strip() else ""
    where = f" in its category {category!r}" if category.strip() else ""
    prompt = f"An Indian plant nursery sells an item called {name!r}{known}{where}."
    try:
        return await _ask("names", SYSTEM_NAMES, prompt, [], NameTerms, "low", 1200)
    except Truncated:
        return None
