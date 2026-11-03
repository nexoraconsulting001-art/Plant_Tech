"""NurseryPOS API (Hara Hisaab). Routes live in routers/; this file wires them up.

Every shop's data carries a business_id and every route filters by the logged-in user's shop.
Phone notifications to the owner (low stock, daily summary): notify.py, push.py, routers/notifications.py;
the daily summary runs in the worker (worker.py), not here.
Public (no login): /health, /livez, /readyz, /api/config, /api/auth/*, photo files, and /i/{token} customer bills.

Start-up only reads: indexes, the starter library and data fixes are migrations (`python -m migrations up`,
run by the deploy before the new version starts; AUTO_MIGRATE runs them here in dev and tests).
Production (ENV=production): no /docs or /openapi.json, CORS only for CORS_ORIGINS, settings checked at start.
Runs under gunicorn with Uvicorn workers (gunicorn.conf.py); `uvicorn server:app` still works on a laptop.
"""
import logging
import time
from contextlib import asynccontextmanager

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

import db as db_module
import jobs
import migrations
import observability
import overload
import push
import storage_client
from db import get_db
from errors import AppError, app_error_handler
from routers import (ai, auth, business, carts, customers, exports, files, library, notifications, products,
                     purchases, reports, sales, staff, suppliers)
from settings import check

settings = check()
observability.setup_logging(settings)
observability.init_sentry(settings)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    if settings.auto_migrate:  # dev and tests only (settings.py refuses it in staging and production)
        await migrations.up(db)
    try:
        await run_in_threadpool(storage_client.init_storage)
        logger.info("Storage initialized")
    except Exception as e:
        logger.error("Storage init failed: %s", e)
    if len(settings.jwt_secret) < 32:
        logger.error("JWT_SECRET is missing or shorter than 32 characters - logins will fail")
    logger.info("Phone notifications: %s", "on (daily summary in the worker)" if push.enabled() else "off (PUSH_ENABLED)")
    yield
    await jobs.close()
    await db_module.close()


docs = {} if settings.docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="NurseryPOS API", lifespan=lifespan, **docs)
# reports before sales: /api/invoices/summary must not be taken for /api/invoices/{invoice_id}
# (the /api/account routes come with auth.router)
for module in (auth, staff, business, library, products, customers, carts, reports, sales, purchases, suppliers, ai,
               files, notifications, exports):
    app.include_router(module.router)
app.include_router(sales.public_router)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(PyMongoError, overload.db_error_handler)  # timeouts: 503 db_busy, not a 500
# Middleware, innermost first: the in-flight limit (overload.py; its 503 still gets CORS headers, the log line and
# the request id), CORS, gzip, the request log line, then the request id around everything.
app.add_middleware(overload.InFlightLimit, limit=settings.in_flight_limit)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_allow_origins, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-Request-ID", "Retry-After", "Idempotent-Replayed"])
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(observability.RequestContextMiddleware)
app.add_middleware(CorrelationIdMiddleware, header_name="X-Request-ID")
observability.setup_metrics(app)


@app.get("/health")
async def health():
    """Kept for the deploy script and old monitors; /livez and /readyz are the real checks."""
    return {"ok": True}


@app.get("/livez", include_in_schema=False)
async def livez():
    """The process is up (no database call): a failing livez means restart the container."""
    return {"ok": True}


_storage_ok = {"at": 0.0, "ok": False}
STORAGE_CHECK_EVERY = 30  # seconds; a load balancer asks every few seconds and R2 calls are not free


def _check_storage():
    s3 = storage_client.init_storage()
    if s3 is not None:
        s3.head_bucket(Bucket=storage_client.R2_BUCKET)
    elif not storage_client.LOCAL_DIR.is_dir():
        raise RuntimeError("local photo folder is missing")


async def _storage_ready() -> bool:
    if time.monotonic() - _storage_ok["at"] > STORAGE_CHECK_EVERY or not _storage_ok["ok"]:
        try:
            await run_in_threadpool(_check_storage)
            _storage_ok["ok"] = True
        except Exception as e:
            logger.warning("readyz: storage check failed: %s", e)
            _storage_ok["ok"] = False
        _storage_ok["at"] = time.monotonic()
    return _storage_ok["ok"]


@app.get("/readyz", include_in_schema=False)
async def readyz():
    """Ready for traffic: MongoDB answers and photo storage is reachable. 503 otherwise (take it out of the pool).
    MongoDB "busy" (every pooled connection in use, db.ping) is still ready: this worker answers 503 busy/db_busy
    at once, which the app retries, while a proxy with no upstream left would hold every request for 30 s."""
    checks = {}
    try:
        checks["mongo"] = await db_module.ping()
    except Exception as e:
        logger.warning("readyz: mongo ping failed: %s", e)
        checks["mongo"] = "down"
    checks["storage"] = "ok" if await _storage_ready() else "down"
    ok = "down" not in checks.values()
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)


@app.get("/api/config")
async def app_config():
    """What the app needs to know before login: the oldest app version still served, and feature flags."""
    return {"min_app_version": settings.min_app_version, "features": settings.feature_flags()}


@app.get("/api/")
async def root():
    return {"message": "NurseryPOS API"}
