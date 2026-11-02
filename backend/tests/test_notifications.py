"""Owner notifications: phone sign-up and removal, low-stock alerts, the daily summary (schedule, claim, spread,
load), the push jobs (retries, receipts) and the worker. Nothing is sent for real: `push.dispatch` is replaced
to collect the messages, and `push.send` / `push.get_receipts` get a fake Expo."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from arq import Retry

import db as db_module
import jobs
import notify
import push
from common import IST, date_key
from conftest import Shop, run

TOKEN = "ExponentPushToken[abcdefgh12345678]"
TOKEN2 = "ExponentPushToken[zyxwvuts87654321]"


@pytest.fixture
def sent(monkeypatch):
    """Every message the server would send, in order."""
    out = []

    async def collect(_db, messages, key=None):
        out.extend(messages)
    monkeypatch.setattr(push, "dispatch", collect)
    return out


@pytest.fixture
def queued(monkeypatch):
    """Every job queued: (name, args, kwargs)."""
    out = []

    async def enqueue(name, *args, **kwargs):
        out.append((name, args, kwargs))
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    return out


def _order(shop, items, headers=None, **extra):
    return shop.c.post("/api/invoices", headers=headers or shop.h,
                       json={"items": items, "customer_phone": "9811100099", "customer_name": "Test", **extra})


def _sign_up(shop, token=TOKEN, lang="en", headers=None):
    r = shop.c.post("/api/push-tokens", headers=headers or shop.h, json={"token": token, "lang": lang, "platform": "android"})
    assert r.status_code == 200, r.text


def _run(coro):
    return run(coro)


def _claims(shop) -> dict:
    return jwt.decode(shop.token, options={"verify_signature": False})


def _row(shop) -> dict | None:
    return _run(db_module.state.db.summary_schedule.find_one({"_id": _claims(shop)["bid"]}))


def _at(utc_naive: datetime) -> datetime:
    """A schedule time (stored as UTC) as India time, for `send_due_summaries(db, now)`."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(IST)


def test_only_owners_sign_up_a_phone(client):
    shop = Shop(client)
    _, worker = shop.worker()
    assert client.post("/api/push-tokens", headers=worker, json={"token": TOKEN}).status_code == 403
    assert shop.post("/api/push-tokens", {"token": "not-a-token"}).status_code == 422
    _sign_up(shop)
    _sign_up(shop)  # the same phone again: still one row
    assert shop.get("/api/notifications/settings").json()["phones"] == 1
    # an employee of the shop logging in on that phone stops the owner's notifications there
    r = client.post("/api/push-tokens/remove", headers=worker, json={"token": TOKEN})
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 1}
    assert shop.get("/api/notifications/settings").json()["phones"] == 0


def test_a_phone_belongs_to_the_last_owner(client, sent):
    a, b = Shop(client), Shop(client, phone="9822200011", name="Mohan")
    _sign_up(a)
    _sign_up(b)  # the phone moved to another shop's owner
    rose = a.product("Rose", 100, 6, min_stock=5)
    _order(a, [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}])
    assert sent == []  # shop A's alert must not reach the phone that is now shop B's
    assert a.get("/api/notifications/settings").json()["phones"] == 0
    assert b.get("/api/notifications/settings").json()["phones"] == 1


def test_low_stock_alert_once_per_product_a_day(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    _sign_up(shop, TOKEN2, lang="hi")
    rose = shop.product("Rose", 100, 8, min_stock=5)
    tulsi = shop.product("Tulsi", 50, 2, min_stock=1)
    sell = [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}]
    _order(shop, sell)  # 8 -> 6
    assert sent == []
    _order(shop, sell)  # 6 -> 4: low
    assert [m["to"] for m in sent] == [TOKEN, TOKEN2]
    en, hi = sent
    assert en["title"] == "Low stock: Rose" and en["body"] == "Only 4 pcs left. Your alert is at 5 pcs."
    assert hi["title"] == "स्टॉक कम: Rose"
    assert en["data"] == {"type": "stock", "stock": "low", "product_id": rose["id"]} and en["channelId"] == "alerts"
    _order(shop, [{**sell[0], "quantity": 4}])  # 4 -> 0: out, but Rose was told already today
    assert len(sent) == 2
    _order(shop, [{"product_id": tulsi["id"], "name": "Tulsi", "quantity": 2, "price": 50}])  # 2 -> 0, straight to out
    assert sent[2]["title"] == "Out of stock: Tulsi" and sent[2]["data"]["stock"] == "out"


