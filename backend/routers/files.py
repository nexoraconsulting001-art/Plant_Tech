"""Photo uploads (checked, shrunk, stored per shop) and serving them.

Uploading: POST /api/upload-base64 or /api/upload, or straight to R2 with /api/uploads/presign then
/api/uploads/{id}/complete (the phone's slow upload then never holds an API worker).
- Every photo is checked and re-encoded as JPEG (ai_client.normalize_photo) and gets a 256 px WebP thumbnail.
- The optional `kind` says what it is for: "product" and "logo" go to the public store, "bill" stays private.
  Without it (1.1.0 and 1.2.0 send none) a photo is private until its use is known (_classify, on first view);
  the prepare_upload job then copies a public one to the public store, and makes thumbnails for photos stored
  before WP-7 the first time one is asked for (with Redis, worker.py must list "routers.files" in JOB_MODULES).
- Limits: UPLOAD_RATE_USER and UPLOAD_RATE_SHOP uploads, and UPLOAD_QUOTA_MB of storage per shop (0 = none),
  counted in `storage_usage`. `uploads.path` is unique (migration 0701).

Serving: GET /api/files/{path}, ?w=256 for the thumbnail. Apps load photos without a login header (an image
tag sends none), so the answer depends on where the photo is:
- public, with PUBLIC_IMAGE_BASE_URL and a separate public store: 302 to the CDN (no storage read here);
- otherwise on R2: 302 to a presigned link valid FILES_LINK_SECONDS;
- on local disk: the bytes, as before WP-7. FILES_SERVE=proxy forces this everywhere (a switch for trouble).
Private photos (supplier bills) are matched to the caller's shop when a login or a signed link (?exp=&sig=, from
GET /api/file-links/...) comes with the request: another shop gets 404. A request with neither is still served
while FILES_PRIVATE_NEED_LOGIN is off, because 1.1.0 and 1.2.0 open bill pages that way (the random path is the
key); turn it on once every app in use sends its login or a signed link for private photos.
"""
import base64
import hashlib
import hmac
import io
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from cachetools import TTLCache
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from PIL import Image
from pydantic import BaseModel, Field

import ai_client
import auth
import jobs
import ratelimit
import storage_client
from common import new_id, now_iso
from db import get_db
from errors import AppError
from settings import get_settings
from storage_client import PRIVATE, PUBLIC

router = APIRouter(prefix="/api", tags=["files"])
log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
THUMB_SIZE = 256
PUBLIC_KINDS = {"product", "logo", "library"}  # shown to customers or other shops anyway
PRIVATE_KINDS = {"bill"}  # supplier bills: prices and the supplier's details
DirectType = Literal["image/jpeg", "image/png", "image/webp"]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


FILES_SERVE = os.environ.get("FILES_SERVE", "auto").lower()  # "auto" | "proxy"
PRIVATE_NEED_LOGIN = _flag("FILES_PRIVATE_NEED_LOGIN")  # off while 1.1.0 / 1.2.0 are in use
LINK_SECONDS = int(os.environ.get("FILES_LINK_SECONDS", "600"))  # presigned and signed links
UPLOAD_RATE_USER = os.environ.get("UPLOAD_RATE_USER", "60/minute")
UPLOAD_RATE_SHOP = os.environ.get("UPLOAD_RATE_SHOP", "2000/day")
UPLOAD_QUOTA_MB = int(os.environ.get("UPLOAD_QUOTA_MB", "5120"))  # about 15,000 phone photos

_unknown = TTLCache(maxsize=20000, ttl=300)  # paths whose use was not known a moment ago: don't ask every view
_queued = TTLCache(maxsize=20000, ttl=600)  # paths already sent to prepare_upload by this process


