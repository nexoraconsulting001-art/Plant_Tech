"""Units: pieces are whole numbers, kg / litre / metre may be 1.5, plants are always pieces."""
import invoice_html
from conftest import Shop, photo_b64, run


def _order(shop, items, **extra):
    extra.setdefault("customer_phone", "9811100099")
    extra.setdefault("customer_name", "Test Customer")
    return shop.post("/api/invoices", {"items": items, **extra})


def test_products_have_a_unit_and_plants_are_pieces(client):
    shop = Shop(client)
    soil = shop.product("Red Soil", 40, 50, category="Pots", unit="kg")
    assert soil["unit"] == "kg"
    assert shop.product("Clay Pot", 100, 5, category="Pots")["unit"] == "pcs"  # old apps send no unit
    assert shop.product("Money Plant", 250, 5, unit="kg")["unit"] == "pcs"  # plant category
    assert shop.post("/api/products", {"name": "X", "unit": "dozen"}).status_code == 422

    r = client.put(f"/api/products/{soil['id']}", headers=shop.h, json={"unit": "litre"})
    assert r.json()["unit"] == "litre"
    r = client.put(f"/api/products/{soil['id']}", headers=shop.h, json={"category_id": shop.cat["Indoor Plants"]})
    assert r.json()["unit"] == "pcs"  # moved into a plant category
    assert shop.get(f"/api/products/{soil['id']}").json()["unit"] == "pcs"


def test_old_products_count_as_pieces(client):
    import db as db_module

    shop = Shop(client)
    pot = shop.product("Clay Pot", 100, 5, category="Pots")
    run(db_module.state.db.products.update_one({"id": pot["id"]}, {"$unset": {"unit": ""}}))
    assert shop.get(f"/api/products/{pot['id']}").json()["unit"] == "pcs"
    r = _order(shop, [{"product_id": pot["id"], "name": "Clay Pot", "quantity": 1.5, "price": 100}])
    assert r.status_code == 400 and r.json()["code"] == "whole_quantity"


def test_bill_takes_decimals_only_for_weight_volume_length(client):
    shop = Shop(client, gst=False)
    soil = shop.product("Red Soil", 40, 10, category="Pots", unit="kg")
    pot = shop.product("Clay Pot", 100, 5, category="Pots")
    r = _order(shop, [{"product_id": pot["id"], "name": "Clay Pot", "quantity": 1.5, "price": 100}])
    assert r.status_code == 400
    assert r.json() == {"detail": "Clay Pot is sold in whole numbers. Please change the quantity.", "code": "whole_quantity",
                        "key": "{{name}} is sold in whole numbers. Please change the quantity.", "params": {"name": "Clay Pot"}}
    assert shop.get(f"/api/products/{pot['id']}").json()["stock"] == 5  # nothing taken

    # the product's own unit counts, not what the app says
    r = _order(shop, [{"product_id": pot["id"], "name": "Clay Pot", "quantity": 1.5, "price": 100, "unit": "kg"}])
    assert r.status_code == 400

    inv = _order(shop, [{"product_id": soil["id"], "name": "Red Soil", "quantity": 2.5, "price": 40},
                        {"product_id": pot["id"], "name": "Clay Pot", "quantity": 2, "price": 100},
                        {"product_id": None, "name": "Twine", "quantity": 1.25, "price": 20, "unit": "metre"},
                        {"product_id": None, "name": "Hibiscus", "quantity": 1, "price": 280}]).json()
    assert [(l["quantity"], l["unit"]) for l in inv["items"]] == [(2.5, "kg"), (2, "pcs"), (1.25, "metre"), (1, "pcs")]
    assert inv["grand_total"] == 100 + 200 + 25 + 280
    assert shop.get(f"/api/products/{soil['id']}").json()["stock"] == 7.5
    # "5 items": 2 pots, the hibiscus, and one line each for the soil and the twine
    assert shop.get("/api/invoices").json()[0]["items_count"] == 5

    # a loose line without a unit is in pieces
    r = _order(shop, [{"product_id": None, "name": "Rose", "quantity": 0.5, "price": 100}])
    assert r.status_code == 400 and r.json()["code"] == "whole_quantity"

    page = invoice_html.render(inv, {"name": "Shop"})
    assert ">2.5 kg<" in page and ">2 pcs<" in page and ">1.25 m<" in page


def test_saved_order_refuses_half_a_piece(client):
    shop = Shop(client)
    kavita = shop.post("/api/customers", {"name": "Kavita", "phone": "9811122334"}).json()
    put = lambda items: client.put("/api/orders/cart", headers=shop.h,  # noqa: E731
                                   json={"customer_id": kavita["id"], "items": items})
    line = {"product_id": "p1", "name": "Clay Pot", "quantity": 1.5, "price": 100}
    r = put([line])
    assert r.status_code == 400 and r.json()["code"] == "whole_quantity"
    r = put([{**line, "unit": "kg"}])
    assert r.status_code == 200 and r.json()["items"][0]["unit"] == "kg"
    r = put([{**line, "quantity": 2}])  # what older apps send
    assert r.status_code == 200 and r.json()["items"][0]["unit"] == "pcs"
    assert put([{**line, "unit": "kg", "quantity": 100001}]).status_code == 422


