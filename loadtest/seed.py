"""Load-test shops: N nurseries, each with an owner, categories, products and customers, written straight into a
target MongoDB database with bulk inserts, then the migrations (indexes, backfills) run on it. Writes shops.json
for k6 and locust (loadtest/README.md).

  cd backend
  python ../loadtest/seed.py --mongo-url "mongodb://127.0.0.1:27117/?replicaSet=rs0" --db hh_loadtest \\
      --shops 200 --products 300 --customers 2000 --out ../loadtest/shops.json
  # with the target server's JWT_SECRET in the environment, shops.json also holds a login token per shop, so the
  # test does not start with a burst of logins (bcrypt) that would itself be the bottleneck

Safety: the database name must contain "loadtest" (anything else needs --force), and a database that already
has migrations applied is refused unless --append (seed a fresh database: backfill migrations then cover the
seeded rows). Every row is marked loadtest=True; --drop deletes the whole database first.
Owner phones are +91 6xxxxxxxxx and customer phones +91 7xxxxxxxxx (made up; nothing is ever sent to them).
The documents match what the API itself writes (routers/auth.py _new_owner, business.py _create_category,
products.py create_product, customers.py create_customer); backend/tests/test_loadtest_seed.py bills against a
seeded shop through the API, so a schema change that breaks this script fails CI.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

PASSWORD = "loadtest-pass-1"
CATEGORIES = [("Indoor Plants", 0), ("Outdoor Plants", 0), ("Flowering Plants", 0), ("Succulents", 0), ("Herbs", 0),
              ("Pots", 18), ("Soil", 5), ("Fertilizers", 5), ("Garden Tools", 5)]
# name, category, price
BASE_PRODUCTS = [
    ("Money Plant", "Indoor Plants", 199), ("Snake Plant", "Indoor Plants", 349), ("Areca Palm", "Indoor Plants", 599),
    ("Peace Lily", "Indoor Plants", 399), ("ZZ Plant", "Indoor Plants", 549), ("Rubber Plant", "Indoor Plants", 449),
    ("Spider Plant", "Indoor Plants", 149), ("Syngonium", "Indoor Plants", 179), ("Jade Plant", "Succulents", 249),
    ("Aloe Vera", "Succulents", 149), ("Echeveria", "Succulents", 129), ("Haworthia", "Succulents", 159),
    ("Rose", "Flowering Plants", 179), ("Hibiscus", "Flowering Plants", 229), ("Bougainvillea", "Flowering Plants", 249),
    ("Mogra", "Flowering Plants", 199), ("Marigold", "Flowering Plants", 49), ("Adenium", "Flowering Plants", 399),
    ("Tulsi", "Herbs", 79), ("Curry Leaf", "Herbs", 129), ("Mint", "Herbs", 69), ("Ixora", "Outdoor Plants", 199),
    ("Croton", "Outdoor Plants", 179), ("Ficus", "Outdoor Plants", 299), ("Plastic Pot", "Pots", 49),
    ("Terracotta Pot", "Pots", 149), ("Ceramic Pot", "Pots", 449), ("Hanging Basket", "Pots", 199),
    ("Potting Mix", "Soil", 199), ("Cocopeat", "Soil", 149), ("Vermicompost", "Soil", 99),
    ("Plant Food", "Fertilizers", 149), ("Neem Oil", "Fertilizers", 199), ("Hand Trowel", "Garden Tools", 149),
    ("Pruning Shears", "Garden Tools", 349), ("Watering Can", "Garden Tools", 249),
]
VARIANTS = ["", " Small", " Medium", " Large", " 4 inch", " 6 inch", " 8 inch", " 10 inch", " 12 inch", " Premium"]
FIRST = ["Anita", "Rajesh", "Priya", "Amit", "Sunita", "Vikram", "Neha", "Rohit", "Kavita", "Sanjay", "Pooja", "Arjun",
         "Meena", "Deepak", "Ritu", "Manoj", "Shalini", "Karan", "Suresh", "Geeta", "Harish", "Nisha", "Prakash", "Rekha"]
LAST = ["Sharma", "Gupta", "Verma", "Kumar", "Yadav", "Singh", "Agarwal", "Mehta", "Joshi", "Malhotra", "Bansal",
        "Reddy", "Iyer", "Chauhan", "Kapoor", "Tiwari", "Nair", "Arora", "Patel", "Rawat"]
# what the load test types: 2 to 4 letters of real names, so the searches find something
PRODUCT_TERMS = sorted({n.lower()[:k] for n, _, _ in BASE_PRODUCTS for k in (3, 4)})
CUSTOMER_TERMS = sorted({n.lower()[:k] for n in FIRST for k in (3, 4)})


def owner_phone(i: int) -> str:
    return f"+916{i:09d}"


def customer_phone(shop: int, j: int) -> str:
    return f"+917{shop % 10000:04d}{j:05d}"


def shop_docs(i: int, products: int, customers: int, password_hash: str, now: str):
    """(business, owner, categories, products, customers) of load-test shop number i, as the API would write them."""
    from catalog_search import keys_for
    from common import name_key, new_id
    from routers.customers import search_fields

    bid = new_id()
    phone = owner_phone(i)
    upi = {"id": new_id(), "upi_id": f"loadtest{i}@upi", "label": ""}  # business.py: upi_ids are {id, upi_id, label}
    business = {"id": bid, "name": f"Load Test Nursery {i}", "address": f"Plot {i}, Sector 5, Gurugram",
                "phone": phone, "gst_registered": True, "gstin": "06ABCDE1234F1Z5", "upi_id": upi["upi_id"],
                "upi_ids": [upi], "default_upi": upi["id"], "whatsapp_bill": True, "logo_path": None,
                "footer_note": "", "currency": "₹", "onboarding_done": True, "profit_percent": 60, "created_at": now,
                "loadtest": True}
    owner = {"id": new_id(), "business_id": bid, "role": "owner", "name": f"Owner {i}", "phone": phone, "email": None,
             "google_sub": None, "password_hash": password_hash, "permissions": {}, "active": True,
             "token_version": 0, "failed_logins": 0, "locked_until": 0, "created_at": now, "last_login_at": now,
             "loadtest": True}
    cats = [{"id": new_id(), "business_id": bid, "name": n, "name_key": name_key(n), "gst_percent": g,
             "created_at": now, "loadtest": True} for n, g in CATEGORIES]
    cat_by_name = {c["name"]: c for c in cats}
    prods = []
    for k in range(products):
        name, cat, price = BASE_PRODUCTS[k % len(BASE_PRODUCTS)]
        variant = VARIANTS[(k // len(BASE_PRODUCTS)) % len(VARIANTS)]
        round_no = k // (len(BASE_PRODUCTS) * len(VARIANTS))
        full = f"{name}{variant}" + (f" {round_no + 1}" if round_no else "")
        prods.append({
            "id": new_id(), "business_id": bid, "sku": f"NP-{k + 1:04d}", "name": full, "botanical_name": "",
            "category_id": cat_by_name[cat]["id"], "library_id": None, "photo_path": None,
            "selling_price": price, "cost_price": round(price * 0.45), "stock": 100000, "min_stock": 5,
            "supplier": "", "notes": "", "unit": "pcs",
            "deleted_at": None, "created_at": now, "created_by": owner["id"], "loadtest": True})
        prods[-1].update(keys_for(prods[-1], None))  # search keys, as insert_product sets them
    custs = [{"id": new_id(), "business_id": bid, "name": f"{FIRST[j % len(FIRST)]} {LAST[(j // len(FIRST)) % len(LAST)]}",
              "phone": customer_phone(i, j), "email": "", "address": "", "notes": "", "total_purchases": 0.0,
              "purchase_count": 0, "created_at": now, "loadtest": True} for j in range(customers)]
    for c in custs:  # search fields, as create_customer sets them
        c.update(search_fields(c["name"], c["phone"]))
    return business, owner, cats, prods, custs


def seed(db, shops: int, products: int, customers: int, first: int = 1, tokens: bool = False,
         log=print) -> dict:
    """Insert the shops into `db` (a blocking PyMongo database). Returns what shops.json holds."""
    import auth
    from common import now_iso

    password_hash = auth.hash_password(PASSWORD)  # once: every load-test owner has the same password
    now = now_iso()
    out = []
    started = time.monotonic()
    for i in range(first, first + shops):
        business, owner, cats, prods, custs = shop_docs(i, products, customers, password_hash, now)
        db.businesses.insert_one(business)
        db.users.insert_one(owner)
        db.categories.insert_many(cats, ordered=False)
        for chunk in range(0, len(prods), 1000):
            db.products.insert_many(prods[chunk:chunk + 1000], ordered=False)
        for chunk in range(0, len(custs), 1000):
            db.customers.insert_many(custs[chunk:chunk + 1000], ordered=False)
        row = {"phone": owner["phone"], "business_id": business["id"]}
        if tokens:
            row["token"] = auth.make_token(owner)
        out.append(row)
        if (i - first + 1) % 50 == 0:
            log(f"  {i - first + 1}/{shops} shops ({time.monotonic() - started:.0f} s)")
    return {"password": PASSWORD, "product_terms": PRODUCT_TERMS, "customer_terms": CUSTOMER_TERMS, "shops": out}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--mongo-url", default=os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27117/?replicaSet=rs0"))
    p.add_argument("--db", default="hh_loadtest")
    p.add_argument("--shops", type=int, default=50)
    p.add_argument("--products", type=int, default=300, help="per shop")
    p.add_argument("--customers", type=int, default=2000, help="per shop")
    p.add_argument("--first", type=int, default=1, help="number of the first shop (to add shops with --append)")
    p.add_argument("--out", default=str(Path(__file__).parent / "shops.json"))
    p.add_argument("--drop", action="store_true", help="delete the whole database first")
    p.add_argument("--append", action="store_true", help="add to a database that already has data and migrations")
    p.add_argument("--force", action="store_true", help="allow a database name without 'loadtest' in it")
    p.add_argument("--no-migrate", action="store_true", help="skip `migrations up` after seeding")
    a = p.parse_args()

    if "loadtest" not in a.db and not a.force:
        raise SystemExit(f"refusing to seed {a.db!r}: the name must contain 'loadtest' (or pass --force)")
    if os.environ.get("ENV", "").lower() == "production":
        raise SystemExit("refusing to seed with ENV=production")
    # the backend modules read these when they are imported
    os.environ.update(MONGO_URL=a.mongo_url, MONGO_DB=a.db, DB_NAME=a.db, ENV="dev")
    tokens = len(os.environ.get("JWT_SECRET", "")) >= 32

    from pymongo import MongoClient

    client = MongoClient(a.mongo_url, serverSelectionTimeoutMS=5000)
    db = client[a.db]
    if a.drop:
        client.drop_database(a.db)
    elif db.migrations.estimated_document_count() and not a.append:
        raise SystemExit(f"{a.db} already has data and migrations: use --drop (start again) or --append")
    print(f"seeding {a.shops} shops x {a.products} products, {a.customers} customers into {a.db}"
          f"{' (with login tokens)' if tokens else ' (no JWT_SECRET: the load test logs in instead)'}")
    data = seed(db, a.shops, a.products, a.customers, first=a.first, tokens=tokens)
    client.close()

    if not a.no_migrate:
        import db as db_module
        import migrations

        async def migrate():
            try:
                return await migrations.up(db_module.get_db())
            finally:
                await db_module.close()

        print("migrations:", ", ".join(asyncio.run(migrate())) or "all up to date")

    out = Path(a.out)
    if a.append and out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        data["shops"] = old.get("shops", []) + data["shops"]
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"wrote {out} ({len(data['shops'])} shops). It holds logins: keep it out of git (loadtest/.gitignore).")


if __name__ == "__main__":
    main()