class ImagePayload(BaseModel):
    image_base64: str = Field(max_length=MAX_IMAGE_BYTES * 4 // 3 + 64)
    kind: str | None = Field(None, max_length=20)  # newer apps: product | logo | bill (optional)


class DirectUploadIn(BaseModel):
    content_type: DirectType = "image/jpeg"
    size: int = Field(gt=0, le=MAX_IMAGE_BYTES)
    kind: str | None = Field(None, max_length=20)


# ---- helpers ----------------------------------------------------------------------------------------------

def thumb_key(path: str) -> str:
    """Where a photo's thumbnail is stored: next to it, `<id>.w256.webp`."""
    return f"{path.rsplit('.', 1)[0]}.w{THUMB_SIZE}.webp"


def make_thumbnail(data: bytes) -> bytes:
    """Shrink to fit inside 256 x 256, keeping the shape, as WebP (list rows and tiles)."""
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (THUMB_SIZE, THUMB_SIZE))  # JPEG: decode at a reduced scale, much less memory and time
    img = img.convert("RGB")
    img.thumbnail((THUMB_SIZE, THUMB_SIZE))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=75, method=4)
    return buf.getvalue()


def _visibility(kind: str | None) -> str | None:
    kind = (kind or "").strip().lower()
    return PUBLIC if kind in PUBLIC_KINDS else PRIVATE if kind in PRIVATE_KINDS else None


async def _limit(user: dict):
    await ratelimit.consume(f"upload:user:{user['id']}", UPLOAD_RATE_USER)
    await ratelimit.consume(f"upload:shop:{user['business_id']}", UPLOAD_RATE_SHOP)


async def _reserve(db, business_id: str, nbytes: int):
    """Count `nbytes` against the shop's storage, or refuse when it would go over UPLOAD_QUOTA_MB."""
    inc = {"$inc": {"bytes": nbytes, "files": 1}, "$set": {"updated_at": now_iso()}}
    if UPLOAD_QUOTA_MB <= 0:
        await db.storage_usage.update_one({"_id": business_id}, inc, upsert=True)
        return
    await db.storage_usage.update_one({"_id": business_id}, {"$setOnInsert": {"bytes": 0, "files": 0}}, upsert=True)
    room = UPLOAD_QUOTA_MB * 1024 * 1024 - nbytes
    if not await db.storage_usage.find_one_and_update({"_id": business_id, "bytes": {"$lte": room}}, inc):
        raise AppError(403, "Your shop has used its {{limit}} MB of photo storage. Please contact support to add more.",
                       code="storage_full", limit=UPLOAD_QUOTA_MB)


async def _unreserve(db, business_id: str, nbytes: int):
    await db.storage_usage.update_one({"_id": business_id}, {"$inc": {"bytes": -nbytes, "files": -1}})


async def _store(normalize, raw, user: dict, kind: str | None) -> dict:
    """Check and shrink a photo, make its thumbnail, count it, and save both in the right store."""
    db, bid = get_db(), user["business_id"]
    try:
        jpeg = await run_in_threadpool(normalize, raw)
    except ai_client.BadImage as e:
        raise HTTPException(400, str(e))
    try:
        thumb = await run_in_threadpool(make_thumbnail, jpeg)
    except Exception:  # never seen with a photo normalize accepted; the full photo is served instead
        log.warning("thumbnail failed for an upload of shop %s", bid, exc_info=True)
        thumb = None
    visibility = _visibility(kind)
    # Public photos go to the public store only when there is one; else prepare_upload moves them once it exists.
    store = PUBLIC if visibility == PUBLIC and storage_client.public_store_separate() else PRIVATE
    path = f"{storage_client.APP_NAME}/shops/{bid}/{new_id()}.jpg"
    size = len(jpeg) + len(thumb or b"")
    await _reserve(db, bid, size)
    try:
        await run_in_threadpool(storage_client.put_object, path, jpeg, "image/jpeg", store)
        if thumb:
            await run_in_threadpool(storage_client.put_object, thumb_key(path), thumb, "image/webp", store)
    except Exception:
        await _unreserve(db, bid, size)
        raise
    await db.uploads.insert_one({
        "path": path, "business_id": bid, "created_at": now_iso(), "kind": (kind or "").strip().lower() or None,
        "visibility": visibility, "store": store, "thumb": thumb_key(path) if thumb else None, "size": size,
        "user_id": user["id"]})
    return {"path": path}


