"""Products, stock, categories with GST, and the shared picture library."""
from conftest import Shop, photo_b64, run


def test_starter_library_and_suggestions(client):
    shop = Shop(client)
    s = shop.get("/api/products/suggest", params={"q": "money"}).json()
    assert s["own"] == [] and s["library"][0]["name"] == "Money Plant"
    assert shop.get("/api/products/suggest", params={"q": "गुलाब"}).json()["library"][0]["name"] == "Rose"
    lib_id = s["library"][0]["id"]
    p = shop.post("/api/products", {"name": "Money Plant", "library_id": lib_id, "selling_price": 250}).json()
    assert p["category"] == "Indoor Plants"  # picked from the library's default category
    s = shop.get("/api/products/suggest", params={"q": "money"}).json()
    assert [x["name"] for x in s["own"]] == ["Money Plant"]
    assert "Money Plant" not in [x["name"] for x in s["library"]]  # already yours; "Jade Plant" (money tree) may stay


def test_first_shop_photo_becomes_shared_picture(client):
    a = Shop(client, phone="9811122334")
    b = Shop(client, phone="9811155667")
    lib_id = a.get("/api/products/suggest", params={"q": "tulsi"}).json()["library"][0]["id"]
    photo = a.post("/api/upload-base64", {"image_base64": photo_b64()}).json()["path"]
    a.post("/api/products", {"name": "Tulsi", "library_id": lib_id, "photo_path": photo, "selling_price": 60})
    mine = a.get("/api/products/suggest", params={"q": "tulsi"}).json()
    assert mine["own"][0]["image_source"] == "own"
    # Shop B sees the shared picture only after an app admin has checked it.
    entry = b.get("/api/products/suggest", params={"q": "tulsi"}).json()["library"][0]
    assert entry["image_status"] == "none" and entry["image_path"] is None
    pb = b.post("/api/products", {"name": "Tulsi", "library_id": lib_id, "selling_price": 70}).json()
    assert pb["image_source"] is None
    admin = Shop(client, phone="9000000001")
    live = admin.post(f"/api/admin/library/{lib_id}/approve").json()
    assert live["image_status"] == "live" and live["image_path"]
    pb = b.get(f"/api/products/{pb['id']}").json()
    assert pb["image_source"] == "library" and pb["image_path"] == live["image_path"]
    assert client.get(f"/api/files/{live['image_path']}").status_code == 200


def test_new_product_starts_a_library_entry(client):
    a = Shop(client, phone="9811122334")
    b = Shop(client, phone="9811155667")
    a.product("Ceramic Pot 10 inch", 150, 4, category="Pots")
    assert b.get("/api/products/suggest", params={"q": "ceramic"}).json()["library"][0]["name"] == "Ceramic Pot 10 inch"


def test_zero_stock_products_stay_listed(client):
    shop = Shop(client)
    p = shop.product("Ficus Bonsai", 899, 0)
    listed = shop.get("/api/products").json()
    assert listed[0]["id"] == p["id"] and listed[0]["out_of_stock"] is True
    assert [x["id"] for x in shop.get("/api/products", params={"low": True}).json()] == [p["id"]]
    r = shop.post(f"/api/products/{p['id']}/stock", {"change": 6, "reason": "Supplier"})
    assert r.json()["stock"] == 6
    assert shop.post(f"/api/products/{p['id']}/stock", {"change": -10}).status_code == 400  # never below 0


def test_categories_gst_rules(client):
    shop = Shop(client)
    pots = shop.cat["Pots"]
    assert client.patch(f"/api/categories/{pots}", headers=shop.h, json={"gst_percent": 7}).status_code == 400
    assert client.patch(f"/api/categories/{pots}", headers=shop.h, json={"gst_percent": 12}).json()["gst_percent"] == 12
    assert client.patch(f"/api/categories/{pots}", headers=shop.h, json={"gst_percent": 40}).json()["gst_percent"] == 40
    shop.product("Clay Pot", 120, 3, category="Pots")
    assert client.delete(f"/api/categories/{pots}", headers=shop.h).status_code == 400  # still used
    cats = {c["name"]: c for c in shop.get("/api/categories").json()}
    assert cats["Pots"]["product_count"] == 1


