"""Deleting an account: confirm, grace period (a login cancels it), then archive the GST records and erase."""
from datetime import datetime, timedelta, timezone

import auth
import storage_client
from conftest import Shop, photo_b64, run
from db import get_db
from routers import account, files
from test_auth import PIN, add_worker


def _delete(client, headers, **body):
    return client.request("DELETE", "/api/account", headers=headers, json=body)


def _sell(shop, product):
    r = shop.post("/api/invoices", {"items": [{"product_id": product["id"], "name": product["name"], "quantity": 1,
                                               "price": product["selling_price"]}],
                                    "customer_phone": "9811100099", "customer_name": "Anita"})
    assert r.status_code == 200, r.text
    return r.json()


def test_owner_deletion_waits_and_a_login_cancels_it(client):
    shop = Shop(client)
    _, wh = add_worker(shop)
    assert _delete(client, shop.h).status_code == 401  # needs the password
    assert _delete(client, shop.h, password="wrong-one").status_code == 401
    r = _delete(client, shop.h, password="secret123", reason="closing the nursery")
    assert r.status_code == 200 and r.json()["status"] == "scheduled" and r.json()["grace_days"] == account.GRACE_DAYS
    erase_after = datetime.fromisoformat(r.json()["erase_after"])
    assert timedelta(days=29) < erase_after - datetime.now(timezone.utc) <= timedelta(days=30)
    # everyone is logged out, employees cannot come back
    assert shop.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=wh).status_code == 401
    r = client.post("/api/auth/login", json={"phone": "9822233445", "password": PIN})
    assert r.status_code == 403 and r.json()["detail"] == "This shop is being deleted. Please contact the shop owner."
    # the owner logging in keeps the shop
    r = client.post("/api/auth/login", json={"phone": "9811122334", "password": "secret123"})
    assert r.status_code == 200 and r.json()["deletion_cancelled"] is True
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": PIN}).status_code == 200
    again = client.post("/api/auth/login", json={"phone": "9811122334", "password": "secret123"}).json()
    assert "deletion_cancelled" not in again  # the usual answer, as 1.1.0 knows it
    actions = [e["action"] for e in run(get_db().audit_log.find({"action": {"$regex": "^account"}}).to_list(10))]
    assert actions == ["account.delete_requested", "account.delete_cancelled"]


def test_after_the_grace_period_gst_records_are_archived_and_the_rest_erased(client):
    shop = Shop(client)
    other = Shop(client, phone="9811155667", name="Bina")
    p = shop.product("Money Plant", 250, 10)
    bill = _sell(shop, p)
    _sell(other, other.product("Rose", 80, 5))
    photo = shop.post("/api/upload-base64", {"image_base64": photo_b64()}).json()["path"]
    assert storage_client._local_path(photo).exists()
    logo = shop.post("/api/upload-base64", {"image_base64": photo_b64(), "kind": "logo"}).json()["path"]
    public = [storage_client._local_path(k, storage_client.PUBLIC) for k in (logo, files.thumb_key(logo))]
    assert all(f.exists() for f in public)  # WP-7: a logo and its thumbnail go to the public store
    bid = shop.get("/api/auth/me").json()["business"]["id"]
    db = get_db()
    run(db.summary_schedule.update_one({"_id": bid}, {"$set": {"due": datetime.now(timezone.utc), "day": "x"}},
                                       upsert=True))  # WP-5's row, keyed by the shop id
    assert _delete(client, shop.h, password="secret123").status_code == 200
    assert run(account.erase_due(db)) == []  # still in the grace period
    later = datetime.now(timezone.utc) + timedelta(days=account.GRACE_DAYS, minutes=1)
    assert run(account.erase_due(db, later)) == [bid]
    for coll in ("users", "products", "customers", "invoices", "categories", "stock_history", "uploads"):
        assert run(db[coll].count_documents({"business_id": bid})) == 0, coll
    assert run(db.businesses.find_one({"id": bid})) is None
    assert not storage_client._local_path(photo).exists()
    assert not any(f.exists() for f in public)
    assert run(db.summary_schedule.find_one({"_id": bid})) is None
    assert run(db.daily_sales.count_documents({"business_id": bid})) == 0  # WP-2's rollups
    archived = run(db.gst_archive.find({"business_id": bid}, {"_id": 0}).to_list(100))
    kinds = sorted({a["kind"] for a in archived})
    assert kinds == ["business", "invoices", "stock_history"]
    inv = next(a for a in archived if a["kind"] == "invoices")["record"]
    assert inv["invoice_number"] == bill["invoice_number"] and inv["grand_total"] == bill["grand_total"]
    identity = next(a for a in archived if a["kind"] == "business")["record"]
    assert identity["gstin"] and "upi_id" not in identity
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # MongoDB hands back naive UTC
    assert all(a["purge_after"] > now + timedelta(days=365 * 8) for a in archived)
    # the other shop is untouched, the old login is gone, the number can sign up again
    assert len(other.get("/api/invoices").json()) == 1
    assert client.post("/api/auth/login", json={"phone": "9811122334", "password": "secret123"}).status_code == 401
    assert run(account.erase_due(db, later)) == []  # nothing left to do
    erased = run(db.audit_log.find_one({"action": "account.erased"}, {"_id": 0}))
    assert erased["business_id"] == bid and erased["details"]["deleted"]["invoices"] == 1
    assert client.post("/api/auth/register", json={"name": "S", "phone": "9811122334",
                                                   "password": "secret123"}).status_code == 200


def test_a_cancelled_deletion_is_never_erased(client):
    shop = Shop(client)
    db = get_db()
    _delete(client, shop.h, password="secret123")
    client.post("/api/auth/login", json={"phone": "9811122334", "password": "secret123"})
    later = datetime.now(timezone.utc) + timedelta(days=60)
    assert run(account.erase_due(db, later)) == []
    assert run(db.users.count_documents({"phone": "+919811122334"})) == 1


def test_an_employee_deletes_only_their_own_login(client):
    shop = Shop(client)
    wid, wh = add_worker(shop)
    assert _delete(client, wh, password="nope00").status_code == 401
    r = _delete(client, wh, password=PIN)
    assert r.status_code == 200 and r.json() == {"status": "erased"}
    assert client.get("/api/auth/me", headers=wh).status_code == 401
    assert shop.get("/api/auth/me").status_code == 200  # the shop goes on
    assert shop.get("/api/staff").json() == []
    assert run(get_db().users.find_one({"id": wid}))["name"] == account.FORMER_EMPLOYEE


def test_google_only_owner_confirms_with_google(client, monkeypatch):
    monkeypatch.setattr(auth, "verify_google_id_token",
                        lambda t: {"sub": "g-123" if t == "mine" else "g-999", "name": "Asha"})
    h = {"Authorization": f"Bearer {client.post('/api/auth/google', json={'id_token': 'mine'}).json()['token']}"}
    assert _delete(client, h).status_code == 400
    assert _delete(client, h, id_token="someone-else").status_code == 401
    assert _delete(client, h, id_token="mine").json()["status"] == "scheduled"
    r = client.post("/api/auth/google", json={"id_token": "mine"})
    assert r.status_code == 200 and r.json()["deletion_cancelled"] is True