def test_an_alert_that_could_not_be_queued_is_tried_again(client, monkeypatch, sent):
    shop = Shop(client)
    _sign_up(shop)
    rose = shop.product("Rose", 100, 10, min_stock=5)
    collect = push.dispatch

    async def queue_down(_db, messages, key=None):
        raise ConnectionError("redis is down")
    monkeypatch.setattr(push, "dispatch", queue_down)
    r = _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 6, "price": 100}])  # 10 -> 4: low
    assert r.status_code == 200, r.text  # the sale never fails because of an alert
    monkeypatch.setattr(push, "dispatch", collect)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])  # 4 -> 3, still low
    assert sent == []  # 4 -> 3 crosses no line ...
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 3, "price": 100}])  # 3 -> 0: out
    assert sent[0]["title"] == "Out of stock: Rose"  # ... but the claim was given back, so today's alert still goes


def test_stock_removed_by_hand_also_alerts(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    pot = shop.product("Clay Pot", 120, 6, category="Pots", min_stock=3)
    r = shop.post(f"/api/products/{pot['id']}/stock", {"change": -3, "reason": "Broken"})
    assert r.status_code == 200, r.text
    assert sent[0]["title"] == "Low stock: Clay Pot"


def test_alerts_off_and_employees_never_get_them(client, sent):
    shop = Shop(client)
    wid, worker = shop.worker()
    rose = shop.product("Rose", 100, 6, min_stock=5)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 2, "price": 100}])
    assert sent == []  # no owner phone signed up
    _sign_up(shop)
    r = shop.c.put("/api/notifications/settings", headers=shop.h, json={"low_stock": False})
    assert r.status_code == 200 and r.json()["low_stock"] is False and r.json()["summary"] is True
    tulsi = shop.product("Tulsi", 50, 6, min_stock=5)
    _order(shop, [{"product_id": tulsi["id"], "name": "Tulsi", "quantity": 2, "price": 50}], headers=worker)
    assert sent == []
    assert client.put("/api/notifications/settings", headers=worker, json={"low_stock": True}).status_code == 403
    assert client.get("/api/notifications/settings", headers=worker).status_code == 403


def test_settings_check_the_time(client):
    shop = Shop(client)
    s = shop.get("/api/notifications/settings").json()
    assert s == {"low_stock": True, "summary": True, "summary_time": "21:00", "phones": 0, "push_on": False}
    assert shop.c.put("/api/notifications/settings", headers=shop.h, json={"summary_time": "25:00"}).status_code == 422
    r = shop.c.put("/api/notifications/settings", headers=shop.h, json={"summary_time": "20:30"})
    assert r.json()["summary_time"] == "20:30"


# ---- the daily summary

def test_daily_summary(client, sent):
    shop = Shop(client)
    rose = shop.product("Rose", 100, 50, min_stock=5)
    shop.product("Fern", 80, 2, min_stock=5)
    for method, qty in (("Cash", 2), ("UPI", 1), ("UPI", 3)):
        assert _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": qty, "price": 100}],
                      payment_method=method).status_code == 200
    bill = _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 10, "price": 100}]).json()
    assert shop.post(f"/api/invoices/{bill['id']}/cancel", {"reason": "Wrong bill"}).status_code == 200
    db = db_module.state.db
    today = datetime.now(IST)

    assert _run(notify.send_due_summaries(db, today.replace(hour=23))) == 0  # no phone signed up
    assert _row(shop) is None
    _sign_up(shop)
    row = _row(shop)
    due = _at(row["due"])
    assert row["day"] == date_key(today) and due.strftime("%H:%M") == notify.summary_clock(_claims(shop)["bid"], None)
    assert _run(notify.send_due_summaries(db, due - timedelta(minutes=1))) == 0  # not its time yet
    assert _run(notify.send_due_summaries(db, due)) == 1
    assert sent[-1]["title"] == "Today's sales"
    assert sent[-1]["body"] == "₹600 from 3 bills (Cash ₹200 / UPI ₹400). 1 product is low on stock."
    assert sent[-1]["data"] == {"type": "summary", "date": date_key(today)}
    assert _run(notify.send_due_summaries(db, due)) == 0  # once a day, however often it is checked
    assert len(sent) == 1
    tomorrow = _row(shop)  # on to tomorrow, same minute
    assert tomorrow["day"] == date_key(today + timedelta(days=1)) and tomorrow["due"] - row["due"] == timedelta(days=1)


