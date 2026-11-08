"""AI cost and safety (WP-6): client limits, models per route, usage log and budget, outages and the breaker,
photo limits, prompts, allowances and the per-user rate limit."""
import asyncio
import base64
import importlib
import io
import json
import os
import struct
import time
import tracemalloc
import zlib
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from PIL import Image

import ai_client
from common import date_key
import ai_guard
import ai_usage
import db as db_module
from conftest import PLANT, INVOICE, Shop, claude_message, fake_claude_client, photo_b64, run

SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"


class Fake(list):
    """A fake Claude that records (headers, body) and answers with `reply(body)` -> (status, json)."""

    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply or (lambda body: (200, claude_message(json.dumps(
            INVOICE if _system_text(body).startswith("You are an OCR") else PLANT))))

    def handler(self, req):
        body = json.loads(req.content)
        self.append((dict(req.headers), body))
        status, payload = self.reply(body)
        return httpx2.Response(status, json=payload)


def _system_text(body) -> str:
    s = body["system"]
    return s if isinstance(s, str) else "".join(b["text"] for b in s)


@pytest.fixture
def fake():
    f = Fake()
    ai_client._client = fake_claude_client(f.handler, max_retries=0)
    yield f
    ai_client._client = None


def db():
    return db_module.get_db()


def _error(status, kind):
    return lambda body: (status, {"type": "error", "error": {"type": kind, "message": kind}})


def _usage(shop):
    return run(db().ai_usage.find_one({"business_id": _bid(shop)}, {"_id": 0}))


def _bid(shop):
    return shop.get("/api/business").json()["id"]


# --- client, models, prompts ---------------------------------------------------------------------------------
def test_client_has_timeouts_and_retries():
    ai_client._client = None
    c = ai_client._get_client()
    try:
        assert c.max_retries == 2
        assert c.timeout.read == 60 and c.timeout.connect == 5
    finally:
        ai_client._client = None


def test_model_per_route_and_fallback_only_where_supported(client, fake, monkeypatch):
    monkeypatch.setattr(ai_client._settings, "claude_model_plant", HAIKU)
    shop = Shop(client)
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    headers, body = fake[-1]
    assert body["model"] == HAIKU and "fallbacks" not in body
    assert ai_client.FALLBACK_BETA not in headers.get("anthropic-beta", "")
    assert shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()}).status_code == 200
    headers, body = fake[-1]
    assert body["model"] == SONNET and body["fallbacks"] == "default"
    assert ai_client.FALLBACK_BETA in headers["anthropic-beta"]
    assert body["output_config"]["effort"] == ai_client.EFFORT_INVOICE
    assert body["output_config"]["format"]["type"] == "json_schema"


def test_fixed_instructions_then_the_shops_stock_list(client, fake, monkeypatch):
    """Claude chooses from the shop's own products (owner, 8 Nov 2026: the same plant must come back as the same
    product). The fixed instructions stay the same bytes for every shop; the list follows them, sorted, so the same
    stock sends the same bytes (cached for that shop's next scans)."""
    shop = Shop(client)
    shop.product("Zamioculcas Special", 900, 2)
    shop.product("Money Plant", 250, 10, botanical_name="Epipremnum aureum")
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    _, body = fake[-1]
    stock = ai_client.STOCK_HEAD + "\n- Money Plant (Epipremnum aureum)\n- Zamioculcas Special"
    # instructions + list are past the model's cache minimum: the mark at the end caches both for this shop
    assert body["system"] == [{"type": "text", "text": ai_client.SYSTEM_PLANT},
                              {"type": "text", "text": stock, "cache_control": {"type": "ephemeral"}}]
    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "text"]
    assert "Indoor Plants, Pots" in content[-1]["text"]
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64(color=(1, 2, 3))}).status_code == 200
    assert fake[-1][1]["system"] == body["system"]  # the same stock, the same bytes
    # below the minimum (here: a higher threshold) the mark is left out, as it would do nothing
    monkeypatch.setenv("AI_CACHE_MIN_TOKENS", "100000")
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    assert fake[-1][1]["system"][-1] == {"type": "text", "text": stock}
    monkeypatch.delenv("AI_CACHE_MIN_TOKENS")
    other = Shop(client, phone="9811155667", name="Bina")  # a shop without products says so
    assert other.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    assert fake[-1][1]["system"][1]["text"] == ai_client.STOCK_NONE
    # the bill and name look-ups are unchanged: one fixed system prompt
    assert shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()}).status_code == 200
    assert _system_text(fake[-1][1]) == ai_client.SYSTEM_INVOICE


