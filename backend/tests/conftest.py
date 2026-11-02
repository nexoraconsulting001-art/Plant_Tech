"""Shared test setup: the real app on a real MongoDB replica set, local photo storage and a fake Claude.

No network, no keys. Run: cd backend && python -m pytest
- MongoDB: MONGO_URL (default mongodb://127.0.0.1:27117/?replicaSet=rs0, a local single-node replica set, so
  transactions work as in production). Each test run gets its own database (hh_test_<pid>_<random>, so parallel
  runs never meet), emptied before every test and dropped at the end. Migrations run at app start (AUTO_MIGRATE).
- MONGO_URL=mongomock runs on the in-memory mongomock instead (no transactions; quick checks only).
- The app's database client lives on the TestClient's event loop: run database coroutines in a test with
  `run(coro)` from here, not asyncio.run.
"""
import asyncio
import base64
import io
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

MONGO_URL = os.environ.get("MONGO_URL") or "mongodb://127.0.0.1:27117/?replicaSet=rs0"
MOCK = MONGO_URL == "mongomock"
TEST_DB = f"hh_test_{os.getpid()}_{secrets.token_hex(3)}"
os.environ.update(
    ENV="dev", MONGO_URL="mongodb://localhost:27017" if MOCK else MONGO_URL, MONGO_DB=TEST_DB, DB_NAME=TEST_DB,
    AUTO_MIGRATE="1", STORAGE_BACKEND="local", LOCAL_STORAGE_DIR=tempfile.mkdtemp(),
    JWT_SECRET="test-secret-0123456789-abcdefghijklmnop", ADMIN_PHONES="9000000001", AI_DAILY_LIMIT="5",
    PUBLIC_BASE_URL="https://pos.test", AI_ENRICH_NAMES="0", LOG_FORMAT="console",
)
for name in ("GOOGLE_CLIENT_IDS", "REDIS_URL", "SENTRY_DSN"):
    os.environ.pop(name, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
import httpx2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import ai_client  # noqa: E402
import db as db_module  # noqa: E402
import ratelimit  # noqa: E402
import server  # noqa: E402

_app = {"client": None}


def run(coro):
    """Run a coroutine where the app's database client lives (the open TestClient's event loop)."""
    c = _app["client"]
    if c is None or MOCK:
        return asyncio.run(coro)
    return c.portal.call(lambda: coro)


@pytest.fixture(scope="session")
def mongo():
    """A plain (blocking) client on the test database, for emptying it; dropped after the last test."""
    if MOCK:
        yield None
        return
    from pymongo import MongoClient

    sync = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    sync.admin.command("ping")
    yield sync
    sync.drop_database(TEST_DB)
    sync.close()


def empty(mongo):
    """Every collection emptied (indexes kept); the app's start-up then runs the migrations again."""
    if MOCK:
        from mongomock_motor import AsyncMongoMockClient

        db_module.state.db = AsyncMongoMockClient()[TEST_DB]
        return
    db = mongo[TEST_DB]
    for name in db.list_collection_names():
        db[name].delete_many({})


@pytest.fixture
def client(mongo):
    """A fresh, empty app per test."""
    empty(mongo)
    ratelimit.reset()
    with TestClient(server.app) as c:
        _app["client"] = c
        try:
            yield c
        finally:
            _app["client"] = None
    db_module.state.db = None

PLANT = {"kind": "plant", "reason": "a potted plant", "plant_name": "Money Plant", "scientific_name": "Epipremnum aureum", "confidence": 91,
         "alternatives": ["Pothos"], "matched_catalog_name": "Money Plant", "suggested_category": "indoor plants"}
INVOICE = {"is_bill": True, "reason": "a printed invoice", "same_supplier": True, "same_bill": True,
           "pages": [{"page": 1, "is_bill": True, "supplier": "Green Farms", "supplier_gstin": "", "invoice_number": "GF-12"}],
           "supplier": "Green Farms", "supplier_phone": "98111 00000", "supplier_address": "Sohna Road, Gurugram",
           "supplier_gstin": "", "invoice_number": "GF-12", "date": "12/10/2026", "total": 1250,
           "items": [{"name": "Snake Plant", "quantity": 5, "purchase_price": 200}]}

NAMES = {"is_nursery_item": True, "english_name": "Holy Basil", "hindi_name": "तुलसी", "hinglish": ["tulsi"], "aliases": ["Krishna Tulsi"]}


def claude_message(text, stop_reason="end_turn", **extra):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": ai_client.MODEL_NAME,
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 1500, "output_tokens": 80}, **extra}


def fake_claude_client(handler, max_retries=2):
    return anthropic.AsyncAnthropic(api_key="test", max_retries=max_retries,
                                    http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


class FakeClaude(list):
    """The (headers, body) of each request sent; set .plant / .invoice to change what it answers."""

    def __init__(self):
        super().__init__()
        self.plant, self.invoice, self.names = dict(PLANT), dict(INVOICE), dict(NAMES)


@pytest.fixture
def claude():
    """Fake Claude that answers like the real API."""
    calls = FakeClaude()

    def handler(req):
        body = json.loads(req.content)
        calls.append((dict(req.headers), body))
        reply = {ai_client.SYSTEM_INVOICE: calls.invoice, ai_client.SYSTEM_NAMES: calls.names}.get(body["system"], calls.plant)
        return httpx2.Response(200, json=claude_message(json.dumps(reply)))

    ai_client._client = fake_claude_client(handler)
    yield calls
    ai_client._client = None


def photo_b64(size=(1200, 900), fmt="JPEG", color=(30, 120, 40)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return base64.b64encode(buf.getvalue()).decode()


class Shop:
    """An owner logged in to a shop that finished setup, with helpers for common calls."""

    def __init__(self, client, phone="9811122334", name="Suresh", gst=True):
        self.c = client
        r = client.post("/api/auth/register", json={"name": name, "phone": phone, "password": "secret123"})
        assert r.status_code == 200, r.text
        self.token = r.json()["token"]
        self.h = {"Authorization": f"Bearer {self.token}"}
        r = client.put("/api/business", headers=self.h, json={
            "name": f"{name}'s Nursery", "address": "Sector 5", "phone": phone, "gst_registered": gst,
            "gstin": "06abcde1234f1z5" if gst else "", "upi_id": "shop@upi", "onboarding_done": True})
        assert r.status_code == 200, r.text
        cats = client.post("/api/categories/bulk", headers=self.h, json={"items": [
            {"name": "Indoor Plants", "gst_percent": 0}, {"name": "Pots", "gst_percent": 18}]}).json()
        self.cat = {c["name"]: c["id"] for c in cats}

    def get(self, url, **kw):
        return self.c.get(url, headers=self.h, **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(url, headers=self.h, json=json, **kw)

    def product(self, name, price, stock, category="Indoor Plants", **extra):
        r = self.post("/api/products", {"name": name, "selling_price": price, "stock": stock,
                                        "category_id": self.cat.get(category), **extra})
        assert r.status_code == 200, r.text
        return r.json()

    def worker(self, phone="9822233445", pin="4821", **perms):
        r = self.post("/api/staff", {"name": "Ramesh", "phone": phone, "password": pin,
                                     "permissions": {"sell": True, "stock": True, "customers": True,
                                                     "reports": False, **perms}})
        assert r.status_code == 200, r.text
        login = self.c.post("/api/auth/login", json={"phone": phone, "password": pin})
        assert login.status_code == 200, login.text
        return r.json()["id"], {"Authorization": f"Bearer {login.json()['token']}"}