async def _prepare_later(path: str):
    """Ask the background job to make a missing thumbnail and copy a public photo to the public store."""
    if path in _queued:
        return
    _queued[path] = True
    try:
        await jobs.enqueue("prepare_upload", path, _job_id=f"prepare_upload:{path}")
    except Exception:  # e.g. Redis down: the photo is still served from where it is
        log.warning("could not queue prepare_upload for %s", path, exc_info=True)
        _queued.pop(path, None)


async def _classify(db, up: dict) -> str | None:
    """Public or private for a photo uploaded without a kind (older apps, library pictures), from where it is used.
    A supplier bill wins over everything; None while nothing uses it yet (e.g. a product photo not saved yet)."""
    path, bid = up["path"], up.get("business_id")
    if path in _unknown:
        return None
    if bid is None:
        visibility = PUBLIC  # the shared picture library (routers/library.py)
    elif await db.purchases.find_one({"business_id": bid, "$or": [{"image_paths": path}, {"image_path": path}]},
                                     {"_id": 1}):
        visibility = PRIVATE
    elif (await db.products.find_one({"business_id": bid, "photo_path": path}, {"_id": 1})
          or await db.businesses.find_one({"id": bid, "logo_path": path}, {"_id": 1})):
        visibility = PUBLIC
    else:
        _unknown[path] = True
        return None
    await db.uploads.update_one({"path": path, "visibility": None}, {"$set": {"visibility": visibility}})
    up["visibility"] = visibility
    return visibility


def _signing_key() -> bytes:
    return hmac.new(get_settings().jwt_secret.encode(), b"hara-hisaab file links", hashlib.sha256).digest()


def _sign(path: str, exp: int) -> str:
    mac = hmac.new(_signing_key(), f"{path}\n{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _signature_ok(path: str, exp: int | None, sig: str) -> bool:
    return bool(exp) and exp >= time.time() and hmac.compare_digest(_sign(path, exp), sig)


async def _allowed(up: dict, visibility: str | None, authorization: str | None, exp: int | None,
                   sig: str | None) -> bool:
    """May this request see the photo? Public ones: always. Others (private, or use not known yet): a signed link
    or the shop's own login (another shop's login: no), or no proof at all while FILES_PRIVATE_NEED_LOGIN is off
    (1.1.0 and 1.2.0 send none)."""
    if visibility == PUBLIC:
        return True
    if sig:
        return _signature_ok(up["path"], exp, sig)
    if authorization:
        user = await auth.current_user(authorization)  # 401 for an expired or bad login
        return user["business_id"] == up.get("business_id")
    return not PRIVATE_NEED_LOGIN


def _cdn_url(up: dict, key: str) -> str | None:
    base = get_settings().public_image_base_url
    if (base and FILES_SERVE != "proxy" and up.get("visibility") == PUBLIC and up.get("store") == PUBLIC
            and storage_client.public_store_separate()):
        return f"{base}/{quote(key, safe='/')}"
    return None


def _pick(up: dict, w: int | None) -> str:
    """The key to send: the thumbnail when one was asked for and exists, else the photo."""
    if w and w <= THUMB_SIZE and up.get("thumb"):
        return up["thumb"]
    return up["path"]


async def _find(db, path: str) -> dict:
    up = await db.uploads.find_one({"path": path}, {"_id": 0})
    if not up:
        raise HTTPException(404, "File not found.")
    return up


async def _look(db, up: dict, w: int | None) -> str | None:
    """Its visibility (classified if unknown), queueing the thumbnail or the move to the public store if due."""
    visibility = up.get("visibility") or await _classify(db, up)
    missing_thumb = bool(w and w <= THUMB_SIZE and not up.get("thumb"))
    to_publish = visibility == PUBLIC and up.get("store") != PUBLIC and storage_client.public_store_separate()
    if missing_thumb or to_publish:
        await _prepare_later(up["path"])
    return visibility


# ---- uploading --------------------------------------------------------------------------------------------

@router.post("/upload-base64")
async def upload_base64(body: ImagePayload, user: dict = Depends(auth.current_user)):
    await _limit(user)
    return await _store(ai_client.normalize_photo_b64, body.image_base64, user, body.kind)


@router.post("/upload")
async def upload(file: UploadFile = File(...), kind: str | None = Form(None, max_length=20),
                 user: dict = Depends(auth.current_user)):
    await _limit(user)
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "This photo is too large. The maximum size is 8 MB.")
    return await _store(ai_client.normalize_photo, data, user, kind)