def _plant(**answer):
    return lambda body: (200, claude_message(json.dumps({**PLANT, **answer})))


def test_claudes_choice_from_the_stock_list_wins_and_is_checked(client, fake):
    shop = Shop(client)
    pothos = shop.product("Pothos Golden 6 inch", 180, 8)
    snake = shop.product("Snake Plant", 300, 4)
    money = shop.product("Money Plant", 250, 10)
    # Claude named it freely as "Money Plant" but chose the shop's own "Pothos Golden 6 inch" from the list
    fake.reply = _plant(stock_match="Pothos Golden 6 inch", stock_alternatives=["Snake Plant", "Invented Plant"])
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    assert r["matched_product"]["id"] == pothos["id"] and r["matched_catalog_name"] == "Pothos Golden 6 inch"
    ids = [p["id"] for p in r["recommended_products"]]
    assert ids[0] == snake["id"] and money["id"] in ids and pothos["id"] not in ids  # never a made-up name
    assert len(ids) <= 4 and all("selling_price" in p for p in r["recommended_products"])
    assert r["similar_products"] == []  # older apps: only when nothing matched, as before
    # a name that is not in the shop is ignored: back to comparing names
    fake.reply = _plant(stock_match="Golden Devil's Ivy", stock_alternatives=[])
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()["matched_product"]["id"] == money["id"]
    # case, spacing and a copied botanical name in brackets do not matter
    fake.reply = _plant(plant_name="Snake", scientific_name="", alternatives=[],
                        stock_match="  snake  plant (Dracaena trifasciata)")
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()["matched_product"]["id"] == snake["id"]


def test_names_in_other_languages_are_recommended_from_stock(client, fake):
    """No exact match, but the shop's search knows the plant under another name: shown as "in your stock"."""
    shop = Shop(client)
    rose = shop.product("Rose", 120, 6)
    fake.reply = _plant(plant_name="Gulab", scientific_name="", alternatives=[], stock_match="", stock_alternatives=[])
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    assert r["matched_product"] is None
    assert [p["id"] for p in r["recommended_products"]] == [rose["id"]]
    assert [p["id"] for p in r["similar_products"]] == [rose["id"]]  # older apps show it too


def test_the_same_photo_is_answered_from_memory(client, fake, monkeypatch):
    monkeypatch.setenv("AI_PHOTO_CACHE_HOURS", "24")
    shop = Shop(client)
    shop.product("Money Plant", 250, 10)
    first = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    fake.reply = _plant(plant_name="Something Else", scientific_name="", alternatives=[])  # would differ
    again = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    assert again == first and len(fake) == 1  # no second call to Claude ...
    assert _usage(shop)["count"] == 1  # ... and no second scan used
    other = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64(color=(9, 9, 9))}).json()
    assert other["plant_name"] == "Something Else" and len(fake) == 2  # another photo is asked
    run(db().ai_photo_answers.update_many({}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}}))
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()["plant_name"] == "Something Else"
    assert len(fake) == 3  # forgotten after its time
    # an answer Claude could not give is not remembered: the next try asks again
    fake.reply = lambda body: (200, claude_message("not json"))
    photo = photo_b64(color=(50, 60, 70))
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo}).status_code == 422
    fake.reply = _plant()
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo}).status_code == 200 and len(fake) == 5
    # another shop never gets this shop's answer
    other_shop = Shop(client, phone="9811155667", name="Bina")
    assert other_shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200 and len(fake) == 6