def test_admin_library_needs_admin(client):
    shop = Shop(client)
    assert shop.get("/api/admin/library").status_code == 403
    admin = Shop(client, phone="9000000001", name="Admin")
    entries = admin.get("/api/admin/library", params={"status": "none"}).json()
    assert len(entries) == 10
    eid = entries[0]["id"]
    r = admin.post(f"/api/admin/library/{eid}/photo", {"image_base64": photo_b64()})
    assert r.json()["image_status"] == "live"
    assert client.delete(f"/api/admin/library/{eid}/photo", headers=admin.h).json()["image_status"] == "none"


def test_translit_and_phonetic_unit():
    from search import CONTAINS, EXACT, STARTS, match_level, phonetic, search_score, translit
    assert translit("गुलाब") == "gulaab" and translit("तुलसी") == "tulasii"
    assert phonetic("tulsi") == phonetic("tulasi") == phonetic("तुलसी")
    assert phonetic("gulab") == phonetic("gulaab") == phonetic("गुलाब")
    assert match_level("money   plant", "Money Plant") == EXACT
    assert match_level("mon", "Money Plant") == STARTS and match_level("plant", "Money Plant") == STARTS
    assert match_level("rose", "Primrose") == CONTAINS and match_level("xyz", "Rose") == 0
    assert search_score("money", ["Money Plant"]) > search_score("money", ["Jade Plant", "money tree"]) > 0


def _lib_id(shop, q):
    return shop.get("/api/products/suggest", params={"q": q}).json()["library"][0]["id"]


def test_cross_language_search(client):
    shop = Shop(client)
    shop.post("/api/products", {"name": "Rose", "library_id": _lib_id(shop, "rose"), "selling_price": 99})
    shop.post("/api/products", {"name": "Tulsi", "library_id": _lib_id(shop, "tulsi"), "selling_price": 49})
    shop.post("/api/products", {"name": "Money Plant", "library_id": _lib_id(shop, "money"), "selling_price": 199})
    names = lambda q: [p["name"] for p in shop.get("/api/products", params={"search": q}).json()]  # noqa: E731
    assert names("rose") == ["Rose"] and names("gulab") == ["Rose"] and names("गुलाब") == ["Rose"]
    assert names("तुलसी") == ["Tulsi"] and names("tulasi") == ["Tulsi"]
    assert names("money   plant") == ["Money Plant"] and names("pothos") == ["Money Plant"]  # an alias
    assert names("plant")[0] == "Money Plant"


def test_hindi_typed_product_links_existing_entry(client):
    shop = Shop(client)
    p = shop.post("/api/products", {"name": "गुलाब", "selling_price": 99}).json()
    rose = shop.get("/api/products/suggest", params={"q": "rose"}).json()
    assert p["library_id"] and rose["own"][0]["id"] == p["id"]  # found under its English name too
    assert all(e["name"] != "Rose" for e in rose["library"])  # the shared Rose entry is already this product


def test_name_enrichment_updates_terms_and_hides_non_nursery(client, claude):

    import db as db_module
    from routers import library
    a, b = Shop(client), Shop(client, phone="9811155667")
    p = a.post("/api/products", {"name": "Holy Basil Plant", "selling_price": 60}).json()
    db = db_module.state.db
    run(library.enrich(db, p["library_id"]))
    entry = run(db.library.find_one({"id": p["library_id"]}))
    assert entry["name_hi"] == "तुलसी" and "tulsi" in entry["aliases"] and entry["hidden"] is False
    assert b.get("/api/products/suggest", params={"q": "tulsi"}).json()["library"]  # found by its new names

    claude.names = {**claude.names, "is_nursery_item": False, "hindi_name": "", "hinglish": [], "aliases": []}
    q = a.post("/api/products", {"name": "Dell Laptop Stand", "selling_price": 900}).json()
    run(library.enrich(db, q["library_id"]))
    assert b.get("/api/products/suggest", params={"q": "dell laptop"}).json()["library"] == []  # hidden from other shops
    assert a.get("/api/products/suggest", params={"q": "dell laptop"}).json()["own"]