@router.post("/uploads/presign")
async def presign_upload(body: DirectUploadIn, user: dict = Depends(auth.current_user)):
    """A link to PUT the photo straight to storage (R2 only). Then call /uploads/{upload_id}/complete.
    Not available (409, code direct_upload_unavailable): send it to /upload-base64 as before."""
    if not storage_client.can_presign() or FILES_SERVE == "proxy":
        raise AppError(409, "Direct upload is not available. Please send the photo the usual way.",
                       code="direct_upload_unavailable")
    await _limit(user)
    upload_id, bid = new_id(), user["business_id"]
    key = f"{storage_client.APP_NAME}/incoming/{bid}/{upload_id}"  # an R2 lifecycle rule can expire this folder
    url = storage_client.presigned_put(key, body.content_type, body.size, LINK_SECONDS)  # signing only, no I/O
    await get_db().upload_intents.insert_one({
        "id": upload_id, "key": key, "business_id": bid, "user_id": user["id"], "kind": body.kind,
        "content_type": body.content_type, "size": body.size, "state": "waiting", "created_at": now_iso(),
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=LINK_SECONDS) + timedelta(days=1)})
    return {"upload_id": upload_id, "url": url, "method": "PUT", "headers": {"Content-Type": body.content_type},
            "expires_in": LINK_SECONDS}


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, user: dict = Depends(auth.current_user)):
    """Check and save a photo PUT through /uploads/presign; answers like /upload-base64. Safe to repeat."""
    db = get_db()
    mine = {"id": upload_id, "business_id": user["business_id"]}
    intent = await db.upload_intents.find_one_and_update(
        {**mine, "state": "waiting"}, {"$set": {"state": "working"}}, {"_id": 0})
    if not intent:
        done = await db.upload_intents.find_one(mine, {"_id": 0})
        if not done or done.get("state") == "failed":
            raise HTTPException(404, "Upload not found. Please start again.")
        if done.get("path"):
            return {"path": done["path"]}
        raise HTTPException(409, "This photo is still being saved. Please wait a moment.")
    key = intent["key"]
    try:
        size = await run_in_threadpool(storage_client.object_size, key)
        if size > MAX_IMAGE_BYTES:
            raise HTTPException(413, "This photo is too large. The maximum size is 8 MB.")
        raw, _ = await run_in_threadpool(storage_client.get_object, key, PRIVATE)
        result = await _store(ai_client.normalize_photo, raw, user, intent.get("kind"))
    except storage_client.NotFound:
        await db.upload_intents.update_one(mine, {"$set": {"state": "waiting"}})
        raise HTTPException(400, "The photo did not reach the server. Please try again.")
    except HTTPException as e:
        if e.status_code in (429, 403):  # rate or storage limit: may be tried again later
            await db.upload_intents.update_one(mine, {"$set": {"state": "waiting"}})
        else:
            await db.upload_intents.update_one(mine, {"$set": {"state": "failed"}})
            await run_in_threadpool(storage_client.delete_object, key)
        raise
    except Exception:
        await db.upload_intents.update_one(mine, {"$set": {"state": "waiting"}})
        raise
    await db.upload_intents.update_one(mine, {"$set": {"state": "done", "path": result["path"]}})
    try:
        await run_in_threadpool(storage_client.delete_object, key)  # the raw upload; the checked copy is saved
    except Exception:
        log.warning("could not remove the raw upload %s", key, exc_info=True)
    return result


# ---- serving ----------------------------------------------------------------------------------------------

