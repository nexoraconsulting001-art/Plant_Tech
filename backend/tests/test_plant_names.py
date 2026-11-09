"""Plant names (owner, 10 Nov 2026: "Tulsi = Holy Basil, Genda = Marigold; find all names, also from Claude, save
them so Claude finds the plant faster"): the names look-up, the hourly run, same-plant entries merged, the owner's
other names, the starter list, and matching what Claude or a bill read through every name of a product."""
import importlib

import pytest

import ai_client
import ai_usage
import db as db_module
from common import name_key
from conftest import PLANT, Shop, photo_b64, run
from routers import library


def _db():
    return db_module.state.db


def _entry(key):
    return run(_db().library.find_one({"name_key": key, "removed": False}))


def _scan(shop, claude, **answer):
    claude.plant = {**PLANT, "stock_match": "", "stock_alternatives": [], **answer}
    r = shop.post("/api/ai/identify-plant", {"image_base64": photo_b64(color=(len(str(answer)) % 250, 9, 9))})
    assert r.status_code == 200, r.text
    return r.json()


def test_claude_names_find_the_shops_product_under_any_name(client, claude, monkeypatch):
    from settings import get_settings

    monkeypatch.setattr(get_settings(), "ai_daily_limit", 50)
    shop = Shop(client)
    tulsi = shop.product("Tulsi", 60, 10, "Herbs")  # linked to the starter Tulsi: holy basil, Ocimum sanctum, ...
    marigold = shop.product("Marigold", 30, 40)  # the starter Marigold: genda, gainda, Tagetes erecta
    kesari = shop.product("Kesari", 45, 5, other_names=["Saffron Marigold"])  # the owner's own other name
    assert tulsi["library_id"] == _entry("tulsi")["id"]
    for answer, product in ((dict(plant_name="Holy Basil", scientific_name="", alternatives=[]), tulsi),
                            (dict(plant_name="Basil", scientific_name="Ocimum sanctum", alternatives=[]), tulsi),
                            (dict(plant_name="Genda", scientific_name="", alternatives=[]), marigold),
                            (dict(plant_name="Gainda Phool", scientific_name="Tagetes erecta", alternatives=[]), marigold),
                            (dict(plant_name="Saffron Marigold", scientific_name="", alternatives=[]), kesari)):
        assert _scan(shop, claude, **answer)["matched_product"]["id"] == product["id"], answer
    # Claude copied an other name from the list instead of the product's name: still that product
    assert _scan(shop, claude, plant_name="x", scientific_name="", alternatives=[],
                 stock_match="Holy Basil")["matched_product"]["id"] == tulsi["id"]
    # the list Claude sees carries the names
    system = claude[-1][1]["system"][1]["text"]
    assert "- Tulsi (Ocimum tenuiflorum; also: तुलसी, holy basil, tulasi, ocimum sanctum, rama tulsi, krishna tulsi)" in system
    assert "- Kesari (also: Saffron Marigold)" in system


def test_two_products_of_one_plant(client, claude):
    """Tulsi and Shyama Tulsi are both the Tulsi plant: Claude's pick is the match and the other comes first among
    the other matches; without a pick neither is guessed, both are offered."""
    shop = Shop(client)
    tulsi = shop.product("Tulsi", 60, 10, "Herbs")
    shyama = shop.product("Shyama Tulsi", 80, 4, "Herbs", library_id=_entry("tulsi")["id"])
    r = _scan(shop, claude, plant_name="Holy Basil", scientific_name="Ocimum tenuiflorum", alternatives=[],
              stock_match="Shyama Tulsi")
    assert r["matched_product"]["id"] == shyama["id"] and r["recommended_products"][0]["id"] == tulsi["id"]
    r = _scan(shop, claude, plant_name="Holy Basil", scientific_name="", alternatives=[])
    assert r["matched_product"] is None
    assert {p["id"] for p in r["recommended_products"]} == {tulsi["id"], shyama["id"]}


