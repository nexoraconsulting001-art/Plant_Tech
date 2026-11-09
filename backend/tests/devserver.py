"""Local API for checking the app in a browser: the real app on in-memory Mongo, local photo storage and a fake
Claude, with a demo shop. Nothing leaves the computer and nothing is kept after it stops.

Run (from anywhere, with the packages of requirements.txt and requirements-dev.txt installed):
    python backend/tests/devserver.py
- Port 8001. Demo owner 9000000009, password from DEV_DEMO_PASSWORD; shop "Green House" with demo data.
- In-memory mongomock (mongomock-motor from requirements-dev.txt): no transactions, nothing kept. The migrations
  run at start (AUTO_MIGRATE), so the starter library is there; no Redis needed (jobs run in the process).
- Or a real local MongoDB replica set, closest to staging (transactions, the same driver paths): set DEV_MONGO_URL,
  e.g. mongodb://127.0.0.1:27017/?replicaSet=rs0 (CLAUDE.md shows how to start one in Docker). Each start uses a new
  database named hh_dev_<time>, so an earlier run's data is never touched.
- The fake Claude answers from fake_ai.json in the dev folder printed at start (re-read on every call),
  keyed "plant" / "invoice" / "names"; edit it to try other answers (a laptop, a bill, two suppliers).
"""
import json
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEV = Path(tempfile.gettempdir()) / "hara-hisaab-dev"
REAL_MONGO = os.environ.get("DEV_MONGO_URL", "").strip()
DB = f"hh_dev_{int(time.time())}" if REAL_MONGO else "dev"
os.environ.update(
    ENV="dev", AUTO_MIGRATE="1", MONGO_URL=REAL_MONGO or "mongodb://localhost:27017", MONGO_DB=DB, DB_NAME=DB,
    STORAGE_BACKEND="local",
    LOCAL_STORAGE_DIR=str(DEV / "uploads"), JWT_SECRET="dev-secret-0123456789-abcdefghijklmnopqrstuv",
    ADMIN_PHONES="9000000009", AI_DAILY_LIMIT="300", PUBLIC_BASE_URL="http://localhost:8001",
)
for name in ("GOOGLE_CLIENT_IDS", "REDIS_URL", "SENTRY_DSN"):
    os.environ.pop(name, None)
sys.path.insert(0, str(BACKEND))
os.chdir(tempfile.gettempdir())  # never load a backend/.env

import anthropic  # noqa: E402
import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

import ai_client  # noqa: E402
import db as db_module  # noqa: E402
import demo_data  # noqa: E402
import server  # noqa: E402
from common import normalize_in_phone  # noqa: E402
from routers import auth as auth_router  # noqa: E402

PHONE = "9000000009"
DEFAULTS = {
    "plant": {"kind": "plant", "reason": "", "plant_name": "Money Plant", "scientific_name": "Epipremnum aureum",
              "confidence": 91, "alternatives": ["Pothos"], "matched_catalog_name": "Money Plant",
              "suggested_category": "Indoor Plants", "stock_match": "", "stock_alternatives": []},
    "invoice": {"is_bill": True, "reason": "printed invoice", "same_supplier": True, "same_bill": True,
                "pages": [{"page": 1, "is_bill": True, "supplier": "Green Valley Growers", "supplier_gstin": "",
                           "invoice_number": "GV-12"}],
                "supplier": "Green Valley Growers", "supplier_phone": "98100 12345",
                "supplier_address": "Sohna Road, Gurugram", "supplier_gstin": "", "invoice_number": "GV-12",
                "date": "12/10/2026", "total": 1250, "items": [{"name": "Snake Plant", "quantity": 5, "purchase_price": 200}]},
    "names": {"is_nursery_item": True, "english_name": "", "hindi_name": "", "hinglish": [], "aliases": [],
              "botanical_name": "", "botanical_synonyms": []},
}
KIND = {ai_client.SYSTEM_PLANT: "plant", ai_client.SYSTEM_INVOICE: "invoice", ai_client.SYSTEM_NAMES: "names"}


def _kind(system) -> str:
    """Which call this is, from its system prompt (a string, or blocks: the instructions, then a plant scan's stock list)."""
    text = system if isinstance(system, str) else "".join(b.get("text", "") for b in system or [])
    return next((k for prompt, k in KIND.items() if text.startswith(prompt)), "plant")


def handler(req):
    kind = _kind(json.loads(req.content).get("system"))
    try:
        custom = json.loads((DEV / "fake_ai.json").read_text(encoding="utf-8"))
    except Exception:
        custom = {}
    return httpx2.Response(200, json={
        "id": "msg_dev", "type": "message", "role": "assistant", "model": ai_client.MODEL_NAME,
        "content": [{"type": "text", "text": json.dumps(custom.get(kind, DEFAULTS[kind]))}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 1500, "output_tokens": 80}})


ai_client._client = anthropic.AsyncAnthropic(api_key="dev", max_retries=0,
                                             http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
if not REAL_MONGO:
    db_module.state.db = AsyncMongoMockClient()["dev"]
original = server.app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with original(app):
        db = db_module.get_db()
        user = await auth_router._new_owner(db, name="Demo Owner", phone=normalize_in_phone(PHONE),
                                            password=os.environ.get("DEV_DEMO_PASSWORD", "local-demo"), is_admin=True)
        await db.businesses.update_one({"id": user["business_id"]}, {"$set": {
            "name": "Green House", "address": "Sector 5, Gurugram", "gst_registered": True,
            "gstin": "06ABCDE1234F1Z4", "upi_id": "greenhouse@upi", "onboarding_done": True}})
        print("dev folder (fake_ai.json):", DEV, flush=True)
        print("demo:", await demo_data.seed(db, PHONE), flush=True)
        yield


server.app.router.lifespan_context = lifespan

if __name__ == "__main__":
    DEV.mkdir(parents=True, exist_ok=True)
    uvicorn.run(server.app, host="127.0.0.1", port=8001, log_level="info")
