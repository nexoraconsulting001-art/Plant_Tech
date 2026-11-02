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
  a name look-up and 120 s for a bill;
- writes one ai_calls document per call (model, tokens, cost, latency) and adds the cost to the month.

Models and effort per kind of call come from settings (CLAUDE_MODEL_PLANT / _INVOICE / _NAMES, all defaulting
to CLAUDE_MODEL, Sonnet: decision D5 keeps it until the eval set says Haiku is good enough). The server-side
refusal fallback is asked for only on models that support it (not Haiku). The fixed instructions are the
system prompt, the same bytes on every call so they can be cached; what changes per call (the photos, the
shop's categories, its name) comes after them, in the user turn.
"""
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
IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")
TIMEOUT = anthropic.Timeout(60.0, connect=5.0)
CALL_TIMEOUT = {"plant": 30.0, "names": 30.0, "invoice": 120.0}  # seconds per try
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
    "copied exactly, or empty string"
)
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
    "You are an expert on plants and garden products sold in Indian nurseries. You give the names people use "
    "for an item in English and in Hindi (Devanagari and Hinglish).\n\n"
    f"- is_nursery_item: true if it is a plant or something a plant nursery sells ({NURSERY_GOODS}); false otherwise\n"
    "- english_name: its common English name\n"
    "- hindi_name: its common Hindi name in Devanagari, or empty string if it has none\n"
    "- hinglish: up to 3 ways people write the Hindi name in Latin letters\n"
    "- aliases: up to 3 other common names (English or regional)"
)


class PlantId(BaseModel):
    kind: Literal["plant", "nursery_product", "bill", "other"]
    reason: str
    plant_name: str
    scientific_name: str
    confidence: int
    alternatives: List[str]
    suggested_category: str


class NameTerms(BaseModel):
    is_nursery_item: bool
    english_name: str
    hindi_name: str
    hinglish: List[str]
    aliases: List[str]


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


def _system(text: str):
    """The system prompt, marked for caching once it is long enough to be cached (about 4 characters a token);
    below the model's minimum the mark would do nothing."""
    min_tokens = int(os.environ.get("AI_CACHE_MIN_TOKENS") or CACHE_MIN_TOKENS)
    if len(text) // 4 >= min_tokens:
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    return text


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
               normalised: bool = False):
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
    params = dict(model=model, max_tokens=max_tokens, system=_system(system),
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
            async with ai_guard.slot():
                resp = await _get_client().beta.messages.create(**params)
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


async def identify_plant(image: bytes, categories: list[str] | None = None) -> dict:
    """What the photo shows; for a plant or nursery product, its name and category.

    kind is "plant", "nursery_product", "bill", "other", or "unknown" when Claude gave no usable answer.
    The shop's products are not sent (they cost tokens on every call); the route matches the names Claude gives.
    """
    categories = categories or []
    category_text = ", ".join(categories) if categories else "none"
    prompt = f"The nursery's product categories are: {category_text}."
    try:
        data = await _ask("plant", SYSTEM_PLANT, prompt, [image], PlantId, EFFORT_PLANT, 2000)
    except Truncated:
        data = None
    if data is None:
        return {"kind": "unknown", "reason": "no usable answer", "plant_name": "", "scientific_name": "",
                "confidence": 0, "confidence_label": "Low", "alternatives": [], "matched_catalog_name": "",
                "suggested_category": ""}
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


async def name_terms(name: str) -> NameTerms | None:
    """The other names of a product a shop typed (for search in any language); None if Claude gave no usable answer."""
    prompt = f"An Indian plant nursery added a product called {name!r}."
    try:
        return await _ask("names", SYSTEM_NAMES, prompt, [], NameTerms, "low", 600)
    except Truncated:
        return None
