"""Overload protection (overload.py): the in-flight limit, database timeouts as 503 db_busy, the transaction limit."""
import asyncio
import json
import logging
import time

import httpx
import pytest
from pymongo import AsyncMongoClient
from pymongo.errors import (AutoReconnect, DuplicateKeyError, ExecutionTimeout, NetworkTimeout, OperationFailure,
                            PyMongoError, ServerSelectionTimeoutError, WaitQueueTimeoutError)

import catalog_search
import db as db_module
import overload
import server
from conftest import MOCK, MONGO_URL, TEST_DB, Shop, run
from settings import Settings

needs_mongo = pytest.mark.skipif(MOCK, reason="needs a real MongoDB")
BUSY = "The server is busy. Please try again in a few seconds."


def _limit_of(app) -> overload.InFlightLimit:
    node = app.middleware_stack
    while not isinstance(node, overload.InFlightLimit):
        node = node.app
    return node


# --- the in-flight limit --------------------------------------------------------------------------------------
def test_requests_over_the_limit_get_503_busy_at_once():
    gate = asyncio.Event()
    entered = []

    async def slow_app(scope, receive, send):
        entered.append(scope["path"])
        if scope["path"] == "/slow":
            await gate.wait()
        if scope["path"] == "/boom":
            raise RuntimeError("route failed")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    limited = overload.InFlightLimit(slow_app, limit=2)

    async def go():
        transport = httpx.ASGITransport(app=limited, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
            held = [asyncio.create_task(c.get("/slow")) for _ in range(2)]
            while limited.in_flight < 2:
                await asyncio.sleep(0.001)
            start = time.perf_counter()
            refused = await c.post("/api/invoices", json={})
            took = time.perf_counter() - start
            exempt = [await c.get(p) for p in ("/livez", "/readyz", "/health", "/metrics", "/api/ai/scan-invoice")]
            gate.set()
            done = await asyncio.gather(*held)
            after = await c.get("/fast")
            await c.get("/boom")  # an app error still frees its place
            return refused, took, exempt, done, after

    refused, took, exempt, done, after = asyncio.run(go())
    assert refused.status_code == 503 and took < 0.5
    assert refused.headers["retry-after"] == str(overload.RETRY_AFTER)
    assert refused.json() == {"detail": BUSY, "code": "busy", "key": BUSY, "params": {}}
    assert "/api/invoices" not in entered  # refused before any route code ran
    assert [r.status_code for r in exempt] == [200, 200, 200, 200, 200]  # the AI route: ai_guard limits it
    assert [r.status_code for r in done] == [200, 200] and after.status_code == 200
    assert limited.in_flight == 0 and limited.refused == 1


def test_default_limit_is_twice_the_pool():
    assert Settings(mongo_max_pool_size=25).in_flight_limit == 50
    assert Settings(mongo_max_pool_size=10, max_in_flight=7).in_flight_limit == 7
    assert Settings().mongo_timeout_ms == 5000


def test_the_app_refuses_with_busy_but_keeps_health_checks(client):
    shop = Shop(client)
    limiter = _limit_of(server.app)
    was = limiter.limit
    limiter.limit = 0
    try:
        r = shop.get("/api/products?search=rose")
        assert r.status_code == 503 and r.json()["code"] == "busy" and r.headers["retry-after"] == "2"
        assert r.headers.get("x-request-id")  # the request id and the log line still wrap it
        assert client.get("/livez").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.post("/api/ai/identify-plant", json={}).status_code in (401, 403)  # not counted, not refused
    finally:
        limiter.limit = was
    assert shop.get("/api/products?search=rose").status_code == 200


# --- database timeouts ------------------------------------------------------------------------------------------
TIMEOUTS = [
    WaitQueueTimeoutError("Timed out while checking out a connection from connection pool"),
    ExecutionTimeout("operation exceeded time limit", 50),
    ServerSelectionTimeoutError("No servers found yet, Timeout: 5.0s, Topology Description: " + "x" * 2000),
    NetworkTimeout("timed out"),
    AutoReconnect("connection closed"),
    OperationFailure("operation exceeded time limit", 50),  # MaxTimeMSExpired as a plain OperationFailure
]


@pytest.mark.parametrize("error", TIMEOUTS, ids=lambda e: type(e).__name__)
def test_database_timeouts_are_503_db_busy_with_one_line(client, monkeypatch, caplog, error):
    shop = Shop(client)

    async def stuck(*_a, **_kw):
        raise error

    monkeypatch.setattr(catalog_search, "find_products", stuck)
    with caplog.at_level(logging.WARNING):
        r = shop.get("/api/products?search=rose")
    assert r.status_code == 503, r.text
    assert r.headers["retry-after"] == "2"
    assert r.json() == {"detail": BUSY, "code": "db_busy", "key": BUSY, "params": {}}
    lines = [rec for rec in caplog.records if "database busy" in rec.getMessage()]
    assert len(lines) == 1 and lines[0].exc_info is None and lines[0].levelno == logging.WARNING
    assert "/api/products" in lines[0].getMessage() and len(lines[0].getMessage()) < 400
    assert not [rec for rec in caplog.records if rec.exc_info]  # no traceback anywhere


def test_other_database_errors_stay_500(client, monkeypatch):
    shop = Shop(client)

    async def broken(*_a, **_kw):
        raise DuplicateKeyError("E11000 duplicate key")

    monkeypatch.setattr(catalog_search, "find_products", broken)
    monkeypatch.setattr(client._transport, "raise_server_exceptions", False)  # answer 500 instead of raising here
    r = shop.get("/api/products?search=rose")
    assert r.status_code == 500


def test_is_db_unavailable():
    for e in TIMEOUTS:
        assert overload.is_db_unavailable(e), e
    assert not overload.is_db_unavailable(DuplicateKeyError("dup"))
    assert not overload.is_db_unavailable(OperationFailure("bad query", 2))


# --- a transaction's whole time is limited -------------------------------------------------------------------------
@needs_mongo
def test_transaction_retries_stop_at_the_time_limit(client):
    """with_transaction alone retries a transient error for 120 s; in_transaction stops at its limit."""
    db = db_module.state.db
    tries = []

    async def always_conflicts(session):
        tries.append(1)
        await db.txn_limit.insert_one({"n": len(tries)}, session=session)
        err = OperationFailure("write conflict", 112)
        err._add_error_label("TransientTransactionError")
        raise err

    async def go():
        start = time.monotonic()
        with pytest.raises(Exception) as caught:
            await db_module.in_transaction(always_conflicts, timeout=0.5)
        return time.monotonic() - start, caught.value

    took, error = run(go())
    assert took < 5 and len(tries) > 1
    assert overload.is_db_unavailable(error)  # answered as 503 db_busy by the API
    assert run(db.txn_limit.count_documents({})) == 0


@needs_mongo
def test_a_transaction_over_its_limit_is_aborted_on_the_server(client):
    """Load-test fix review: a try that runs out of time mid-way (the event loop sent its next command late) is
    aborted on the server, so a bill from another worker on the same document goes through at once instead of
    meeting a write conflict until MongoDB drops the stale transaction 60 s later."""
    db = db_module.state.db
    run(db.txn_abort.insert_one({"_id": "counter", "n": 0}))

    async def late(session):
        await db.txn_abort.update_one({"_id": "counter"}, {"$inc": {"n": 1}}, session=session)
        await asyncio.sleep(0.8)
        await db.txn_abort.find_one({"_id": "counter"}, session=session)

    async def go():
        with pytest.raises(PyMongoError) as caught:
            await db_module.in_transaction(late, timeout=0.5)
        other = AsyncMongoClient(MONGO_URL, **db_module.client_options())  # another worker's connections
        try:
            async def bump(session):
                await other[TEST_DB].txn_abort.update_one({"_id": "counter"}, {"$inc": {"n": 1}}, session=session)

            start = time.monotonic()
            await db_module.in_transaction(bump, other[TEST_DB], timeout=3)
            return caught.value, time.monotonic() - start
        finally:
            await other.close()

    error, took = run(go())
    assert overload.is_db_unavailable(error)  # 503 db_busy
    assert took < 1, f"the second transaction waited {took:.1f} s for the first one's locks"
    assert run(db.txn_abort.find_one({"_id": "counter"}))["n"] == 1


# --- readiness while the pool is full ------------------------------------------------------------------------------
@needs_mongo
def test_readyz_stays_ready_while_every_pooled_connection_is_busy(client):
    """Load-test fix review: a full pool (slow MongoDB operations) is not "down". /readyz answers 200 with mongo
    "busy", so Caddy keeps the only upstream and requests get a fast 503 instead of being held for 30 s."""
    async def go():
        small = AsyncMongoClient(MONGO_URL, **{**db_module.client_options(), "maxPoolSize": 1})
        real = db_module.state.db
        db_module.state.db = small[TEST_DB]
        try:
            await small[TEST_DB].slow.insert_one({"x": 1})
            hold = asyncio.create_task(small[TEST_DB].slow.find_one({"$where": "sleep(2600) || true"}))
            await asyncio.sleep(0.3)
            busy = await server.readyz()
            await hold
            idle = await server.readyz()
            return busy, idle
        finally:
            db_module.state.db = real
            await small.close()

    busy, idle = run(go())
    assert busy.status_code == 200 and json.loads(busy.body)["checks"] == {"mongo": "busy", "storage": "ok"}
    assert idle.status_code == 200 and json.loads(idle.body)["checks"] == {"mongo": "ok", "storage": "ok"}