def test_lookup_saves_every_name_and_joins_the_same_plant(client, claude):
    """A shop's "Holy Basil Plant" starts its own entry; the look-up finds it is Ocimum tenuiflorum, the starter
    Tulsi's plant: the product moves to Tulsi (its names and picture), the private entry is merged away."""
    shop = Shop(client)
    p = shop.post("/api/products", {"name": "Holy Basil Plant", "selling_price": 60}).json()
    own = p["library_id"]
    assert own != _entry("tulsi")["id"]
    assert run(library.enrich(_db(), own)) is True
    old = run(_db().library.find_one({"id": own}))
    assert old["removed"] is True and old["merged_into"] == _entry("tulsi")["id"]
    assert old["botanical"] == "Ocimum tenuiflorum" and "Ocimum sanctum" in old["aliases"]
    assert old["names_version"] == library.NAMES_VERSION
    assert shop.get(f"/api/products/{p['id']}").json()["library_id"] == _entry("tulsi")["id"]
    for q in ("tulsi", "तुलसी", "holy basil", "ocimum sanctum"):
        assert [x["id"] for x in shop.get("/api/products", params={"search": q}).json()] == [p["id"]], q
    assert run(library.enrich(_db(), own)) is False  # done: never looked up twice


def test_a_new_product_with_a_botanical_name_joins_its_plant(client):
    shop = Shop(client)
    p = shop.post("/api/products", {"name": "Kali Tulsi", "botanical_name": "Ocimum sanctum", "selling_price": 70}).json()
    assert p["library_id"] == _entry("tulsi")["id"]  # Ocimum sanctum is one of the starter Tulsi's names


def test_hourly_run_looks_up_old_entries_within_limits(client, claude, monkeypatch):
    from settings import get_settings

    db = _db()
    monkeypatch.setattr(get_settings(), "ai_enrich_names", False)
    assert run(library.refresh_names({"db": db})) == 0  # off by setting
    monkeypatch.setattr(get_settings(), "ai_enrich_names", True)
    total = len(library.STARTER)
    assert run(library.refresh_names({"db": db})) == library.NAMES_BATCH
    assert run(library.refresh_names({"db": db})) == total - library.NAMES_BATCH
    assert run(library.refresh_names({"db": db})) == 0  # all at NAMES_VERSION
    assert run(db.library.count_documents({"names_version": library.NAMES_VERSION})) == total
    # a starter entry keeps its own botanical name; Claude's other name for it becomes another name
    claude.names = {**claude.names, "botanical_name": "Sansevieria trifasciata", "botanical_synonyms": []}
    run(db.library.update_one({"name_key": "snake plant"}, {"$set": {"names_version": 1}}))
    assert run(library.refresh_names({"db": db})) == 1
    snake = _entry("snake plant")
    assert snake["botanical"] == "Dracaena trifasciata" and "Sansevieria trifasciata" in snake["aliases"]
    assert snake.get("hidden") is not True and snake["public"] is True  # starter entries stay as they are


def test_no_answer_is_tried_three_times_a_day_apart_and_the_budget_stops_the_run(client, claude, monkeypatch):
    from settings import get_settings

    db = _db()
    monkeypatch.setattr(get_settings(), "ai_enrich_names", True)
    run(db.library.update_many({}, {"$set": {"names_version": library.NAMES_VERSION}}))
    run(db.library.update_one({"name_key": "neem"}, {"$unset": {"names_version": 1}}))
    claude.refuse = True
    assert run(library.refresh_names({"db": db})) == 0
    neem = _entry("neem")
    assert neem["names_tries"] == 1 and neem["names_tried_at"]
    assert run(library.refresh_names({"db": db})) == 0 and _entry("neem")["names_tries"] == 1  # not again today
    run(db.library.update_one({"name_key": "neem"}, {"$set": {"names_tried_at": "2000-01-01", "names_tries": 3}}))
    assert run(library.refresh_names({"db": db})) == 0 and _entry("neem")["names_tries"] == 3  # given up
    claude.refuse = False
    run(db.library.update_one({"name_key": "neem"}, {"$set": {"names_tries": 0}}))

    async def spent(db=None):
        return True
    monkeypatch.setattr(ai_usage, "over_budget", spent)
    assert run(library.refresh_names({"db": db})) == 0 and "names_version" not in _entry("neem")


