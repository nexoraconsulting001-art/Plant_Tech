"""Owner notifications: phone sign-up, low-stock alerts, the daily summary, and the Expo sender. Nothing is sent
for real: `push.dispatch` is replaced to collect the messages, and `push.send` gets a fake Expo."""
import asyncio
from datetime import datetime

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

import db as db_module
import notify
import push
from common import IST, date_key
from conftest import Shop

TOKEN = "ExponentPushToken[abcdefgh12345678]"
TOKEN2 = "ExponentPushToken[zyxwvuts87654321]"


@pytest.fixture
def sent(monkeypatch):
    """Every message the server would send, in order."""
    out = []
    monkeypatch.setattr(push, "dispatch", lambda _db, messages: out.extend(messages))
    return out


def _order(shop, items, headers=None, **extra):
    return shop.c.post("/api/invoices", headers=headers or shop.h,
                       json={"items": items, "customer_phone": "9811100099", "customer_name": "Test", **extra})


def _sign_up(shop, token=TOKEN, lang="en", headers=None):
    r = shop.c.post("/api/push-tokens", headers=headers or shop.h, json={"token": token, "lang": lang, "platform": "android"})
    assert r.status_code == 200, r.text


def _run(coro):
    return asyncio.run(coro)


def test_only_owners_sign_up_a_phone(client):
    shop = Shop(client)
    _, worker = shop.worker()
    assert client.post("/api/push-tokens", headers=worker, json={"token": TOKEN}).status_code == 403
    assert shop.post("/api/push-tokens", {"token": "not-a-token"}).status_code == 422
    _sign_up(shop)
    _sign_up(shop)  # the same phone again: still one row
    assert shop.get("/api/notifications/settings").json()["phones"] == 1
    # an employee logging in on that phone stops the owner's notifications there
    assert client.post("/api/push-tokens/remove", headers=worker, json={"token": TOKEN}).status_code == 200
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


def test_stock_removed_by_hand_also_alerts(client, sent):
    shop = Shop(client)
    _sign_up(shop)
    pot = shop.product("Clay Pot", 120, 6, category="Pots", min_stock=3)
    r = shop.post(f"/api/products/{pot['id']}/stock", {"change": -3, "reason": "Broken"})  # mongomock wants stock left >= removed
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
    early, late = today.replace(hour=20, minute=59), today.replace(hour=21, minute=0)

    assert _run(notify.send_due_summaries(db, late)) == 0  # no phone signed up
    _sign_up(shop)
    assert _run(notify.send_due_summaries(db, early)) == 0  # not 9 pm yet
    assert _run(notify.send_due_summaries(db, late)) == 1
    assert sent[-1]["title"] == "Today's sales"
    assert sent[-1]["body"] == "₹600 from 3 bills (Cash ₹200 / UPI ₹400). 1 product is low on stock."
    assert sent[-1]["data"] == {"type": "summary", "date": date_key(today)}
    assert _run(notify.send_due_summaries(db, late)) == 0  # once a day, however often it is checked
    assert len(sent) == 1


def test_summary_skips_a_day_without_bills_and_can_be_off(client, sent):
    shop = Shop(client)
    _sign_up(shop, lang="hi")
    db = db_module.state.db
    late = datetime.now(IST).replace(hour=22, minute=0)
    assert _run(notify.send_due_summaries(db, late)) == 0 and sent == []
    other = Shop(client, phone="9822200011", name="Mohan")
    _sign_up(other, TOKEN2)
    other.c.put("/api/notifications/settings", headers=other.h, json={"summary": False})
    rose = other.product("Rose", 100, 50)
    _order(other, [{"product_id": rose["id"], "name": "Rose", "quantity": 1, "price": 100}])
    assert _run(notify.send_due_summaries(db, late)) == 0 and sent == []


def test_summary_text_in_hindi():
    s = {"sales": 125000, "bills": 1, "by_method": {"Cash": 125000, "UPI": 0, "Card": 0}, "low_stock": 0}
    assert notify.summary_message(s, "en") == ("Today's sales", "₹1,25,000 from 1 bill (Cash ₹1,25,000).")
    assert notify.summary_message(s, "hi") == ("आज की बिक्री", "1 बिल से ₹1,25,000 (नकद ₹1,25,000)।")


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


# ---- the Expo sender

def _expo(tickets, seen):
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": tickets})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_send_posts_to_expo(monkeypatch):
    monkeypatch.setenv("EXPO_ACCESS_TOKEN", "expo-key")
    seen = []
    msgs = [{"to": TOKEN, "title": "a", "body": "b"}]
    tickets = _run(push.send(msgs, _expo([{"status": "ok", "id": "1"}], seen)))
    assert tickets == [{"status": "ok", "id": "1"}]
    assert str(seen[0].url) == push.PUSH_URL and seen[0].headers["authorization"] == "Bearer expo-key"


def test_deliver_drops_phones_without_the_app(monkeypatch):
    db = AsyncMongoMockClient()["push"]

    async def fake_send(messages, client=None):
        return [{"status": "ok"}, {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}]

    monkeypatch.setattr(push, "send", fake_send)

    async def go():
        await db.push_tokens.insert_many([{"token": TOKEN}, {"token": TOKEN2}])
        await push.deliver(db, [{"to": TOKEN}, {"to": TOKEN2}])
        return [t["token"] for t in await db.push_tokens.find({}).to_list(10)]
    assert _run(go()) == [TOKEN]


def test_deliver_never_raises(monkeypatch):
    async def down(messages, client=None):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(push, "send", down)
    _run(push.deliver(AsyncMongoMockClient()["push"], [{"to": TOKEN}]))  # logged, not raised