def test_bill_products_and_identified_plants(client, claude):
    shop = Shop(client)
    r = shop.post("/api/purchases", {"supplier": "Green Farms", "items": [
        {"name": "Cocopeat", "quantity": 4, "purchase_price": 50}]})
    assert r.status_code == 200
    made = [p for p in shop.get("/api/products").json() if p["name"] == "Cocopeat"][0]
    assert made["unit"] == "pcs"
    assert shop.post("/api/ai/identify-plant", {"image_base64": photo_b64()}).json()["kind"] == "plant"


def test_decimal_stock_stays_on_three_decimals(client):
    """0.7 - 0.4 is 0.29999999999999993 in floating point: the last 0.3 kg must still sell, and leave exactly 0."""
    import db as db_module

    shop = Shop(client, gst=False)
    _, worker = shop.worker()
    soil = shop.product("Red Soil", 40, 0.7, category="Pots", unit="kg")
    line = lambda q: {"product_id": soil["id"], "name": "Red Soil", "quantity": q, "price": 40}  # noqa: E731
    sell = lambda q: client.post("/api/invoices", headers=worker, json={  # noqa: E731
        "items": [line(q)], "customer_phone": "9811100099", "customer_name": "Test"})
    assert sell(0.4).status_code == 200
    assert shop.get(f"/api/products/{soil['id']}").json()["stock"] == 0.3
    r = sell(0.3)
    assert r.status_code == 200, r.text
    assert "short_qty" not in r.json()["items"][0]
    p = shop.get(f"/api/products/{soil['id']}").json()
    assert p["stock"] == 0 and p["history"][0]["new_stock"] == 0
    assert shop.get("/api/dashboard/today").json()["below_zero"] == []

    # by hand and from a supplier bill too
    assert shop.post(f"/api/products/{soil['id']}/stock", {"change": 0.7}).json()["stock"] == 0.7
    assert shop.post(f"/api/products/{soil['id']}/stock", {"change": -0.4}).json()["stock"] == 0.3
    assert shop.post("/api/purchases", {"supplier": "Green Farms", "items": [
        {"product_id": soil["id"], "name": "Red Soil", "quantity": 0.1, "purchase_price": 20}]}).status_code == 200
    assert shop.get(f"/api/products/{soil['id']}").json()["stock"] == 0.4

    # stock saved unrounded before this fix still sells in full
    run(db_module.state.db.products.update_one({"id": soil["id"]}, {"$set": {"stock": 0.7 - 0.4}}))
    assert sell(0.3).status_code == 200
    assert shop.get(f"/api/products/{soil['id']}").json()["stock"] == 0
    assert sell(0.001).json()["code"] == "out_of_stock"


def test_saved_order_takes_the_products_unit(client):
    """Older apps send no unit, and a product's unit may change while its order waits: the product's unit counts."""
    shop = Shop(client)
    soil = shop.product("Potting Soil", 40, 50, category="Pots", unit="kg")
    pot = shop.product("Clay Pot", 100, 5, category="Pots")
    kavita = shop.post("/api/customers", {"name": "Kavita", "phone": "9811122334"}).json()
    put = lambda items: client.put("/api/orders/cart", headers=shop.h,  # noqa: E731
                                   json={"customer_id": kavita["id"], "items": items})
    get = lambda: shop.get("/api/orders/cart", params={"customer_id": kavita["id"]}).json()  # noqa: E731
    line = lambda p, q, **kw: {"product_id": p["id"], "name": p["name"], "quantity": q, "price": p["selling_price"], **kw}  # noqa: E731

    r = put([line(soil, 2), line(pot, 1, unit="kg"), line({"id": "loose-1", "name": "Twine", "selling_price": 5}, 1.5, unit="metre")])
    assert r.status_code == 200, r.text
    assert [i["unit"] for i in r.json()["items"]] == ["kg", "pcs", "metre"]  # the app's word does not count for products
    assert put([line(soil, 1.5)]).status_code == 200
    assert put([line(pot, 1.5, unit="kg")]).json()["code"] == "whole_quantity"

    # saved before units existed (no unit), then the owner switches the product to litre
    import db as db_module
    run(db_module.state.db.carts.update_many({}, {"$set": {"items": [line(soil, 2)]}}))
    assert get()["items"][0]["unit"] == "kg"
    client.put(f"/api/products/{soil['id']}", headers=shop.h, json={"unit": "litre"})
    assert get()["items"][0]["unit"] == "litre"
