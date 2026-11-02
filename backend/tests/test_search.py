"""Catalogue search through indexed search keys, the cached library, private shop entries, product codes and
the paged product list (WP-3)."""
import os
import statistics
import time

import pytest
from bson.regex import Regex
from pymongo.errors import DuplicateKeyError

import catalog_search
import db as db_module
import jobs
import migrations
from common import name_key, new_id, now_iso
from conftest import MOCK, Shop, run
from routers import library
from search import END, STARTS, keys_of, level_of, match_level, phonetic, product_keys, search_score

pytestmark = pytest.mark.skipif(MOCK, reason="needs the real MongoDB (indexes, explain, pipeline updates)")


def _db():
    return db_module.state.db


def _lib_id(shop, q):
    return shop.get("/api/products/suggest", params={"q": q}).json()["library"][0]["id"]


# ---- keys ----------------------------------------------------------------------------------------

def test_keys_cover_names_word_tails_and_sound_alikes():
    keys = keys_of(["Money Plant", "Epipremnum aureum", "NP-0001"])
    assert "k:money plant" + END in keys and "k:plant" in keys  # whole own name, and from a word on
    assert "p:" + phonetic("money plant") + END in keys and "p:" + phonetic("plant") in keys
    assert "s:epipremnum aureum" + END in keys and "s:aureum" in keys and "s:np 0001" + END in keys
    assert not any(k.startswith("k:plant" + END) for k in keys)  # "plant" alone is not this product's name
    rose = {"name": "Rose", "name_hi": "गुलाब", "aliases": ["gulab"]}
    own, both = product_keys({"name": "Rose", "sku": "NP-0002"}, rose)
    assert "s:गुलाब" + END in both and "t:" + phonetic("gulab") + END in both and "s:गुलाब" + END not in own
    gone = {"name": "Rose", "name_hi": "गुलाब", "removed": True}  # a removed entry adds no names
    assert product_keys({"name": "Rose"}, gone)[1] == product_keys({"name": "Rose"}, None)[0]


def test_level_of_is_match_level():
    words = ["Money Plant", "money", "plant", "गुलाब", "gulab", "rose", "Primrose", "tulasi", "तुलसी", "NP-0001",
             "0001", "pl", "a", "devil's ivy", "ivy", "gudhal", "गुड़हल", ""]
    for q in words:
        for t in words:
            assert level_of(name_key(q), phonetic(q), name_key(t), phonetic(t)) == match_level(q, t)


# ---- product search ------------------------------------------------------------------------------

NAMES = ["Money Plant", "Snake Plant", "Rose", "Rosemary", "Primrose", "Tulsi", "Holy Basil", "Areca Palm",
         "Ceramic Pot 10 inch", "Plastic Pot 8 inch", "Peace Lily", "Jade Plant", "गुड़हल", "Aloe Vera", "Coco Peat"]
QUERIES = ["rose", "gulab", "गुलाब", "pl", "plant", "pot", "money", "tulasi", "तुलसी", "basil", "aloe", "pea", "np 00",
           "ros", "gud", "jaswand", "crassula", "pothos", "devil s", "hibiscus", "zz", "lily", "mother in", "8 inch"]


def test_indexed_search_ranks_like_scoring_every_product(client):
    shop = Shop(client)
    for n in NAMES:
        shop.product(n, 100, 3)
    db = _db()
    every = run(db.products.find({"business_id": _bid(shop)}, catalog_search.FIELDS).to_list(None))
    for q in QUERIES:
        old = run(catalog_search.search_products(db, every, q))
        new = [p["name"] for p in shop.get("/api/products", params={"search": q}).json()]
        lib = run(catalog_search.library_images(db, every))
        strong = [p["name"] for p in old if _score(q, p, lib) >= STARTS * 2]
        assert [n for n in new if n in strong] == strong, q  # every name/word-start match, in the same order
        assert set(new) <= {p["name"] for p in old}, q  # and nothing the old search would not have found


def _bid(shop):
    return shop.get("/api/auth/me").json()["business"]["id"]


def _score(q, p, lib):
    from search import product_terms
    return search_score(q, product_terms(p, lib.get(p.get("library_id"))))