def test_identification_still_matches_without_the_catalogue(client, fake):
    shop = Shop(client)
    money = shop.product("Money Plant", 250, 10)
    rose = shop.product("Gulab Red", 90, 5, botanical_name="Rosa indica")
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    assert r["matched_product"]["id"] == money["id"] and r["matched_catalog_name"] == "Money Plant"
    assert r["matched_product"]["selling_price"] == 250  # the full product view, not just the name
    fake.reply = lambda body: (200, claude_message(json.dumps({
        **PLANT, "plant_name": "Red Rose", "scientific_name": "Rosa indica", "alternatives": []})))
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    assert r["matched_product"]["id"] == rose["id"]
    fake.reply = lambda body: (200, claude_message(json.dumps({
        **PLANT, "plant_name": "Snake Plant", "scientific_name": "", "alternatives": []})))
    shop.product("Snake Plant 6 inch", 300, 3)
    shop.product("Snake Plant 8 inch", 450, 3)
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()
    assert r["matched_product"] is None and r["matched_catalog_name"] == ""
    assert {p["name"] for p in r["similar_products"]} == {"Snake Plant 6 inch", "Snake Plant 8 inch"}
    assert all("selling_price" in p for p in r["similar_products"])


# --- usage log and budget ------------------------------------------------------------------------------------
def test_every_call_is_logged_with_tokens_and_cost(client, fake):
    fake.reply = lambda body: (200, claude_message(json.dumps(PLANT), model="claude-opus-5-5", usage={
        "input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 2000}))
    shop = Shop(client)
    r = client.post("/api/ai/identify-plant", json={"image_base64": photo_b64()},
                    headers={**shop.h, "X-Request-ID": "0123456789abcdef0123456789abcdef"})
    assert r.status_code == 200
    call = run(db().ai_calls.find_one({}, {"_id": 0}))
    assert call["kind"] == "plant" and call["outcome"] == "ok"
    assert call["model"] == "claude-opus-5-5" and call["requested_model"] == SONNET  # a fallback answered
    assert (call["input_tokens"], call["output_tokens"], call["cache_read_input_tokens"]) == (1000, 100, 2000)
    # Opus 5.5: $4 in, $20 out, cache reads at 0.1x the input price
    assert call["cost_usd"] == pytest.approx((1000 * 4 + 2000 * 4 * 0.1 + 100 * 20) / 1e6)
    assert call["request_id"] == "0123456789abcdef0123456789abcdef" and call["anthropic_request_id"] is None
    assert call["business_id"] == _bid(shop) and call["user_id"] and call["latency_ms"] >= 0
    assert call["images"] == 1 and call["at"]
    spend = run(db().ai_spend.find_one({"_id": ai_usage.month()}))
    assert spend["cost_usd"] == pytest.approx(call["cost_usd"]) and spend["calls"] == 1


def test_prices():
    assert ai_usage.price(SONNET) == (2.0, 10.0) and ai_usage.price(HAIKU) == (0.1, 0.5)
    assert ai_usage.price("claude-sonnet-5") == (2.0, 10.0)
    assert ai_usage.price("claude-unknown-9") == max(ai_usage.PRICES.values())  # errs on the dear side


def test_monthly_budget_pauses_scans(client, fake, monkeypatch):
    shop = Shop(client)
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    spent = run(db().ai_spend.find_one({"_id": ai_usage.month()}))["cost_usd"]
    monkeypatch.setenv("AI_MONTHLY_BUDGET_USD", str(spent))  # this month's cost has reached it
    calls = len(fake)
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    assert r.status_code == 503 and r.json()["code"] == "ai_budget_reached"
    assert r.json()["detail"].startswith("Photo recognition is paused")
    r = shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()})
    assert r.status_code == 503 and r.json()["code"] == "ai_budget_reached"
    assert len(fake) == calls  # Claude was not called
    assert _usage(shop)["count"] == 1  # and the refused scans were given back
    monkeypatch.setenv("AI_MONTHLY_BUDGET_USD", str(spent * 10))
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200


# --- errors, breaker, concurrency ----------------------------------------------------------------------------
@pytest.mark.parametrize("status,kind", [(401, "authentication_error"), (402, "billing_error"),
                                         (403, "permission_error"), (404, "not_found_error")])
def test_key_and_billing_errors_give_the_scan_back_and_alert(client, fake, monkeypatch, status, kind):
    alerts = []
    monkeypatch.setattr(ai_client.sentry_sdk, "capture_exception", lambda e, *a, **k: alerts.append(e))
    fake.reply = _error(status, kind)
    shop = Shop(client)
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    assert r.status_code == 503 and r.json()["code"] == "ai_unavailable"
    assert len(alerts) == 1 and alerts[0].status_code == status
    assert _usage(shop)["count"] == 0
    call = run(db().ai_calls.find_one({}))
    assert call["outcome"] == "error" and call["error"] == kind and call["cost_usd"] == 0


def test_rate_limited_by_anthropic_is_busy_and_does_not_trip_the_breaker(client, fake, monkeypatch):
    monkeypatch.setenv("AI_BREAKER_FAILURES", "2")
    fake.reply = _error(429, "rate_limit_error")
    shop = Shop(client)
    for _ in range(3):
        r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
        assert r.status_code == 503 and r.json()["code"] == "ai_busy" and r.headers["Retry-After"] == "10"
    assert ai_guard._local["breaker"].state == "closed"
    assert _usage(shop)["count"] == 0


def test_breaker_opens_on_an_outage_and_closes_after_a_good_trial(client, fake, monkeypatch):
    monkeypatch.setenv("AI_BREAKER_FAILURES", "3")
    monkeypatch.setenv("AI_BREAKER_RESET_SECONDS", "30")
    fake.reply = _error(529, "overloaded_error")
    shop = Shop(client)
    for _ in range(3):
        assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 503
    breaker = ai_guard._local["breaker"]
    assert breaker.state == "open" and len(fake) == 3
    started = time.monotonic()
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    assert r.status_code == 503 and time.monotonic() - started < 5  # at once, without calling Claude
    assert r.json()["code"] == "ai_unavailable" and r.headers["Retry-After"] == "30"
    assert len(fake) == 3 and _usage(shop)["count"] == 0
    # after the pause one trial call goes through; Claude is back, so the breaker closes
    fake.reply = Fake().reply
    breaker.opened_at -= 31
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    assert breaker.state == "closed" and len(fake) == 4
    # a failed trial reopens it at once
    fake.reply = _error(500, "api_error")
    breaker.failures, breaker.opened_at = 3, time.monotonic() - 31
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 503
    assert breaker.state == "open" and len(fake) == 5


def test_connection_errors_count_for_the_breaker(client, monkeypatch):
    monkeypatch.setenv("AI_BREAKER_FAILURES", "2")

    def down(req):
        raise httpx2.ConnectError("no route")

    ai_client._client = fake_claude_client(down, max_retries=0)
    try:
        shop = Shop(client)
        for _ in range(2):
            r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
            assert r.status_code == 503 and r.json()["detail"].startswith("Could not reach")
        assert ai_guard._local["breaker"].state == "open"
        assert run(db().ai_calls.count_documents({"outcome": "unreachable"})) == 2
    finally:
        ai_client._client = None


def test_process_slots_and_busy(monkeypatch):
    monkeypatch.setenv("AI_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AI_QUEUE_WAIT_SECONDS", "0.2")

    async def scenario():
        ai_guard.reset()
        order = []
        async with ai_guard.slot():
            with pytest.raises(ai_guard.Busy):  # the only slot is taken, and the wait is short
                async with ai_guard.slot():
                    pass

            async def later():
                async with ai_guard.slot():
                    order.append("second")
            task = asyncio.create_task(later())
            await asyncio.sleep(0.05)
            order.append("first")
        await task  # got the slot once the first call let go
        return order

    assert asyncio.run(scenario()) == ["first", "second"]
    ai_guard.reset()


def test_redis_down_lets_calls_through(monkeypatch):
    """With REDIS_URL set but Redis unreachable the shared limit is skipped (logged), never blocking scans."""
    class S:
        redis_url = "redis://127.0.0.1:1/0"

    monkeypatch.setattr(ai_guard, "get_settings", lambda: S())

    async def scenario():
        ai_guard.reset()
        async with ai_guard.slot():
            pass
        started = time.monotonic()
        async with ai_guard.slot():  # Redis is not tried again for a while: no wait for its timeout
            pass
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 0.5
    ai_guard.reset()


@pytest.mark.skipif(not os.environ.get("REDIS_TEST_URL"), reason="needs a Redis server (REDIS_TEST_URL)")
def test_shared_slots_in_redis(monkeypatch):
    class S:
        redis_url = os.environ["REDIS_TEST_URL"]

    monkeypatch.setattr(ai_guard, "get_settings", lambda: S())
    monkeypatch.setattr(ai_guard, "SLOT_KEY", f"hh-ai:test:{os.getpid()}")
    monkeypatch.setenv("AI_GLOBAL_CONCURRENCY", "1")
    monkeypatch.setenv("AI_QUEUE_WAIT_SECONDS", "0.3")

    async def scenario():
        ai_guard.reset()
        async with ai_guard.slot():
            ai_guard._local.update(loop=None)  # as if a second process: its own semaphore, the same Redis set
            with pytest.raises(ai_guard.Busy):
                async with ai_guard.slot():
                    pass
        async with ai_guard.slot():  # freed
            pass
        await ai_guard._redis["client"].delete(ai_guard.SLOT_KEY)

    asyncio.run(scenario())
    ai_guard.reset()


def test_breaker_unit():
    b = ai_guard.CircuitBreaker(2, 10)
    b.failure()
    b.check()
    b.failure()
    with pytest.raises(ai_guard.Open):
        b.check()
    b.opened_at -= 11
    b.check()  # the trial call
    with pytest.raises(ai_guard.Open):
        b.check()  # only one at a time
    b.release()  # the trial was cancelled: the next call may try
    b.check()
    b.success()
    assert b.state == "closed"


# --- answers cut off, refusals --------------------------------------------------------------------------------
def test_answer_cut_off_at_max_tokens(client, fake):
    fake.reply = lambda body: (200, claude_message('{"is_bill": true, "items": [', "max_tokens"))
    shop = Shop(client)
    r = shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()})
    assert r.status_code == 422 and r.json()["code"] == "bill_too_long"
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    assert r.status_code == 422 and r.json()["code"] == "not_identified"
    assert run(db().ai_calls.count_documents({"outcome": "max_tokens"})) == 2
    assert _usage(shop)["count"] == 2  # Claude did the work: these count