def test_other_names_are_cleaned_searchable_and_editable(client):
    shop = Shop(client)
    p = shop.post("/api/products", {"name": "Marigold Orange", "selling_price": 30,
                                    "other_names": ["Kesariya Genda", " kesariya  genda ", "Marigold Orange",
                                                    "call 9811122334", ""]}).json()
    assert p["other_names"] == ["Kesariya Genda", "call"]
    assert [x["id"] for x in shop.get("/api/products", params={"search": "kesariya"}).json()] == [p["id"]]
    r = shop.c.put(f"/api/products/{p['id']}", headers=shop.h, json={"other_names": ["Narangi Genda"]})
    assert r.status_code == 200 and r.json()["other_names"] == ["Narangi Genda"]
    assert [x["id"] for x in shop.get("/api/products", params={"search": "narangi"}).json()] == [p["id"]]
    assert shop.get("/api/products", params={"search": "kesariya"}).json() == []
    too_many = shop.c.put(f"/api/products/{p['id']}", headers=shop.h, json={"other_names": [f"n{i}" for i in range(11)]})
    assert too_many.status_code == 422


def test_supplier_bill_lines_match_through_other_names(client, claude):
    shop = Shop(client)
    tulsi = shop.product("Tulsi", 60, 10, "Herbs")
    claude.invoice = {**claude.invoice, "items": [{"name": "Holy Basil", "quantity": 10, "purchase_price": 20}]}
    r = shop.post("/api/ai/scan-invoice", {"image_base64": photo_b64()}).json()
    assert r["items"][0]["product_id"] == tulsi["id"] and r["items"][0]["matched_name"] == "Tulsi"


def test_starter_names_migration_adds_and_merges(client):
    db = _db()
    migration = importlib.import_module("migrations.1102_library_names")
    run(db.library.delete_one({"name_key": "marigold"}))
    run(db.library.update_one({"name_key": "tulsi"}, {"$set": {"aliases": ["holy basil", "my own name"], "name_hi": ""}}))
    run(migration.up(db))
    run(migration.up(db))  # twice: no change
    assert run(db.library.count_documents({"source": "starter", "removed": False})) == len(library.STARTER)
    tulsi = _entry("tulsi")
    assert tulsi["name_hi"] == "तुलसी" and tulsi["aliases"][:2] == ["holy basil", "my own name"]
    assert "ocimum sanctum" in tulsi["aliases"] and len(tulsi["aliases"]) == len(set(map(name_key, tulsi["aliases"])))
    assert _entry("marigold")["botanical"] == "Tagetes erecta"


def test_stock_line_format():
    line = ai_client._stock_line({"name": "Tulsi", "botanical_name": "Ocimum tenuiflorum",
                                  "also": ["Tulsi", "ocimum tenuiflorum", "तुलसी", "Holy Basil", "holy basil", "x" * 60]})
    assert line == "- Tulsi (Ocimum tenuiflorum; also: तुलसी, Holy Basil, " + "x" * ai_client.STOCK_NAME_CHARS + ")"
    assert ai_client._stock_line({"name": "Pot"}) == "- Pot"


@pytest.mark.parametrize("names, expected", [(["Gulab"], "Rose"), (["Ocimum sanctum"], "Tulsi"), (["Pot"], None)])
def test_best_product_with_every_name(names, expected):
    from search import best_product, product_names

    entries = {"rose": {"name": "Rose", "name_hi": "गुलाब", "botanical": "Rosa", "aliases": ["gulab"]},
               "tulsi": {"name": "Tulsi", "name_hi": "तुलसी", "botanical": "Ocimum tenuiflorum",
                         "aliases": ["holy basil", "ocimum sanctum"]}}
    products = [{"id": "1", "name": "Rose", "library_id": "rose"}, {"id": "2", "name": "Tulsi", "library_id": "tulsi"},
                {"id": "3", "name": "Pothos", "library_id": None}]
    match, _ = best_product(products, names, lambda p: product_names(p, entries.get(p.get("library_id"))))
    assert (match["name"] if match else None) == expected
