"""Product search per keystroke (load test, 3 Nov): no extra query for products without keys on every search,
every writer sets the keys, and the faster ranking returns exactly what the old one did."""
import itertools

import pytest

import catalog_search
import db as db_module
import demo_data
import search
from common import name_key, new_id, now_iso, now_ist
from conftest import MOCK, Shop, run
from routers import customers, products

pytestmark = pytest.mark.skipif(MOCK, reason="needs the real MongoDB (indexes, pipeline updates)")


def _db():
    return db_module.state.db


def _bid(shop):
    return shop.get("/api/auth/me").json()["business"]["id"]


def _counting(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    async def counted(*args, **kwargs):
        calls.append(args[1])
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


# ---- the safety net runs once per shop and period, not per keystroke ---------------------------------------------
def test_unkeyed_check_runs_once_per_shop_not_per_keystroke(client, monkeypatch):
    shop, other = Shop(client), Shop(client, phone="9811122999", name="Mohan")
    shop.product("Rose", 99, 3)
    other.product("Rose", 99, 3)
    calls = _counting(monkeypatch, catalog_search, "index_unkeyed")
    for typed in ("r", "ro", "ros", "rose"):
        assert shop.get("/api/products", params={"search": typed}).status_code == 200
    other.get("/api/products", params={"search": "rose"})
    assert sorted(calls) == sorted([_bid(shop), _bid(other)])  # one check per shop, not one per keystroke

    # A product an older server version saved without keys (deploy overlap, rollback) is found after the period.
    run(_db().products.insert_one({"id": new_id(), "business_id": _bid(shop), "sku": "NP-0099", "name": "Snake Plant",
                                   "botanical_name": "", "library_id": None, "stock": 2, "min_stock": 1,
                                   "deleted_at": None, "created_at": now_iso()}))
    catalog_search.unkeyed_checked.forget(_bid(shop))  # = UNKEYED_EVERY seconds later
    assert [p["name"] for p in shop.get("/api/products", params={"search": "snake"}).json()] == ["Snake Plant"]
    assert len(calls) == 3


def test_a_failed_check_is_tried_again(client, monkeypatch):
    shop = Shop(client)
    bid = _bid(shop)

    async def down(*_a, **_kw):
        raise RuntimeError("database went away")

    monkeypatch.setattr(catalog_search, "index_unkeyed", down)
    with pytest.raises(RuntimeError):
        run(catalog_search.find_products(_db(), bid, "rose"))
    assert catalog_search.unkeyed_checked.due(bid)  # not counted as done


def test_customer_fill_runs_once_per_shop_not_per_keystroke(client, monkeypatch):
    shop = Shop(client)
    shop.post("/api/customers", {"name": "Kavita Rao", "phone": "9811100066"})
    calls = _counting(monkeypatch, customers, "_fill_now")
    for typed in ("ka", "kav", "kavi"):
        assert len(shop.get("/api/customers/find", params={"q": typed}).json()) == 1
    shop.get("/api/customers", params={"limit": 5})
    assert calls == [_bid(shop)]


# ---- every writer of products sets the keys -------------------------------------------------------------------------
def test_every_writer_sets_search_keys(client, monkeypatch):
    shop = Shop(client)
    p = shop.product("Areca Palm", 300, 2)  # create
    assert shop.post("/api/purchases", {"supplier": "Green Farms", "invoice_number": "GF-1", "items": [
        {"name": "Snake Plant", "quantity": 2, "purchase_price": 100},  # supplier bill: a new product
        {"product_id": p["id"], "name": "Areca Palm", "quantity": 1, "purchase_price": 150}]}).status_code == 200
    assert client.put(f"/api/products/{p['id']}", headers=shop.h, json={"name": "Bamboo Palm"}).status_code == 200
    run(demo_data.seed(_db(), "9811122334", now=now_ist().replace(hour=15, minute=0)))  # demo data
    assert shop.post(f"/api/products/{p['id']}/stock", {"change": 3, "reason": "Count"}).status_code == 200
    db = _db()
    assert run(db.products.count_documents({})) > 40
    assert run(db.products.count_documents({"search_keys": {"$exists": False}})) == 0
    assert run(db.products.count_documents({"own_keys": {"$exists": False}})) == 0
    for doc in run(db.products.find({"deleted_at": None}, {"_id": 0}).to_list(None)):
        entry = run(db.library.find_one({"id": doc["library_id"]})) if doc.get("library_id") else None
        assert sorted(doc["search_keys"]) == catalog_search.keys_for(doc, entry)["search_keys"], doc["name"]


def test_loadtest_seed_docs_carry_keys_and_fields():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "loadtest" / "seed.py"
    spec = importlib.util.spec_from_file_location("loadtest_seed", path)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    _, _, _, prods, custs = seed.shop_docs(1, 20, 20, "hash", now_iso())
    assert all(p["search_keys"] == catalog_search.keys_for(p, None)["search_keys"] for p in prods)
    assert all(c["name_key"] == name_key(c["name"]) and c["phone10"] for c in custs)


# ---- the ranking is exactly the old one ---------------------------------------------------------------------------
def _old_ranking(products: list[dict], lib: dict, query: str) -> list[dict]:
    """search_products as it was before the forms were cached and the query worked out once."""
    def score(p):
        entry = lib.get(p.get("library_id"))
        terms = search.product_terms(p, entry)
        return search.score_of(name_key(query), search.phonetic(query),
                               [(name_key(t), search.phonetic(t)) if t else ("", "") for t in terms])
    scored = [(s, p) for p in products if (s := score(p))]
    scored.sort(key=lambda sp: (-sp[0], sp[1]["name"].lower()))
    return [p for _, p in scored]


NAMES = ["Money Plant", "money plant", "Jade Plant", "Rose", "Desi Rose", "Primrose", "Tulsi", "Holy Basil",
         "Snake Plant", "Areca Palm", "Bamboo Palm", "Plastic Pot", "Terracotta Pot 8 inch", "Pothos", "Gulab Jamun Tree",
         "ZZ Plant", "Aloe Vera", "Neem Oil", "Coco Peat", "Cocopeat", "Marigold", "Mogra", "Jasmine", "Kopi Plant", ""]
QUERIES = ["mo", "money", "monei", "plant", "pl", "rose", "gulab", "गुलाब", "गुलाब जामुन", "tulsi", "tulasi", "तुलसी",
           "basil", "pot", "8 inch", "coco", "np 0003", "NP-0012", "jas", "zz", "a", "ज़", "x", "  rose  "]


def test_ranking_is_identical_to_the_old_one(client):
    shop = Shop(client)
    db = _db()
    # Library entries with Hindi names and aliases (the starter library has them), some linked to products.
    starter = run(db.library.find({"removed": False}, {"_id": 0}).limit(40).to_list(40))
    prods = []
    for i, (name, entry) in enumerate(zip(itertools.cycle(NAMES), itertools.cycle([None, *starter]))):
        if i >= 200:
            break
        prods.append({"id": f"p{i}", "name": name or entry and entry["name"] or "Item", "sku": f"NP-{i:04d}",
                      "botanical_name": "Epipremnum aureum" if i % 7 == 0 else "",
                      "library_id": entry["id"] if entry else None})
    lib = run(catalog_search.library_images(db, prods))
    assert any(e.get("name_hi") for e in lib.values())
    for q in QUERIES:
        assert run(catalog_search.search_products(db, prods, q)) == _old_ranking(prods, lib, q), q
        assert run(catalog_search.search_products(db, prods, q, lib)) == _old_ranking(prods, lib, q), q
    for t in [*NAMES, *QUERIES, *(e.get("name_hi") or "" for e in lib.values())]:
        if t:
            assert search.forms_of(t) == (name_key(t), search.phonetic(t)), t
    assert shop  # the shop only exists to have the starter library in the database


def test_search_responses_are_unchanged(client, monkeypatch):
    """The whole route: the same items and fields with the library entries passed on as when views read them."""
    shop = Shop(client)
    for name in ("Money Plant", "Rose", "Desi Rose", "Tulsi", "Plastic Pot", "Jade Plant"):
        shop.product(name, 100, 5)
    queries = ["mo", "rose", "गुलाब", "tulsi", "pot", "plant"]
    new = {q: shop.get("/api/products", params={"search": q}).json() for q in queries}
    paged = {q: shop.get("/api/products", params={"search": q, "limit": 3}).json() for q in queries}

    async def old_views(db, user, found, lib=None):
        return await real_views(db, user, found)  # reads the entries itself, as before

    real_views = products.views
    monkeypatch.setattr(products, "views", old_views)
    assert {q: shop.get("/api/products", params={"search": q}).json() for q in queries} == new
    assert {q: shop.get("/api/products", params={"search": q, "limit": 3}).json() for q in queries} == paged
    assert [p["name"] for p in new["rose"]][:2] == ["Rose", "Desi Rose"]
