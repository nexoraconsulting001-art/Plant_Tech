"""The shop's data, for its CA and for the shop to take away: GST returns, a full export, the audit log. Owner only.

    GET  /api/exports/gstr1?month=2026-10                 GSTR-1 JSON for the GST portal / offline tool (gstr.py)
    GET  /api/exports/gstr1?month=2026-10&format=csv      the offline tool's sheets as CSV files in a zip
                                    &section=b2cs         ... or one sheet as a CSV
    GET  /api/exports/gstr3b?month=2026-10                GSTR-3B tables 3.1 and 3.2, with notes for the CA
    POST /api/exports/shop                                start a full export (a zip of everything; a worker job)
    GET  /api/exports/shop                                the latest exports
    GET  /api/exports/shop/{id}                           one export; when ready, `url` is a short-lived link
    GET  /api/audit-log                                   who did what in the shop, newest first (audit.py)

The full export is built by the worker (jobs.enqueue), stored privately under <prefix>/exports/ (EXPORT_BUCKET, or
the R2 bucket; never served by /api/files), and handed out as a presigned R2 link valid for EXPORT_LINK_MINUTES.
On a laptop (local storage) the link points back here with a signed token instead. After EXPORT_KEEP_HOURS the file
is removed (`purge_exports`, a daily worker job) and the record goes 30 days later (TTL on `delete_after`).
Each export and GST download is written to the audit log.
"""
import asyncio
import csv
import io
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pymongo import ReturnDocument

import audit
import auth
import gst
import gstr
import jobs
import ratelimit
import storage_client
from common import IST, csv_text, new_id
from db import get_db
from errors import AppError

router = APIRouter(prefix="/api", tags=["exports"])
log = logging.getLogger(__name__)

MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
EXPORT_RATE = "5/day"  # full exports per shop
RUNNING_FOR = timedelta(hours=1)  # an export still queued or running after this no longer blocks a new one
KEEP_FOR_DAYS = 30  # the record (not the file) after it expires
TOKEN_AUDIENCE = "hh-export"
# Each shop's collections in the export: file name, collection, fields (None = all but _id)
USER_FIELDS = ("id", "name", "phone", "email", "role", "permissions", "active", "created_at", "last_login_at")
PARTS = [
    ("categories", "categories", None), ("products", "products", None), ("customers", "customers", None),
    ("invoices", "invoices", None), ("purchases", "purchases", None), ("suppliers", "suppliers", None),
    ("stock_history", "stock_history", None), ("users", "users", USER_FIELDS), ("photos", "uploads", None),
    ("ai_usage", "ai_usage", None),
]
BILL_COLUMNS = ("invoice_number", "date_key", "created_at", "status", "customer_name", "customer_phone",
                "payment_method", "subtotal", "discount", "gst_total", "grand_total")
TEXT_COLUMNS = {"customer_name"}  # typed by a person: escaped for spreadsheets (common.csv_text); the rest the server makes


def _minutes(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def link_seconds() -> int:
    return _minutes("EXPORT_LINK_MINUTES", 15) * 60


def keep_hours() -> int:
    return _minutes("EXPORT_KEEP_HOURS", 72)


def _bucket() -> str:
    return os.environ.get("EXPORT_BUCKET", "").strip() or storage_client.R2_BUCKET


# ---- GST returns ----

async def _gst_month(owner: dict, month: str) -> gstr.Month:
    db = get_db()
    biz = await db.businesses.find_one({"id": owner["business_id"]}, {"_id": 0}) or {}
    if not biz.get("gst_registered") or not gstr.state_of(biz.get("gstin")):
        raise AppError(400, "GST returns need the shop's GSTIN. Please add it in Shop details.", "no_gstin")
    if gst.scheme(biz) == "composition":  # no GSTR-1 or 3B: CMP-08 and GSTR-4, which this app does not make yet
        raise AppError(400, "A composition shop files CMP-08 and GSTR-4, not GSTR-1 or GSTR-3B. Please ask your CA.",
                       "gst_composition")
    return await gstr.collect(db, biz, month)


def _download(content: bytes | str, filename: str, media_type: str) -> Response:
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})