# --- photos ---------------------------------------------------------------------------------------------------
def _png_header(width, height) -> bytes:
    """A PNG that claims width x height pixels: only the header, as an attacker would send it."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


def test_huge_png_is_refused_without_decoding():
    tracemalloc.start()
    try:
        for w, h in ((13_000, 13_000), (8_000, 6_000)):  # 170 MP (Pillow's bomb error), 48 MP (over our limit)
            with pytest.raises(ai_client.BadImage, match="too large"):
                ai_client.normalize_photo(_png_header(w, h))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5 * 1024 * 1024


def test_huge_png_on_the_route_gives_the_scan_back(client, fake):
    shop = Shop(client)
    raw = base64.b64encode(_png_header(13_000, 13_000)).decode()
    r = shop.post("/api/ai/identify-plant", {"image_base64": raw})
    assert r.status_code == 400 and "too large" in r.json()["detail"]
    assert len(fake) == 0 and _usage(shop)["count"] == 0


def test_formats():
    for fmt in ("JPEG", "PNG", "WEBP"):
        out = ai_client.normalize_photo(base64.b64decode(photo_b64((3000, 2000), fmt)))
        img = Image.open(io.BytesIO(out))
        assert img.format == "JPEG" and max(img.size) == ai_client.MAX_IMAGE_EDGE
    gif = io.BytesIO()
    Image.new("RGB", (50, 50)).save(gif, "GIF")
    with pytest.raises(ai_client.BadImage, match="could not be read"):
        ai_client.normalize_photo(gif.getvalue())


def test_normalised_pages_are_sent_as_they_are(client, fake):
    shop = Shop(client)
    paths = [shop.post("/api/upload-base64", {"image_base64": photo_b64((3000, 2000))}).json()["path"] for _ in range(2)]
    import storage_client
    stored = [base64.b64encode(storage_client.get_object(p)[0]).decode() for p in paths]
    r = shop.post("/api/ai/scan-invoice", {"image_paths": paths})
    assert r.status_code == 200, r.text
    _, body = fake[-1]
    sent = [c["source"]["data"] for c in body["messages"][0]["content"] if c["type"] == "image"]
    assert sent == stored  # not decoded and encoded again
    # a photo that was not normalised is still checked and shrunk
    big = base64.b64decode(photo_b64((4000, 3000)))
    assert ai_client.prepare_image(big, normalised=True) != big


# --- allowances, rate limit, migration ------------------------------------------------------------------------
def test_allowance_per_period_and_kind(client, fake):
    shop = Shop(client)
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code == 200
    doc = _usage(shop)
    assert (doc["kind"], doc["period"], doc["count"]) == ("scan", date_key(), 1)
    indexes = {i["name"]: i for i in run(_indexes())}
    idx = indexes["business_period_kind"]
    assert idx["unique"] and list(idx["key"]) == ["business_id", "period", "kind"]
    for _ in range(4):
        shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    assert r.status_code == 429  # AI_DAILY_LIMIT=5 in tests
    assert _usage(shop)["count"] == 5  # the refused scan is not counted


async def _indexes():
    return [i async for i in await db().ai_usage.list_indexes()]


def test_per_user_rate_limit(client, fake, monkeypatch):
    monkeypatch.setenv("AI_USER_RATE", "2/minute")
    shop = Shop(client)
    codes = [shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "Retry-After" in r.headers
    assert _usage(shop)["count"] == 2  # refused before the allowance was touched


def test_migration_merges_old_daily_counts(client):
    migration = importlib.import_module("migrations.0601_ai_usage_and_calls")
    run(db().ai_usage.drop_indexes())
    run(db().ai_usage.delete_many({}))
    run(db().ai_usage.insert_many([
        {"business_id": "b1", "date": "2026-11-01", "count": 3},
        {"business_id": "b1", "date": "2026-11-01", "count": 2},  # the race the unique index now prevents
        {"business_id": "b1", "date": "2026-11-02", "count": 1},
        {"business_id": "b2", "date": "2026-11-01", "count": 7},
    ]))
    run(migration.up(db()))
    run(migration.up(db()))  # twice changes nothing
    docs = run(db().ai_usage.find({}, {"_id": 0, "business_id": 1, "period": 1, "kind": 1, "count": 1})
               .sort([("business_id", 1), ("period", 1)]).to_list(None))
    assert docs == [{"business_id": "b1", "period": "2026-11-01", "kind": "scan", "count": 5},
                    {"business_id": "b1", "period": "2026-11-02", "kind": "scan", "count": 1},
                    {"business_id": "b2", "period": "2026-11-01", "kind": "scan", "count": 7}]
    ttl = {i["name"]: i for i in run(_call_indexes())}["at_1"]
    assert ttl["expireAfterSeconds"] == migration.KEEP_DAYS * 86400


async def _call_indexes():
    return [i async for i in await db().ai_calls.list_indexes()]


def test_a_call_longer_than_the_app_waits_is_given_back(client, monkeypatch):
    """Review fix: the app gave up after 45 s while a bill read could take 3 x 120 s, and the scan was charged.
    Now the whole call has a limit below the app's (TOTAL_TIMEOUT); past it the scan is given back."""
    assert ai_client.TOTAL_TIMEOUT["invoice"] < 180 and ai_client.TOTAL_TIMEOUT["plant"] < 75  # frontend/src/api.ts
    assert ai_client.TOTAL_TIMEOUT["invoice"] >= ai_client.CALL_TIMEOUT["invoice"]

    async def slow(**params):
        await asyncio.sleep(5)

    class Slow:
        class beta:
            class messages:
                create = staticmethod(slow)

    monkeypatch.setattr(ai_client, "_client", Slow())
    monkeypatch.setitem(ai_client.TOTAL_TIMEOUT, "plant", 0.2)
    shop = Shop(client)
    started = time.monotonic()
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()})
    assert r.status_code == 503 and r.json()["code"] == "ai_timeout", r.text
    assert time.monotonic() - started < 4
    assert _usage(shop)["count"] == 0
    call = run(db().ai_calls.find_one({"kind": "plant"}, {"_id": 0}))
    assert call["outcome"] == "timeout"