def test_existing_hindi_and_hinglish_cases(client):
    shop = Shop(client)
    shop.post("/api/products", {"name": "Rose", "library_id": _lib_id(shop, "rose"), "selling_price": 99})
    shop.post("/api/products", {"name": "Tulsi", "library_id": _lib_id(shop, "tulsi"), "selling_price": 49})
    shop.post("/api/products", {"name": "Money Plant", "library_id": _lib_id(shop, "money"), "selling_price": 199})
    names = lambda q: [p["name"] for p in shop.get("/api/products", params={"search": q}).json()]  # noqa: E731
    assert names("gulab") == ["Rose"] and names("गुलाब") == ["Rose"] and names("gulaab") == ["Rose"]
    assert names("tulasi") == ["Tulsi"] and names("holy basil") == ["Tulsi"] and names("pothos") == ["Money Plant"]
    assert names("money")[0] == "Money Plant" and names("0003") == ["Money Plant"]
    s = shop.get("/api/products/suggest", params={"q": "मनी"}).json()
    assert [p["name"] for p in s["own"]] == ["Money Plant"]


def test_products_inserted_without_keys_are_found(client):
    """Supplier bills and demo data insert products without keys: the next search in the shop fills them."""
    shop = Shop(client)
    shop.product("Rose", 99, 1)
    bid = _bid(shop)
    run(_db().products.insert_one({"id": new_id(), "business_id": bid, "sku": "NP-0099", "name": "Snake Plant",
                                   "botanical_name": "", "library_id": None, "stock": 2, "min_stock": 1,
                                   "deleted_at": None, "created_at": now_iso()}))
    assert [p["name"] for p in shop.get("/api/products", params={"search": "snake"}).json()] == ["Snake Plant"]
    assert run(_db().products.count_documents({"business_id": bid, "search_keys": {"$exists": False}})) == 0


def test_renamed_and_deleted_products(client):
    shop = Shop(client)
    p = shop.product("Areca Palm", 300, 2)
    assert client.put(f"/api/products/{p['id']}", headers=shop.h, json={"name": "Bamboo Palm"}).status_code == 200
    names = lambda q: [x["name"] for x in shop.get("/api/products", params={"search": q}).json()]  # noqa: E731
    assert names("bamboo") == ["Bamboo Palm"] and names("areca") == ["Bamboo Palm"]  # still its entry's name
    assert client.delete(f"/api/products/{p['id']}", headers=shop.h).status_code == 200
    assert names("bamboo") == [] and run(_db().products.find_one({"id": p["id"]}))["search_keys"] == []


def test_search_reads_few_documents(client):
    shop = Shop(client)
    bid = _bid(shop)
    docs = []
    for i in range(400):
        doc = {"id": new_id(), "business_id": bid, "sku": f"NP-{i + 1:04d}", "name": f"Plant {i:03d}",
               "botanical_name": "", "library_id": None, "deleted_at": None}
        docs.append({**doc, **catalog_search.keys_for(doc, None)})
    run(_db().products.insert_many(docs))
    for prefix in ("k:plant 1", "k:plant", "p:plnt"):
        q = {"business_id": bid, "deleted_at": None, "search_keys": {"$in": [Regex("^" + prefix)]}}
        plan = run(_db().products.find(q).limit(30).explain())
        stats = plan["executionStats"]
        assert stats["totalDocsExamined"] <= 30 * 2, (prefix, stats["totalDocsExamined"])
        assert "IXSCAN" in str(plan["queryPlanner"]["winningPlan"])
    found = shop.get("/api/products", params={"search": "plant 12", "limit": 30}).json()["items"]
    assert len(found) == 30  # (digits have no sound, so every "Plant nnn" sounds like "plant 12")
    # The check for products without keys, run before each search, reads nothing when there are none.
    plan = run(_db().products.find({"business_id": bid, "search_keys": {"$exists": False}}).limit(500).explain())
    assert plan["executionStats"]["totalDocsExamined"] == 0 and "IXSCAN" in str(plan["queryPlanner"]["winningPlan"])
    # A shop's private library entries: through the {source_business_id, search_keys} index.
    plan = run(_db().library.find({"source_business_id": bid, "public": False, "removed": False,
                                   "search_keys": {"$in": [Regex("^k:ro"), Regex("^s:ro")]}}).limit(30).explain())
    assert "source_business_id_1_search_keys_1" in str(plan["queryPlanner"]["winningPlan"])


# ---- the shared library --------------------------------------------------------------------------