@router.get("/exports/gstr1")
async def gstr1(month: str = Query(..., pattern=MONTH), format: str = Query("json", pattern="^(json|csv)$"),
                section: str = Query("", pattern=f"^({'|'.join(gstr.SHEETS)})?$"),
                owner: dict = Depends(auth.require("owner"))):
    m = await _gst_month(owner, month)
    await audit.log(get_db(), owner, "export.gstr1", {"type": "month", "id": month}, {"format": format, "section": section})
    base = f"GSTR1_{m.gstin}_{gstr.fp(month)}"
    if format == "json":
        body = json.dumps(gstr.gstr1_json(m), ensure_ascii=False, separators=(",", ":"))
        return _download(body, f"{base}.json", "application/json")
    sheets = gstr.gstr1_csv(m)
    if section:
        return _download(sheets[section], f"{base}_{section}.csv", "text/csv; charset=utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in sheets.items():
            zf.writestr(f"{name}.csv", text)
    return _download(buf.getvalue(), f"{base}_csv.zip", "application/zip")


@router.get("/exports/gstr3b")
async def gstr3b(month: str = Query(..., pattern=MONTH), owner: dict = Depends(auth.require("owner"))):
    m = await _gst_month(owner, month)
    await audit.log(get_db(), owner, "export.gstr3b", {"type": "month", "id": month})
    return gstr.gstr3b(m)


# ---- the full shop export ----

def _iso(at) -> str | None:
    return _aware(at).isoformat() if isinstance(at, datetime) else at


def _shown(doc: dict, url: str | None = None) -> dict:
    out = {k: _iso(doc.get(k)) for k in ("id", "status", "created_at", "finished_at", "expires_at", "size", "counts",
                                         "error") if doc.get(k) is not None}
    if url:
        out |= {"url": url, "url_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=link_seconds())).isoformat()}
    return out


@router.post("/exports/shop")
async def start_shop_export(owner: dict = Depends(auth.require("owner"))):
    """Starts a full export; the app polls GET /api/exports/shop/{id} until `status` is "ready" (or "failed")."""
    db, bid = get_db(), owner["business_id"]
    now = datetime.now(timezone.utc)
    if await db.exports.find_one({"business_id": bid, "status": {"$in": ["queued", "running"]},
                                  "created_at": {"$gt": now - RUNNING_FOR}}, {"_id": 1}):
        raise AppError(409, "An export of your shop is already being prepared.", "export_running")
    await ratelimit.consume(f"export:{bid}", EXPORT_RATE)
    doc = {"id": new_id(), "business_id": bid, "kind": "shop", "status": "queued", "created_at": now,
           "requested_by": {"id": owner["id"], "name": owner.get("name", "")},
           "delete_after": now + timedelta(hours=keep_hours(), days=KEEP_FOR_DAYS)}
    await db.exports.insert_one(dict(doc))
    await audit.log(db, owner, "export.shop", {"type": "export", "id": doc["id"]})
    await jobs.enqueue("build_shop_export", doc["id"], _job_id=f"shop-export:{doc['id']}")
    return _shown(doc)


@router.get("/exports/shop")
async def list_shop_exports(owner: dict = Depends(auth.require("owner"))):
    rows = await get_db().exports.find({"business_id": owner["business_id"]}, {"_id": 0}).sort(
        "created_at", -1).limit(10).to_list(10)
    return [_shown(r) for r in rows]


@router.get("/exports/shop/{export_id}")
async def get_shop_export(export_id: str, request: Request, owner: dict = Depends(auth.require("owner"))):
    doc = await get_db().exports.find_one({"id": export_id, "business_id": owner["business_id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Export not found.")
    url = None
    if doc.get("status") == "ready":
        if _aware(doc["expires_at"]) <= datetime.now(timezone.utc):
            doc["status"] = "expired"
        else:
            url = await run_in_threadpool(_link, doc, str(request.base_url).rstrip("/"))
    return _shown(doc, url)


@router.get("/exports/shop/{export_id}/file", include_in_schema=False)
async def download_local_export(export_id: str, token: str):
    """Local storage only (a laptop): the file behind the signed link from GET /api/exports/shop/{id}."""
    try:
        claims = jwt.decode(token, auth._secret(), algorithms=["HS256"], audience=TOKEN_AUDIENCE)
    except jwt.InvalidTokenError:
        raise HTTPException(403, "This download link has expired. Please open the export again.")
    if claims.get("sub") != export_id or storage_client.init_storage() is not None:
        raise HTTPException(404, "Export not found.")
    doc = await get_db().exports.find_one({"id": export_id, "business_id": claims.get("bid"), "status": "ready"}, {"_id": 0})
    path = _local_file(doc["path"]) if doc else None
    if not path or not path.is_file():
        raise HTTPException(404, "Export not found.")
    return FileResponse(path, media_type="application/zip", filename=_filename(doc),
                        headers={"Cache-Control": "private, no-store"})


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _filename(doc: dict) -> str:
    return f"hara-hisaab-export-{_aware(doc['created_at']).astimezone(IST):%Y-%m-%d-%H%M}.zip"


def _local_file(key: str) -> Path:
    root = storage_client.LOCAL_DIR.resolve()
    p = (root / key).resolve()
    if root not in p.parents:
        raise ValueError("Invalid path")
    return p


def _link(doc: dict, base_url: str) -> str:
    s3 = storage_client.init_storage()
    if s3 is not None:
        return s3.generate_presigned_url("get_object", ExpiresIn=link_seconds(), Params={
            "Bucket": _bucket(), "Key": doc["path"],
            "ResponseContentDisposition": f'attachment; filename="{_filename(doc)}"'})
    now = int(time.time())
    token = jwt.encode({"sub": doc["id"], "bid": doc["business_id"], "aud": TOKEN_AUDIENCE, "iat": now,
                        "exp": now + link_seconds()}, auth._secret(), algorithm="HS256")
    return f"{base_url}/api/exports/shop/{doc['id']}/file?token={token}"


def _json_default(value):
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return str(value)


def _line(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, default=_json_default, separators=(",", ":")) + "\n"


async def _write_part(db, zf: zipfile.ZipFile, name: str, coll, query: dict, fields) -> int:
    """One collection as JSON lines, written in batches from a thread so the event loop stays free."""
    proj = {"_id": 0, **{f: 1 for f in fields}} if fields else {"_id": 0}
    n, batch = 0, []
    with zf.open(f"{name}.jsonl", "w", force_zip64=True) as out:
        async for doc in coll.find(query, proj).batch_size(1000):
            batch.append(_line(doc))
            n += 1
            if len(batch) >= 1000:
                await asyncio.to_thread(out.write, "".join(batch).encode())
                batch = []
        if batch:
            await asyncio.to_thread(out.write, "".join(batch).encode())
    return n


async def _write_bills_csv(db, zf: zipfile.ZipFile, bid: str):
    """invoices.csv (with a BOM, so Excel reads the Hindi names), in batches from a thread like _write_part."""
    buf = io.StringIO()
    w = csv.writer(buf)  # the csv module's own line ending, CR LF
    w.writerow(BILL_COLUMNS)
    with zf.open("invoices.csv", "w", force_zip64=True) as out:
        await asyncio.to_thread(out.write, buf.getvalue().encode("utf-8-sig"))
        buf.seek(0), buf.truncate()
        async for inv in db.invoices.find({"business_id": bid}, {"_id": 0, **{c: 1 for c in BILL_COLUMNS}}).sort(
                "created_at", 1).batch_size(1000):
            w.writerow([csv_text(inv.get(c, "")) if c in TEXT_COLUMNS else inv.get(c, "") for c in BILL_COLUMNS])
            if buf.tell() > 256_000:
                await asyncio.to_thread(out.write, buf.getvalue().encode())
                buf.seek(0), buf.truncate()
        await asyncio.to_thread(out.write, buf.getvalue().encode())


README = """Hara Hisaab: export of {shop}
Made {made} (India time) for the shop owner.

business.json        the shop's details
<name>.jsonl         one record per line (JSON), one file per kind of data:
                     categories, products, customers, invoices (bills, with every line and tax),
                     purchases (supplier bills), suppliers, stock_history, users (owner and staff; no passwords),
                     photos (where each photo is stored), ai_usage (photo scans per day), audit_log (who did what)
invoices.csv         one row per bill, for a spreadsheet
Counts: {counts}

Bills must be kept for 8 years under GST law; keep this file safe, it holds your customers' phone numbers.
"""


async def build_zip(db, export: dict, path: str) -> dict:
    """Writes the shop's export to `path`; returns how many records of each kind it holds."""
    bid = export["business_id"]
    biz = await db.businesses.find_one({"id": bid}, {"_id": 0}) or {}
    counts = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("business.json", json.dumps(biz, ensure_ascii=False, default=_json_default, indent=2))
        for name, coll, fields in PARTS:
            counts[name] = await _write_part(db, zf, name, db[coll], {"business_id": bid}, fields)
        counts["audit_log"] = await _write_part(db, zf, "audit_log", audit.collection(db), {"business_id": bid}, None)
        await _write_bills_csv(db, zf, bid)
        zf.writestr("README.txt", README.format(shop=biz.get("name") or "your shop", counts=json.dumps(counts),
                                                made=datetime.now(IST).strftime("%d %b %Y %H:%M")))
    return counts


def _store(local_path: str, key: str):
    s3 = storage_client.init_storage()
    if s3 is not None:
        s3.upload_file(local_path, _bucket(), key, ExtraArgs={"ContentType": "application/zip"})  # multipart when large
        return
    target = _local_file(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local_path, target)


def _remove(key: str):
    s3 = storage_client.init_storage()
    if s3 is not None:
        s3.delete_object(Bucket=_bucket(), Key=key)
    else:
        _local_file(key).unlink(missing_ok=True)


@jobs.job
async def build_shop_export(ctx, export_id: str):
    """Worker job: build the zip, store it, mark the export ready. Safe to run again (arq retries after a crash):
    a finished export is left alone, a half-done one starts over."""
    db = ctx["db"]
    export = await db.exports.find_one_and_update(
        {"id": export_id, "status": {"$in": ["queued", "running"]}},
        {"$set": {"status": "running", "started_at": datetime.now(timezone.utc)}, "$inc": {"tries": 1}},
        projection={"_id": 0}, return_document=ReturnDocument.AFTER)
    if not export:
        return None
    fd, tmp = tempfile.mkstemp(prefix="hh-export-", suffix=".zip")
    os.close(fd)
    try:
        counts = await build_zip(db, export, tmp)
        key = f"{storage_client.APP_NAME}/exports/{export['business_id']}/{export_id}.zip"
        await run_in_threadpool(_store, tmp, key)
        now = datetime.now(timezone.utc)
        await db.exports.update_one({"id": export_id}, {"$set": {
            "status": "ready", "path": key, "size": os.path.getsize(tmp), "counts": counts, "finished_at": now,
            "expires_at": now + timedelta(hours=keep_hours())}})
        return key
    except Exception as e:
        log.exception("shop export %s failed", export_id)
        await db.exports.update_one({"id": export_id}, {"$set": {
            "status": "failed", "error": type(e).__name__, "finished_at": datetime.now(timezone.utc)}})
        return None
    finally:
        os.unlink(tmp)


@jobs.job
async def purge_exports(ctx):
    """Worker cron, daily: removes export files whose link time is over (the record stays until `delete_after`)."""
    db, removed = ctx["db"], 0
    async for doc in db.exports.find({"status": "ready", "expires_at": {"$lte": datetime.now(timezone.utc)}},
                                     {"_id": 0, "id": 1, "path": 1}):
        try:
            await run_in_threadpool(_remove, doc["path"])
        except Exception:
            log.exception("could not remove export file %s", doc["path"])
            continue
        await db.exports.update_one({"id": doc["id"]}, {"$set": {"status": "expired"}, "$unset": {"path": ""}})
        removed += 1
    return removed


# ---- the audit log ----

@router.get("/audit-log")
async def audit_log(action: str = Query("", max_length=60, pattern=r"^[a-z0-9_.]*$"),
                    actor_id: str = Query("", max_length=64), target_id: str = Query("", max_length=64),
                    before: str = Query("", max_length=40), limit: int = Query(50, ge=1, le=audit.PAGE_MAX),
                    owner: dict = Depends(auth.require("owner"))):
    """The shop's audit entries, newest first. Next page: `before` = the `at` of the last entry you got."""
    when = None
    if before:
        try:
            when = _aware(datetime.fromisoformat(before))
        except ValueError:
            raise HTTPException(400, "Please choose a valid date.")
    rows = await audit.find(get_db(), owner["business_id"], action=action, actor_id=actor_id, target_id=target_id,
                            before=when, limit=limit)
    return JSONResponse(rows, headers={"Cache-Control": "private, no-store"})