def test_summary_skips_a_day_without_bills_and_can_be_off(client, sent):
    shop = Shop(client)
    _sign_up(shop, lang="hi")
    db = db_module.state.db
    late = datetime.now(IST).replace(hour=22, minute=0)
    assert _run(notify.send_due_summaries(db, late)) == 0 and sent == []
    other = Shop(client, phone="9822200011", name="Mohan")
    _sign_up(other, TOKEN2)
    assert _row(other) is not None
    other.c.put("/api/notifications/settings", headers=other.h, json={"summary": False})
    assert _row(other) is None  # off: out of the schedule
    rose = other.product("Rose", 100, 50)
    _order(other, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    assert _run(notify.send_due_summaries(db, late)) == 0 and sent == []
    other.c.put("/api/notifications/settings", headers=other.h, json={"summary": True})
    assert _run(notify.send_due_summaries(db, late)) == 1 and sent[0]["to"] == TOKEN2


def test_a_picked_time_moves_the_schedule(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    rose = shop.product("Rose", 100, 50)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    shop.c.put("/api/notifications/settings", headers=shop.h, json={"summary_time": "18:45"})
    row = _row(shop)
    assert _at(row["due"]).strftime("%H:%M") == "18:45" and row["day"] == date_key()
    db = db_module.state.db
    assert _run(notify.send_due_summaries(db, _at(row["due"]))) == 1
    # a later time picked after today's went out: nothing more today, tomorrow at the new time
    shop.c.put("/api/notifications/settings", headers=shop.h, json={"summary_time": "20:15"})
    assert _run(notify.send_due_summaries(db, datetime.now(IST).replace(hour=20, minute=20))) == 0
    row = _row(shop)
    assert row["day"] == date_key(datetime.now(IST) + timedelta(days=1)) and _at(row["due"]).strftime("%H:%M") == "20:15"
    assert len(sent) == 1


def test_default_times_are_spread_by_shop():
    clocks = [notify.summary_clock(f"shop-{i}", None) for i in range(300)]
    assert all("21:00" <= c <= "21:19" for c in clocks)
    assert len(set(clocks)) == notify.SPREAD_MINUTES  # every minute of the window is used
    assert notify.summary_clock("shop-7", None) == notify.summary_clock("shop-7", {})  # stable
    assert notify.summary_clock("shop-7", {"summary_time": "21:00"}) == "21:00"  # a picked time is kept


def test_three_runners_send_each_summary_once(client, sent):
    """Several workers (or overlapping runs) at the same minute: every shop still gets exactly one summary."""
    shops = [Shop(client, phone=f"98{i:08d}", name=f"Owner{i}") for i in range(4)]
    for i, shop in enumerate(shops):
        _sign_up(shop, f"ExponentPushToken[shopphone{i:04d}]")
        rose = shop.product("Rose", 100, 50)
        _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    db = db_module.state.db
    late = datetime.now(IST).replace(hour=23, minute=0)

    async def three():
        return await asyncio.gather(*(notify.send_due_summaries(db, late) for _ in range(3)))
    assert sum(_run(three())) == 4
    assert sorted(m["to"] for m in sent) == sorted(f"ExponentPushToken[shopphone{i:04d}]" for i in range(4))


def test_a_summary_claimed_by_a_run_that_died_is_sent_later(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    rose = shop.product("Rose", 100, 50)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    db = db_module.state.db
    key = f"summary:{_claims(shop)['bid']}:{date_key()}"
    late = datetime.now(IST).replace(hour=23, minute=0)
    _run(db.push_marks.insert_one({"_id": key, "at": datetime.now(timezone.utc), "state": "claimed"}))
    assert _run(notify.send_due_summaries(db, late)) == 0  # another run is on it: left alone, still due
    assert _row(shop)["day"] == date_key()
    stale = datetime.now(timezone.utc) - notify.STALE_CLAIM - timedelta(minutes=1)
    _run(db.push_marks.update_one({"_id": key}, {"$set": {"at": stale}}))  # ... and that run died
    assert _run(notify.send_due_summaries(db, late)) == 1 and len(sent) == 1
    assert _run(db.push_marks.find_one({"_id": key}))["state"] == "sent"


def test_a_marker_from_before_the_schedule_counts_as_sent(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    rose = shop.product("Rose", 100, 50)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    db = db_module.state.db
    _run(db.push_marks.insert_one({"_id": f"summary:{_claims(shop)['bid']}:{date_key()}", "at": datetime.now(timezone.utc)}))
    assert _run(notify.send_due_summaries(db, datetime.now(IST).replace(hour=23))) == 0 and sent == []
    assert _row(shop)["day"] == date_key(datetime.now(IST) + timedelta(days=1))


def test_a_missed_day_is_not_sent_the_next_morning(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    rose = shop.product("Rose", 100, 50)
    _order(shop, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    db = db_module.state.db
    bid = _claims(shop)["bid"]
    yesterday = date_key(datetime.now(IST) - timedelta(days=1))
    _run(db.summary_schedule.update_one({"_id": bid}, {"$set": {
        "day": yesterday, "due": notify.summary_due(bid, None, yesterday)}}))  # the worker was down last night
    assert _run(notify.send_due_summaries(db)) == 0 and sent == []
    assert _row(shop)["day"] == date_key()  # today's goes at its time


def test_a_shop_without_phones_leaves_the_schedule(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    r = shop.post("/api/auth/password", {"current_password": "secret123", "new_password": "newsecret123"})
    assert r.status_code == 200, r.text
    assert _run(notify.send_due_summaries(db_module.state.db, datetime.now(IST).replace(hour=23))) == 0
    assert _row(shop) is None


def test_1000_shops_at_nine_pm(client, sent):
    """Acceptance: 1000 shops due at once are done in well under 5 minutes, each exactly once."""
    db = db_module.state.db
    n, day = 1000, date_key()
    due = datetime.now(timezone.utc) - timedelta(minutes=1)

    async def seed():
        await db.businesses.insert_many([{"id": f"b{i}", "name": f"Shop {i}"} for i in range(n)])
        await db.users.insert_many([{"id": f"u{i}", "business_id": f"b{i}", "role": "owner"} for i in range(n)])
        await db.push_tokens.insert_many([{"token": f"ExponentPushToken[load{i:06d}]", "user_id": f"u{i}",
                                           "business_id": f"b{i}", "lang": "en"} for i in range(n)])
        await db.invoices.insert_many([{"id": f"i{i}-{k}", "business_id": f"b{i}", "date_key": day, "status": "Paid",
                                        "grand_total": 100.0 * (k + 1), "payment_method": "UPI" if k else "Cash"}
                                       for i in range(n) for k in range(3)])
        await db.products.insert_many([{"id": f"p{i}", "business_id": f"b{i}", "stock": 1, "min_stock": 2,
                                        "deleted_at": None} for i in range(n)])
        await db.summary_schedule.insert_many([{"_id": f"b{i}", "due": due, "day": day} for i in range(n)])
    _run(seed())
    start = time.monotonic()
    assert _run(notify.send_due_summaries(db)) == n
    took = time.monotonic() - start
    assert took < 60, f"{n} summaries took {took:.1f} s"
    assert len(sent) == n and len({m["to"] for m in sent}) == n
    assert sent[0]["body"] == "₹600 from 3 bills (Cash ₹100 / UPI ₹500). 1 product is low on stock."
    assert _run(notify.send_due_summaries(db)) == 0


def test_migration_puts_shops_with_phones_on_the_schedule(client):
    import importlib

    shop = Shop(client)
    _sign_up(shop)
    db = db_module.state.db
    _run(db.summary_schedule.delete_many({}))
    up = importlib.import_module("migrations.0501_summary_schedule").up
    _run(up(db))
    _run(up(db))  # twice: the same
    assert _row(shop)["day"] == date_key()
    assert _run(db.summary_schedule.count_documents({})) == 1


def test_summary_text_in_hindi():
    s = {"sales": 125000, "bills": 1, "by_method": {"Cash": 125000, "UPI": 0, "Card": 0}, "low_stock": 0}
    assert notify.summary_message(s, "en") == ("Today's sales", "₹1,25,000 from 1 bill (Cash ₹1,25,000).")
    assert notify.summary_message(s, "hi") == ("आज की बिक्री", "1 बिल से ₹1,25,000 (नकद ₹1,25,000)।")


def test_summary_uses_the_rollup_when_there_is_one(client, monkeypatch):
    async def read_day(db, business_id, day):
        return {"orders": 2, "sales": 750.0, "by_method": {"Cash": 500.0, "Card": 250.0}}
    monkeypatch.setattr(notify.rollups, "read_day", read_day)
    s = _run(notify.day_summary(db_module.state.db, "b1", date_key()))
    assert s["bills"] == 2 and s["sales"] == 750 and s["by_method"] == {"Cash": 500, "UPI": 0, "Card": 250}


def test_test_notification(client, sent, monkeypatch):
    shop = Shop(client)
    _sign_up(shop)
    assert shop.post("/api/notifications/test").json() == {"sent": 0, "phones": 1, "push_on": False}
    monkeypatch.setenv("PUSH_ENABLED", "1")
    assert shop.post("/api/notifications/test").json() == {"sent": 1, "phones": 1, "push_on": True}
    assert sent[0]["title"] == "Notifications are on" and sent[0]["data"] == {"type": "test"}


def test_dashboard_low_stock_card(client):
    shop = Shop(client)
    shop.product("Rose", 100, 3, min_stock=5)
    shop.product("Fern", 80, 1, min_stock=5)
    shop.product("Tulsi", 50, 0)
    shop.product("Palm", 900, 20)
    d = shop.get("/api/dashboard/today").json()
    assert [(p["name"], p["stock"]) for p in d["low_stock"]] == [("Fern", 1), ("Rose", 3)]
    assert d["low_stock_count"] == 2 and d["out_of_stock_count"] == 1
    _, worker = shop.worker(reports=True)
    assert "low_stock" not in client.get("/api/dashboard/today", headers=worker).json()


def test_crossed_to_zero():
    assert notify.crossed(3, 0, 5) == "out_of_stock"
    assert notify.crossed(10, 0, 5) == "out_of_stock"
    assert notify.crossed(10, -2, 5) == "below_zero"
    assert notify.crossed(0, 0, 5) is None


# ---- the push jobs

def _expo(answer, seen, status=200, headers=None):
    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=answer, headers=headers)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_send_posts_to_expo(monkeypatch):
    monkeypatch.setenv("EXPO_ACCESS_TOKEN", "expo-key")
    seen = []
    msgs = [{"to": TOKEN, "title": "a", "body": "b"}]
    tickets = _run(push.send(msgs, _expo({"data": [{"status": "ok", "id": "1"}]}, seen)))
    assert tickets == [{"status": "ok", "id": "1"}]
    assert str(seen[0].url) == push.PUSH_URL and seen[0].headers["authorization"] == "Bearer expo-key"
    receipts = _run(push.get_receipts(["1"], _expo({"data": {"1": {"status": "ok"}}}, seen)))
    assert receipts == {"1": {"status": "ok"}} and str(seen[1].url) == push.RECEIPTS_URL


def test_one_http_client_per_process():
    async def twice():
        a, b = push.http(), push.http()
        await push.close()
        return a is b, a.is_closed
    assert _run(twice()) == (True, True)


def test_dispatch_queues_durable_jobs(client, queued, monkeypatch):
    monkeypatch.setenv("PUSH_ENABLED", "1")
    msgs = [{"to": f"ExponentPushToken[m{i:08d}]"} for i in range(250)]
    _run(push.dispatch(None, msgs, "summary:b1:2026-11-02"))
    assert [(name, len(args[0]), kw["_job_id"]) for name, args, kw in queued] == [
        ("deliver_push", 100, "push:summary:b1:2026-11-02:0"), ("deliver_push", 100, "push:summary:b1:2026-11-02:1"),
        ("deliver_push", 50, "push:summary:b1:2026-11-02:2")]
    monkeypatch.setenv("PUSH_ENABLED", "0")
    _run(push.dispatch(None, msgs))
    assert len(queued) == 3  # push off: nothing queued


def test_deliver_drops_phones_without_the_app_and_checks_receipts(client, monkeypatch, queued):
    db = db_module.state.db

    async def fake_send(messages, client=None):
        return [{"status": "ok", "id": "t1"},
                {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}]
    monkeypatch.setattr(push, "send", fake_send)

    async def go():
        await db.push_tokens.insert_many([{"token": TOKEN}, {"token": TOKEN2}])
        await push.deliver_push({"db": db, "job_try": 1}, [{"to": TOKEN}, {"to": TOKEN2}])
        return [t["token"] for t in await db.push_tokens.find({}).to_list(10)]
    assert _run(go()) == [TOKEN]
    assert queued == [("check_push_receipts", ({"t1": TOKEN},), {"_defer_by": push.RECEIPT_DELAY})]


def test_a_failed_send_is_tried_again_later(client, monkeypatch, queued):
    """The worker restarting or Expo being down does not lose the push: the job is tried again with back-off."""
    db = db_module.state.db
    calls = []

    async def down(messages, client=None):
        calls.append(messages)
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(push, "send", down)
    msgs = [{"to": TOKEN}]
    with pytest.raises(Retry) as e:  # in the worker: arq keeps the job in Redis and runs it again
        _run(push.deliver_push({"db": db, "job_try": 1}, msgs))
    assert e.value.defer_score == push.BACKOFF[0] * 1000
    _run(push.deliver_push({"db": db, "job_try": push.MAX_TRIES}, msgs))  # the last try: logged, given up
    _run(push.deliver_push({"db": db, "job_try": 1, "in_process": True}, msgs))  # no Redis: queued again here
    assert queued == [("deliver_push", (msgs,), {"attempt": 2, "_defer_by": push.BACKOFF[0]})]
    assert len(calls) == 3


def test_a_bad_request_is_not_tried_again_and_429_waits(client, monkeypatch, queued):
    db = db_module.state.db
    answer = {"status": 400, "headers": None}
    real_send = push.send

    async def refused(messages, client=None):
        return await real_send(messages, _expo({"errors": [{"code": "x"}]}, [], answer["status"], answer["headers"]))
    monkeypatch.setattr(push, "send", refused)
    _run(push.deliver_push({"db": db, "job_try": 1}, [{"to": TOKEN}]))  # 400: never sent, not raised
    answer.update(status=429, headers={"Retry-After": "300"})
    with pytest.raises(Retry) as e:
        _run(push.deliver_push({"db": db, "job_try": 1}, [{"to": TOKEN}]))
    assert e.value.defer_score == 300 * 1000  # Expo's Retry-After when longer than our back-off


def test_rate_limited_messages_are_sent_again(client, monkeypatch, queued):
    async def busy(messages, client=None):
        return [{"status": "ok", "id": "t1"}, {"status": "error", "details": {"error": "MessageRateExceeded"}}]
    monkeypatch.setattr(push, "send", busy)
    _run(push.deliver_push({"db": db_module.state.db, "job_try": 1, "in_process": True}, [{"to": TOKEN}, {"to": TOKEN2}]))
    assert queued == [("deliver_push", ([{"to": TOKEN2}],), {"attempt": 2, "_defer_by": push.BACKOFF[0]})]


def test_receipts_forget_phones_and_ask_again_for_missing_ones(client, monkeypatch, queued):
    db = db_module.state.db

    async def receipts(ids, client=None):
        return {"t1": {"status": "ok"}, "t2": {"status": "error", "details": {"error": "DeviceNotRegistered"}}}
    monkeypatch.setattr(push, "get_receipts", receipts)

    async def go():
        await db.push_tokens.insert_many([{"token": TOKEN}, {"token": TOKEN2}])
        await push.check_push_receipts({"db": db, "job_try": 1}, {"t1": TOKEN, "t2": TOKEN2, "t3": "later"})
        return [t["token"] for t in await db.push_tokens.find({}).to_list(10)]
    assert _run(go()) == [TOKEN]
    assert queued == [("check_push_receipts", ({"t3": "later"},), {"attempt": 2, "_defer_by": push.RECEIPT_DELAY})]
    queued.clear()
    _run(push.check_push_receipts({"db": db, "job_try": 1}, {"t3": "later"}, attempt=push.RECEIPT_TRIES))
    assert queued == []  # asked often enough


def test_jobs_run_in_the_process_without_redis(client, monkeypatch):
    """No REDIS_URL (a laptop, tests): dispatch still sends, in this process."""
    monkeypatch.setenv("PUSH_ENABLED", "1")
    got = []

    async def fake_send(messages, client=None):
        got.extend(messages)
        return [{"status": "ok", "id": "t1"}]
    monkeypatch.setattr(push, "send", fake_send)

    async def go():
        await push.dispatch(db_module.state.db, [{"to": TOKEN}], "test")
        await jobs.drain()
    _run(go())
    assert got == [{"to": TOKEN}]


# ---- removing a phone

def _login_token(shop, **change) -> str:
    """The shop owner's login, re-signed with `change` (e.g. an expiry in the past)."""
    import auth
    return jwt.encode({**_claims(shop), **change}, auth._secret(), algorithm="HS256")


def test_removing_a_phone_needs_a_login(client):
    shop = Shop(client)
    _sign_up(shop)
    assert client.post("/api/push-tokens/remove", json={"token": TOKEN}).status_code == 401
    forged = {"Authorization": "Bearer forged"}
    assert client.post("/api/push-tokens/remove", headers=forged, json={"token": TOKEN}).status_code == 401
    other = Shop(client, phone="9822200011", name="Mohan")
    r = client.post("/api/push-tokens/remove", headers=other.h, json={"token": TOKEN})  # another shop's login
    assert r.status_code == 200 and r.json()["removed"] == 0
    assert shop.get("/api/notifications/settings").json()["phones"] == 1
    assert client.post("/api/push-tokens/remove", json={"token": "x" * 301}).status_code == 422
    r = client.post("/api/push-tokens/remove", headers=shop.h, json={"token": TOKEN})
    assert r.json() == {"ok": True, "removed": 1}


def test_an_expired_login_still_removes_its_own_phone(client, monkeypatch):
    """Apps in testers' hands log out with the login they have, even an expired one (the narrow exception)."""
    shop = Shop(client)
    other = Shop(client, phone="9822200011", name="Mohan")
    _sign_up(shop)
    _sign_up(shop, TOKEN2)
    past = int(time.time()) - 3600
    expired = {"Authorization": f"Bearer {_login_token(shop, exp=past)}"}
    not_theirs = {"Authorization": f"Bearer {_login_token(other, exp=past)}"}
    assert client.post("/api/push-tokens/remove", headers=not_theirs, json={"token": TOKEN}).json()["removed"] == 0
    r = client.post("/api/push-tokens/remove", headers=expired, json={"token": TOKEN})
    assert r.status_code == 200 and r.json()["removed"] == 1
    too_old = {"Authorization": f"Bearer {_login_token(shop, exp=past - 200 * 86400)}"}
    assert client.post("/api/push-tokens/remove", headers=too_old, json={"token": TOKEN2}).status_code == 401
    monkeypatch.setenv("PUSH_REMOVE_LEGACY", "0")
    assert client.post("/api/push-tokens/remove", headers=expired, json={"token": TOKEN2}).json()["removed"] == 0
    assert shop.get("/api/notifications/settings").json()["phones"] == 1


def test_new_password_forgets_the_owners_phones(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    _sign_up(shop, TOKEN2)
    r = shop.post("/api/auth/password", {"current_password": "secret123", "new_password": "newsecret123"})
    assert r.status_code == 200, r.text
    shop.h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert shop.get("/api/notifications/settings").json()["phones"] == 0
    _sign_up(shop)  # the phone the password was changed on signs up again
    assert shop.get("/api/notifications/settings").json()["phones"] == 1


# ---- the worker

def test_worker_runs_the_summary_and_the_heartbeat(client, monkeypatch):
    import worker

    assert {"deliver_push", "check_push_receipts"} <= {f.__name__ for f in worker.WorkerSettings.functions}
    [cron] = worker.WorkerSettings.cron_jobs
    assert cron.name == "tick" and cron.unique and cron.second == 0
    pings, runs = [], []
    monkeypatch.setattr(push, "http", lambda: _expo({}, pings))
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    monkeypatch.delenv("PUSH_ENABLED", raising=False)

    async def summaries(db):
        runs.append(db)
        return 0
    monkeypatch.setattr(notify, "send_due_summaries", summaries)
    ctx = {"db": db_module.state.db}
    _run(worker.tick(ctx))  # no HEARTBEAT_URL, push off: nothing
    assert pings == [] and runs == []
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc-ping.example/abc")
    monkeypatch.setenv("PUSH_ENABLED", "1")
    _run(worker.tick(ctx))
    assert len(runs) == 1 and [str(p.url) for p in pings] == ["https://hc-ping.example/abc"]

    async def broken(db):
        raise RuntimeError("mongo is down")
    monkeypatch.setattr(notify, "send_due_summaries", broken)
    _run(worker.tick(ctx))
    assert len(pings) == 1  # a failed tick does not ping: the monitor alerts