def test_shop_entries_are_private_until_approved(client):
    a, b = Shop(client), Shop(client, phone="9811155667")
    pa = a.product("Kokedama Moss Ball", 450, 2)
    assert a.get("/api/products/suggest", params={"q": "kokedama"}).json()["own"][0]["id"] == pa["id"]
    assert b.get("/api/products/suggest", params={"q": "kokedama"}).json()["library"] == []
    # The same name typed by shop B is linked to the same entry (one entry per name), still private.
    pb = b.product("Kokedama moss ball", 400, 1)
    assert pb["library_id"] == pa["library_id"]
    admin = Shop(client, phone="9000000001", name="Admin", admin=True)
    waiting = admin.get("/api/admin/library", params={"visibility": "private"}).json()
    assert [e["name"] for e in waiting] == ["Kokedama Moss Ball"] and waiting[0]["public"] is False
    admin.post(f"/api/admin/library/{pa['library_id']}/approve")
    c = Shop(client, phone="9811177889")
    assert c.get("/api/products/suggest", params={"q": "koke"}).json()["library"][0]["name"] == "Kokedama Moss Ball"
    admin.post(f"/api/admin/library/{pa['library_id']}/unpublish")
    assert c.get("/api/products/suggest", params={"q": "koke"}).json()["library"] == []


def test_shared_names_are_cleaned():
    assert library.clean_name("Rose plant call 98111 22334 www.greenshop.in") == "Rose plant call"
    assert library.clean_name("Money Plant - visit https://x.co/abc, mail me@shop.com") == "Money Plant - visit mail"
    assert library.clean_name("Ceramic Pot 10 inch") == "Ceramic Pot 10 inch"
    assert library.clean_name("Bonsai +91-98111-22334") == "Bonsai"
    long = library.clean_name("Very " * 40)
    assert len(long) <= library.NAME_MAX and not long.endswith(" ")


def test_cleaned_name_links_and_entry_has_no_phone(client):
    shop = Shop(client)
    p = shop.product("Areca Palm 9811122334", 300, 1)
    entry = run(_db().library.find_one({"id": p["library_id"]}))
    assert entry["name"] == "Areca Palm" and entry["source"] == "starter"  # the starter entry, by its clean name
    q = shop.product("Gift hamper call 9811122334", 300, 1)
    entry = run(_db().library.find_one({"id": q["library_id"]}))
    assert entry["name"] == "Gift hamper call" and entry["public"] is False


def test_library_cache_follows_changes(client):
    shop = Shop(client)
    admin = Shop(client, phone="9000000001", name="Admin", admin=True)
    rose = _lib_id(shop, "rose")
    assert client.patch(f"/api/admin/library/{rose}", headers=admin.h, json={"name_hi": "रोज़ा"}).status_code == 200
    assert shop.get("/api/products/suggest", params={"q": "रोज़ा"}).json()["library"][0]["id"] == rose
    # Another process changed an entry: this one sees it once its copy is older than CACHE_SECONDS.
    db = _db()
    run(db.library.update_one({"id": rose}, {"$set": {"name": "Desi Rose", "search_keys": library.library_keys(
        {"name": "Desi Rose", "name_hi": "रोज़ा", "botanical": "Rosa", "aliases": ["gulab"]})}}))
    run(db.counters.update_one({"_id": library.VERSION_ID}, {"$set": {"v": "elsewhere"}}))
    assert shop.get("/api/products/suggest", params={"q": "desi"}).json()["library"] == []  # still the copy
    library._index.checked -= library.CACHE_SECONDS
    assert shop.get("/api/products/suggest", params={"q": "desi"}).json()["library"][0]["id"] == rose
    # Names taken by another entry are refused.
    tulsi = _lib_id(shop, "tulsi")
    r = client.patch(f"/api/admin/library/{tulsi}", headers=admin.h, json={"name": "rose"})
    assert r.status_code == 409 and r.json()["detail"] == "Another catalogue item already has this name."


def test_new_entry_names_reach_linked_products(client, claude, monkeypatch):
    from settings import get_settings
    queued = []

    async def enqueue(name, *args, **kw):
        queued.append((name, args, kw))

    monkeypatch.setattr(get_settings(), "ai_enrich_names", True)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    a, b = Shop(client), Shop(client, phone="9811155667")
    pa = a.post("/api/products", {"name": "Holy Basil Plant", "selling_price": 60}).json()
    pb = b.post("/api/products", {"name": "Holy basil plant", "selling_price": 70}).json()
    assert pb["library_id"] == pa["library_id"]
    assert [q[0] for q in queued] == ["look_up_names"] and queued[0][2]["_job_id"].endswith(pa["library_id"])
    run(jobs.REGISTRY["look_up_names"]({"db": _db()}, pa["library_id"]))
    for shop, p in ((a, pa), (b, pb)):  # both shops' products now answer to the Hindi and Hinglish names
        assert [x["id"] for x in shop.get("/api/products", params={"search": "tulsi"}).json()] == [p["id"]]
        assert [x["id"] for x in shop.get("/api/products", params={"search": "तुलसी"}).json()] == [p["id"]]
    entry = run(_db().library.find_one({"id": pa["library_id"]}))
    assert entry["public"] is False and entry["hidden"] is False  # the AI never publishes an entry


