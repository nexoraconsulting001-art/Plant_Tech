"""Upload order details (POST /api/ai/scan-order, owner 10 Nov 2026): a customer's order on paper read into lines,
each matched to the shop's products through every name (Claude's pick from the stock list first)."""
import ai_client
from conftest import Shop, photo_b64


def _pages(shop, n):
    return [shop.post("/api/upload-base64", {"image_base64": photo_b64(color=(i, 40, 80))}).json()["path"]
            for i in range(n)]


def _line(name, quantity=1, price=0, stock_match="", stock_alternatives=()):
    return {"name": name, "quantity": quantity, "price": price, "stock_match": stock_match,
            "stock_alternatives": list(stock_alternatives)}


def test_lines_are_matched_to_the_shops_products(client, claude):
    shop = Shop(client)
    tulsi = shop.product("Tulsi", 60, 10, "Herbs")
    pot6 = shop.product("Clay Pot 6 inch", 80, 20, "Pots")
    pot8 = shop.product("Clay Pot 8 inch", 120, 20, "Pots")
    marigold = shop.product("Marigold", 30, 40)
    claude.order = {"is_order": True, "reason": "a handwritten list", "items": [
        _line("Holy Basil", 2, 50),                                   # another name of Tulsi
        _line("Clay pot", 1, 0, stock_match="Clay Pot 6 inch", stock_alternatives=["Clay Pot 8 inch"]),
        _line("Genda", 12, 25, stock_match="Imaginary Genda"),        # an invented pick is ignored
        _line("Plastic tray", 3, 15),                                 # not in the shop
        _line("  ", 1, 5)]}                                           # an empty line is dropped
    r = shop.post("/api/ai/scan-order", {"image_paths": _pages(shop, 2)})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["Holy Basil", "Clay pot", "Genda", "Plastic tray"]
    assert items[0]["matched_product"]["id"] == tulsi["id"] and items[0]["quantity"] == 2 and items[0]["price"] == 50
    assert items[1]["matched_product"]["id"] == pot6["id"] and [p["id"] for p in items[1]["other_products"]] == [pot8["id"]]
    assert items[2]["matched_product"]["id"] == marigold["id"]
    assert items[3]["matched_product"] is None
    full = items[0]["matched_product"]  # full product views, as the order needs them
    assert {"selling_price", "stock", "unit", "gst_percent", "image_path"} <= full.keys()
    # the stock list went to Claude with the photos, after the fixed instructions
    _, body = claude[-1]
    assert body["system"][0]["text"] == ai_client.SYSTEM_ORDER and "- Tulsi (Ocimum tenuiflorum" in body["system"][1]["text"]
    assert [c["type"] for c in body["messages"][0]["content"]] == ["text", "image", "text", "image", "text"]
    assert r.json()["pages"] == 2


def test_not_an_order_pages_and_limits(client, claude):
    shop = Shop(client)
    claude.order = {"is_order": False, "reason": "a flower", "items": []}
    r = shop.post("/api/ai/scan-order", {"image_paths": _pages(shop, 1)})
    assert r.status_code == 422 and r.json()["code"] == "not_an_order"
    assert shop.post("/api/ai/scan-order", {"image_paths": ["someone/else.jpg"]}).status_code == 400
    r = shop.post("/api/ai/scan-order", {"image_paths": _pages(shop, 6)})
    assert r.status_code == 422 and r.json()["code"] == "too_many_pages"
    assert shop.post("/api/ai/scan-order", {"image_paths": []}).status_code == 422
    # one scan per page: 1 used above; the daily limit is 5 in tests
    claude.order = {"is_order": True, "reason": "a list", "items": [_line("Rose")]}
    assert shop.post("/api/ai/scan-order", {"image_paths": _pages(shop, 4)}).status_code == 200
    assert shop.post("/api/ai/scan-order", {"image_paths": _pages(shop, 1)}).status_code == 429


def test_quantity_and_price_are_never_below_zero(client, claude):
    shop = Shop(client)
    claude.order = {"is_order": True, "reason": "a list", "items": [_line("Rose", 0, -5), _line("Mint", -2, 30)]}
    items = shop.post("/api/ai/scan-order", {"image_paths": _pages(shop, 1)}).json()["items"]
    assert [(i["quantity"], i["price"]) for i in items] == [(1, 0), (1, 30)]


def test_an_employee_who_sells_may_read_an_order(client, claude):
    shop = Shop(client)
    _, seller = shop.worker(stock=False)
    _, no_sell = shop.worker(phone="9822233446", sell=False)
    paths = _pages(shop, 1)  # the shop's upload: anyone in the shop may use it
    own = client.post("/api/upload-base64", headers=seller, json={"image_base64": photo_b64()}).json()["path"]
    assert client.post("/api/ai/scan-order", headers=seller, json={"image_paths": [own]}).status_code == 200
    assert client.post("/api/ai/scan-order", headers=seller, json={"image_paths": paths}).status_code == 200
    assert client.post("/api/ai/scan-order", headers=no_sell, json={"image_paths": paths}).status_code == 403
    other = Shop(client, phone="9811155667", name="Other")
    assert other.post("/api/ai/scan-order", {"image_paths": paths}).status_code == 400  # another shop's page