@router.get("/files/{path:path}")
async def get_file(path: str, w: int | None = Query(None, ge=1, le=4096), exp: int | None = None,
                   sig: str | None = Query(None, max_length=64), authorization: str | None = Header(None)):
    db = get_db()
    up = await _find(db, path)
    visibility = await _look(db, up, w)
    if not await _allowed(up, visibility, authorization, exp, sig):
        raise HTTPException(404, "File not found.")
    key, store = _pick(up, w), up.get("store") or PRIVATE
    private = visibility == PRIVATE
    if cdn := _cdn_url(up, key):
        return RedirectResponse(cdn, status_code=302, headers={"Cache-Control": "public, max-age=86400"})
    if storage_client.can_presign() and FILES_SERVE != "proxy":
        url = storage_client.presigned_get(key, store, LINK_SECONDS)  # signing only, no I/O
        cache = "private, no-store" if private else f"private, max-age={max(0, LINK_SECONDS // 2)}"
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": cache})
    try:
        content, ctype = await run_in_threadpool(storage_client.get_object, key, store)
    except Exception:
        try:  # stored before WP-7 kept no store; look in both
            content, ctype = await run_in_threadpool(storage_client.get_object, key)
        except Exception:
            raise HTTPException(404, "File not found.")
    cache = "private, max-age=3600" if private else "public, max-age=31536000"
    return Response(content=content, media_type=ctype, headers={"Cache-Control": cache})


@router.get("/file-links/{path:path}")
async def file_link(path: str, request: Request, w: int | None = Query(None, ge=1, le=4096),
                    user: dict = Depends(auth.current_user)):
    """A link to one of the shop's photos that opens without a login header (e.g. a bill page in the browser):
    the CDN address for public photos, otherwise valid for FILES_LINK_SECONDS. Other shops' photos: 404."""
    db = get_db()
    up = await _find(db, path)
    if up.get("business_id") not in (None, user["business_id"]):
        raise HTTPException(404, "File not found.")
    await _look(db, up, w)
    key = _pick(up, w)
    if cdn := _cdn_url(up, key):
        return {"url": cdn, "expires_in": None}
    if storage_client.can_presign() and FILES_SERVE != "proxy":
        url = storage_client.presigned_get(key, up.get("store") or PRIVATE, LINK_SECONDS)
        return {"url": url, "expires_in": LINK_SECONDS}
    exp = int(time.time()) + LINK_SECONDS
    query = f"exp={exp}&sig={_sign(path, exp)}" + (f"&w={w}" if w else "")
    return {"url": f"{str(request.base_url).rstrip('/')}/api/files/{quote(path, safe='/')}?{query}",
            "expires_in": LINK_SECONDS}


# ---- background -------------------------------------------------------------------------------------------

@jobs.job
async def prepare_upload(ctx, path: str):
    """Give an older photo its thumbnail, and copy a photo found to be public to the public store (then the CDN
    serves it). Safe to run twice."""
    db = ctx["db"]
    up = await db.uploads.find_one({"path": path}, {"_id": 0})
    if not up:
        return
    store, update = up.get("store") or PRIVATE, {}
    if not up.get("thumb"):
        try:
            data, _ = await run_in_threadpool(storage_client.get_object, path)
            small = await run_in_threadpool(make_thumbnail, data)
            await run_in_threadpool(storage_client.put_object, thumb_key(path), small, "image/webp", store)
            update["thumb"] = thumb_key(path)
        except Exception:  # unreadable or gone: the full photo is served instead
            log.warning("thumbnail for %s failed", path, exc_info=True)
    if up.get("visibility") == PUBLIC and store != PUBLIC and storage_client.public_store_separate():
        try:
            await run_in_threadpool(storage_client.copy_to_public, path, "image/jpeg")
            if thumb := update.get("thumb") or up.get("thumb"):
                await run_in_threadpool(storage_client.copy_to_public, thumb, "image/webp")
            update["store"] = PUBLIC
        except Exception:
            log.warning("copy of %s to the public store failed", path, exc_info=True)
    if update:
        await db.uploads.update_one({"path": path}, {"$set": update})
    _queued.pop(path, None)