# ---- product codes -------------------------------------------------------------------------------

def test_codes_come_from_the_counter_and_are_unique(client):
    shop = Shop(client)
    bid = _bid(shop)
    assert [shop.product(f"Item {i}", 10, 0)["sku"] for i in range(3)] == ["NP-0001", "NP-0002", "NP-0003"]
    # A code given out the old way (count + 1, as supplier bills still do) is skipped, not repeated.
    run(_db().products.insert_one({"id": new_id(), "business_id": bid, "sku": "NP-0004", "name": "From bill",
                                   "deleted_at": None}))
    assert shop.product("Item 4", 10, 0)["sku"] == "NP-0005"
    with pytest.raises(DuplicateKeyError):
        run(_db().products.insert_one({"id": new_id(), "business_id": bid, "sku": "NP-0005", "name": "Twin"}))
    other = Shop(client, phone="9811155667")
    assert other.product("Item", 10, 0)["sku"] == "NP-0001"  # per shop


def test_codes_stay_unique_when_added_at_once(client):
    shop = Shop(client)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        skus = list(pool.map(lambda i: shop.product(f"Plant {i}", 10, 0)["sku"], range(16)))
    assert len(set(skus)) == 16


# ---- the product list ----------------------------------------------------------------------------

def test_paged_list_and_counts(client):
    shop = Shop(client)
    for i, (n, stock) in enumerate([("Fern", 0), ("Aloe", 9), ("Cactus", 2), ("Dahlia", 20), ("Begonia", 1),
                                    ("Gerbera", 30), ("Echeveria", -2)]):
        shop.product(n, 50 + i, max(stock, 0), min_stock=5)
        if stock < 0:
            p = shop.get("/api/products", params={"search": n}).json()[0]
            run(_db().products.update_one({"id": p["id"]}, {"$set": {"stock": stock}}))
    old = shop.get("/api/products").json()  # 1.1.0: no parameters, the whole array by name
    assert isinstance(old, list) and [p["name"] for p in old] == sorted(p["name"] for p in old) and len(old) == 7
    seen, cursor = [], ""
    while True:
        page = shop.get("/api/products", params={"limit": 3, **({"cursor": cursor} if cursor else {})}).json()
        assert len(page["items"]) <= 3
        seen += [p["name"] for p in page["items"]]
        if not (cursor := page["next_cursor"]):
            break
    assert seen == [p["name"] for p in old]
    low = shop.get("/api/products", params={"limit": 50, "low": True}).json()
    assert [p["name"] for p in low["items"]] == ["Begonia", "Cactus", "Echeveria", "Fern"]
    assert low["next_cursor"] is None
    found = shop.get("/api/products", params={"limit": 2, "search": "e"}).json()
    assert [p["name"] for p in found["items"]] == ["Echeveria"] and found["next_cursor"] is None
    assert shop.get("/api/products", params={"cursor": "not-a-cursor"}).status_code == 400
    assert shop.get("/api/products", params={"limit": 500}).status_code == 422
    assert shop.get("/api/products/counts").json() == {"all": 7, "low": 2, "out": 2, "below": 1}
    pots = shop.get("/api/products", params={"limit": 10, "category": shop.cat["Pots"]}).json()
    assert pots == {"items": [], "next_cursor": None}
    assert all("search_keys" not in p and "own_keys" not in p for p in old)


# ---- migrations ----------------------------------------------------------------------------------

