"""Several UPI IDs per shop (one default), the UPI ID kept on UPI bills, and the WhatsApp-the-bill setting."""

import db as db_module
from conftest import Shop, run


def _order(shop, product, headers=None, **extra):
    body = {"items": [{"product_id": product["id"], "name": product["name"], "quantity": 1, "price": product["selling_price"]}],
            "customer_phone": "9811100099", "customer_name": "Test Customer", **extra}
    return shop.c.post("/api/invoices", headers=headers or shop.h, json=body)


def _two_upis(shop):
    r = shop.c.put("/api/business", headers=shop.h, json={
        "upi_ids": [{"id": "a", "upi_id": "shop@upi", "label": "Shop"}, {"id": "b", "upi_id": "owner@okaxis", "label": "Owner GPay"}],
        "default_upi": "b"})
    assert r.status_code == 200, r.text
    return r.json()


def test_old_single_upi_becomes_the_default_entry(client):
    shop = Shop(client)  # sets upi_id the old way
    biz = shop.get("/api/business").json()
    assert [e["upi_id"] for e in biz["upi_ids"]] == ["shop@upi"]
    assert biz["default_upi"] == biz["upi_ids"][0]["id"] and biz["upi_id"] == "shop@upi"
    assert biz["whatsapp_bill"] is True
    assert shop.get("/api/business").json()["default_upi"] == biz["default_upi"]  # same id on the next read


def test_shop_saved_before_the_list_is_migrated_on_read(client):
    shop = Shop(client)
    run(db_module.state.db.businesses.update_one(
        {}, {"$unset": {"upi_ids": "", "default_upi": "", "whatsapp_bill": ""}, "$set": {"upi_id": "old@ybl"}}))
    biz = shop.get("/api/business").json()
    assert [e["upi_id"] for e in biz["upi_ids"]] == ["old@ybl"] and biz["upi_id"] == "old@ybl"
    assert biz["whatsapp_bill"] is True


def test_owner_keeps_a_list_with_a_default(client):
    shop = Shop(client)
    biz = _two_upis(shop)
    assert biz["default_upi"] == "b" and biz["upi_id"] == "owner@okaxis"  # old apps see the default
    assert [e["label"] for e in biz["upi_ids"]] == ["Shop", "Owner GPay"]
    # the default removed: the first one takes over
    biz = shop.c.put("/api/business", headers=shop.h, json={"upi_ids": [{"id": "a", "upi_id": "shop@upi"}], "default_upi": "b"}).json()
    assert biz["default_upi"] == "a" and biz["upi_id"] == "shop@upi"
    # just the default changed
    _two_upis(shop)
    biz = shop.c.put("/api/business", headers=shop.h, json={"default_upi": "a"}).json()
    assert biz["upi_id"] == "shop@upi" and len(biz["upi_ids"]) == 2
    # all removed
    biz = shop.c.put("/api/business", headers=shop.h, json={"upi_ids": []}).json()
    assert biz["upi_ids"] == [] and biz["upi_id"] == "" and biz["default_upi"] is None


def test_upi_ids_are_checked(client):
    shop = Shop(client)
    r = shop.c.put("/api/business", headers=shop.h, json={"upi_ids": [{"upi_id": "not a upi"}]})
    assert r.status_code == 400 and r.json()["code"] == "bad_upi" and r.json()["params"] == {"upi": "not a upi"}
    r = shop.c.put("/api/business", headers=shop.h, json={"upi_ids": [{"upi_id": "green@upi"}, {"upi_id": "Green@UPI"}]})
    assert r.status_code == 400 and r.json()["code"] == "duplicate_upi"
    r = shop.c.put("/api/business", headers=shop.h, json={"upi_id": "9811122334"})  # old app, no @bank
    assert r.status_code == 400 and r.json()["code"] == "bad_upi"
    assert shop.get("/api/business").json()["upi_id"] == "shop@upi"  # nothing changed


def test_a_upi_id_saved_before_the_check_does_not_block_other_edits(client):
    shop = Shop(client)
    run(db_module.state.db.businesses.update_one({}, {"$set": {"upi_ids": [{"id": "x", "upi_id": "shop upi", "label": ""}],
                                                                       "default_upi": "x", "upi_id": "shop upi"}}))
    r = shop.c.put("/api/business", headers=shop.h, json={"upi_id": "shop upi", "name": "New name"})  # 1.1.0 sends it back
    assert r.status_code == 200 and r.json()["name"] == "New name"
    r = shop.c.put("/api/business", headers=shop.h, json={"upi_ids": [{"id": "x", "upi_id": "shop upi"}, {"upi_id": "bad one"}]})
    assert r.status_code == 400 and r.json()["params"] == {"upi": "bad one"}


def test_old_app_upi_id_changes_only_the_default(client):
    shop = Shop(client)
    _two_upis(shop)
    biz = shop.c.put("/api/business", headers=shop.h, json={"upi_id": "new@ybl", "name": "Renamed"}).json()
    assert [e["upi_id"] for e in biz["upi_ids"]] == ["shop@upi", "new@ybl"] and biz["upi_id"] == "new@ybl"
    biz = shop.c.put("/api/business", headers=shop.h, json={"upi_id": "shop@upi"}).json()  # one already in the list
    assert len(biz["upi_ids"]) == 2 and biz["default_upi"] == "a"
    biz = shop.c.put("/api/business", headers=shop.h, json={"upi_id": ""}).json()  # cleared: the default goes
    assert [e["upi_id"] for e in biz["upi_ids"]] == ["new@ybl"] and biz["upi_id"] == "new@ybl"


def test_whatsapp_setting_and_who_can_read_or_change_it(client):
    shop = Shop(client)
    assert shop.c.put("/api/business", headers=shop.h, json={"whatsapp_bill": False}).json()["whatsapp_bill"] is False
    _, worker = shop.worker()
    biz = client.get("/api/business", headers=worker)
    assert biz.status_code == 200 and biz.json()["whatsapp_bill"] is False and biz.json()["upi_ids"]
    assert client.put("/api/business", headers=worker, json={"whatsapp_bill": True}).status_code == 403
    assert client.put("/api/business", headers=worker, json={"upi_ids": []}).status_code == 403


def test_upi_bill_keeps_the_upi_id_it_was_paid_to(client):
    shop = Shop(client)
    _two_upis(shop)
    plant = shop.product("Money Plant", 250, 10)
    inv = _order(shop, plant, payment_method="UPI", upi_id="SHOP@upi").json()
    assert inv["upi_id"] == "shop@upi"  # the one chosen, as the shop wrote it
    assert "Paid to UPI ID shop@upi" in client.get(f"/api/invoices/{inv['id']}/html", headers=shop.h).text
    # not one of the shop's, or an old app that sends none: the default
    assert _order(shop, plant, payment_method="UPI", upi_id="someone@else").json()["upi_id"] == "owner@okaxis"
    _, worker = shop.worker()
    assert _order(shop, plant, headers=worker, payment_method="UPI").json()["upi_id"] == "owner@okaxis"
    # cash bills, and shops without a UPI ID, keep none
    assert "upi_id" not in _order(shop, plant, payment_method="Cash", upi_id="shop@upi").json()
    shop.c.put("/api/business", headers=shop.h, json={"upi_ids": []})
    assert "upi_id" not in _order(shop, plant, payment_method="UPI").json()
