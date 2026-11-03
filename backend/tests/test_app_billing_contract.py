"""What the 1.2.x app's billing path (WP-12) relies on from the server, so a server change can't quietly break it:
- the Pay answer is the bill itself: the app shows the bill screen from it instead of loading it again
  (frontend/src/order.ts afterBill), so POST /invoices must answer what GET /invoices/{id} does, less `shop`;
- a cancel answers with the cancelled bill (the bill screen keeps it from the answer);
- the bill's WhatsApp link never changes (the app asks for it once per bill);
- "Recent customers" (the empty search) lists the customer just billed first (the app moves them up on the phone);
- a customer with no saved order is an answer (null), not an error; a save for a customer that isn't there is
  refused with a 4xx, which the app does not retry (frontend/src/sync-policy.ts);
- an Idempotency-Key header on the bill is accepted (old apps send none)."""
import uuid

from conftest import Shop


def _customer(shop, name="Kavita", phone="+919811100011"):
    r = shop.post("/api/customers", {"name": name, "phone": phone})
    assert r.status_code == 200, r.text
    return r.json()


def _bill(shop, product, customer, key=None, **extra):
    headers = {**shop.h, **({"Idempotency-Key": key} if key else {})}
    return shop.c.post("/api/invoices", headers=headers, json={
        "items": [{"product_id": product["id"], "name": product["name"], "quantity": 1, "price": product["selling_price"]}],
        "customer_id": customer["id"], "payment_method": "Cash", **extra})


def test_pay_answer_is_the_bill(client):
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    kavita = _customer(shop)
    made = _bill(shop, plant, kavita, key=str(uuid.uuid4()))
    assert made.status_code == 200, made.text
    inv = made.json()
    got = shop.get(f"/api/invoices/{inv['id']}").json()
    assert set(got["shop"]) == {"name", "address", "phone"}  # the app fills these from Shop details
    assert {k: v for k, v in got.items() if k != "shop"} == inv


def test_cancel_answers_with_the_cancelled_bill(client):
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    inv = _bill(shop, plant, _customer(shop)).json()
    r = shop.post(f"/api/invoices/{inv['id']}/cancel", {"reason": "Entered by mistake"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == inv["id"] and r.json()["status"] == "Cancelled"


def test_share_link_is_stable(client):
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    inv = _bill(shop, plant, _customer(shop)).json()
    first = shop.get(f"/api/invoices/{inv['id']}/share").json()
    assert shop.get(f"/api/invoices/{inv['id']}/share").json() == first


def test_recent_customers_put_the_last_billed_first(client):
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    kavita, rahul = _customer(shop), _customer(shop, "Rahul", "+919811100022")
    _bill(shop, plant, kavita)
    _bill(shop, plant, rahul)
    _bill(shop, plant, kavita)
    recent = shop.get("/api/customers/find", params={"q": ""}).json()
    assert [c["id"] for c in recent[:2]] == [kavita["id"], rahul["id"]]
    assert recent[0]["purchase_count"] == 2


def test_saved_order_none_and_refused(client):
    shop = Shop(client)
    kavita = _customer(shop)
    r = shop.get("/api/orders/cart", params={"customer_id": kavita["id"]})
    assert r.status_code == 200 and r.json() is None  # "not found" is an answer
    r = client.put("/api/orders/cart", headers=shop.h, json={"customer_id": "no-such-customer", "items": [
        {"product_id": "loose-1", "name": "Pot", "quantity": 1, "price": 50}]})
    assert 400 <= r.status_code < 500 and r.status_code not in (408, 425, 429)  # the app drops it instead of retrying


def test_bill_clears_the_saved_order(client):
    """The app doesn't send the last waiting save before Pay: the bill carries the order and clears the saved one."""
    shop = Shop(client)
    plant = shop.product("Money Plant", 250, 10)
    kavita = _customer(shop)
    r = client.put("/api/orders/cart", headers=shop.h, json={"customer_id": kavita["id"], "items": [
        {"product_id": plant["id"], "name": "Money Plant", "quantity": 1, "price": 250}]})
    assert r.status_code == 200, r.text
    assert _bill(shop, plant, kavita, key=str(uuid.uuid4())).status_code == 200
    assert shop.get("/api/orders/cart", params={"customer_id": kavita["id"]}).json() is None