def test_migrations_fix_old_data(client):
    """Data written before WP-3: duplicate library names, no keys, no `public`, products sharing a code."""
    shop = Shop(client)
    bid = _bid(shop)
    db = _db()
    run(db.library.drop_index("name_key_live_unique"))
    run(db.products.drop_index("business_sku_unique"))
    old_entry = {"name": "Coco Peat", "name_key": "coco peat", "name_hi": "", "botanical": "", "aliases": [],
                 "source": "shop", "removed": False, "image_status": "none"}
    first, second = new_id(), new_id()
    run(db.library.insert_many([{**old_entry, "id": first, "created_at": "2026-01-01T00:00:00+00:00"},
                                {**old_entry, "id": second, "created_at": "2026-02-01T00:00:00+00:00"},
                                {**old_entry, "id": new_id(), "name": "Desk Lamp", "name_key": "desk lamp",
                                 "hidden": True, "created_at": "2026-02-01T00:00:00+00:00"}]))
    base = {"business_id": bid, "botanical_name": "", "deleted_at": None, "stock": 1, "min_stock": 0}
    run(db.products.insert_many([
        {**base, "id": "p1", "sku": "NP-0001", "name": "Coco Peat 1 kg", "library_id": second,
         "created_at": "2026-01-01"},
        {**base, "id": "p2", "sku": "NP-0001", "name": "Red Pot", "library_id": None, "created_at": "2026-01-02"},
        {**base, "id": "p3", "sku": "NP-0007", "name": "Blue Pot", "library_id": None, "created_at": "2026-01-03"}]))
    run(db.migrations.delete_many({"_id": {"$regex": "^03"}}))
    assert run(migrations.up(db)) == ["0301_library_search_keys", "0302_unique_skus", "0303_product_search_keys"]
    dupe = run(db.library.find_one({"id": second}))
    assert dupe["removed"] is True and dupe["merged_into"] == first
    assert run(db.products.find_one({"id": "p1"}))["library_id"] == first
    kept = run(db.library.find_one({"id": first}))
    assert kept["public"] is True and "k:coco peat" + END in kept["search_keys"]
    assert run(db.library.find_one({"name_key": "desk lamp"}))["public"] is False  # it was hidden
    skus = {p["id"]: p["sku"] for p in run(db.products.find({"business_id": bid}).to_list(None))}
    assert skus["p1"] == "NP-0001" and skus["p2"] == "NP-0008" and skus["p3"] == "NP-0007"
    assert shop.product("Green Pot", 10, 0)["sku"] == "NP-0009"
    pots = [p["name"] for p in shop.get("/api/products", params={"search": "pot"}).json()]
    assert pots == ["Blue Pot", "Green Pot", "Red Pot"]
    assert run(migrations.up(db)) == []


# ---- speed (opt-in: RUN_BENCH=1) -----------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("RUN_BENCH") != "1", reason="benchmark: RUN_BENCH=1")
def test_suggest_speed_with_a_big_library(client):
    """200k library entries (10k public, the rest private to 1000 shops) and 5k products in the shop."""
    shop = Shop(client)
    bid = _bid(shop)
    db = _db()
    words = ["rose", "lily", "palm", "fern", "pot", "basil", "money", "snake", "jade", "aloe", "tulsi", "orchid",
             "cactus", "bonsai", "ivy", "mint", "neem", "lemon", "mango", "peat"]
    entries = []
    for i in range(200_000):
        name = f"{words[i % 20]} {words[(i // 20) % 20]} {i}"
        e = {"id": new_id(), "name": name, "name_key": name_key(name), "name_hi": "", "botanical": "", "aliases": [],
             "default_category": "", "image_path": None, "image_status": "none", "source": "shop",
             "source_business_id": f"shop-{i % 1000}", "public": i < 10_000, "removed": False}
        entries.append({**e, "search_keys": library.library_keys(e)})
    for i in range(0, len(entries), 10_000):
        run(db.library.insert_many(entries[i:i + 10_000]))
    products = []
    for i in range(5000):
        p = {"id": new_id(), "business_id": bid, "sku": f"NP-{i + 1:04d}", "name": f"{words[i % 20]} {i}",
             "botanical_name": "", "library_id": None, "deleted_at": None, "stock": 1, "min_stock": 0}
        products.append({**p, **catalog_search.keys_for(p, None)})
    run(db.products.insert_many(products))
    run(library.changed(db))
    took = []
    for q in ["ro", "rose", "rose l", "lily", "pa", "palm 1", "ba", "mon", "tul", "orch"] * 10:
        t = time.perf_counter()
        r = shop.get("/api/products/suggest", params={"q": q})
        took.append((time.perf_counter() - t) * 1000)
        assert r.status_code == 200
    p95 = statistics.quantiles(took, n=20)[18]
    print(f"suggest p50 {statistics.median(took):.1f} ms, p95 {p95:.1f} ms")
    assert p95 < 50
