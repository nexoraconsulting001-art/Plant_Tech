"""Finding products as the user types: the shop's own products, then the shared library.

Products store `search_keys` (search.py), indexed with {business_id, search_keys}. A search reads at most a
few times `limit` products (exact names, then own names starting with what was typed, then other names
starting with it) and ranks those with search_score, the same ranking as when every product was scored.
`own_keys` (the product's own names only) is kept beside them, so an entry's new names can be merged in
(library.refresh_products). Product reads use FIELDS so the keys never leave the database.

Every writer of products sets the keys: routers/products.insert_product (new products, supplier bills, demo
data), update_product (renames), delete_product (none), library.refresh_products (an entry's new names), migration
0303 (all existing ones) and loadtest/seed.py. Only an older server version writes products without them (the
old version billing while a deploy migrates, or a rollback). For those, `index_unkeyed` fills a shop's products
without keys at most once every UNKEYED_EVERY seconds per shop and process, from its search (it used to run
before every search: one more query per keystroke). Such a product is found at the latest that long after the
newer version serves again.
"""
from bson.regex import Regex
from pymongo import UpdateOne

from common import Throttle, name_key
from routers import library
from search import exact_keys, prefixes, product_keys, product_terms, query_forms, score_of, term_forms

LIMIT = 30
FIELDS = {"_id": 0, "own_keys": 0, "search_keys": 0}
UNKEYED_BATCH = 500
UNKEYED_EVERY = 300  # seconds between two checks of one shop for products without keys, per process
unkeyed_checked = Throttle(UNKEYED_EVERY)


async def library_images(db, products: list[dict]) -> dict:
    ids = list({p["library_id"] for p in products if p.get("library_id")})
    if not ids:
        return {}
    return {e["id"]: e for e in await db.library.find({"id": {"$in": ids}}, library.FIELDS).to_list(len(ids))}


def keys_for(product: dict, entry: dict | None) -> dict:
    """The key fields to store on a product (none searchable once it is deleted)."""
    own, keys = product_keys(product, entry)
    return {"own_keys": own, "search_keys": [] if product.get("deleted_at") else keys}


async def with_keys(db, product: dict) -> dict:
    """The product with its key fields set, its shared entry's names included (for inserts and renames)."""
    entry = None
    if product.get("library_id"):
        entry = await db.library.find_one({"id": product["library_id"]}, library.FIELDS)
    return {**product, **keys_for(product, entry)}


async def index_unkeyed(db, business_id: str) -> int:
    """Fill the keys of this shop's products that have none yet (one indexed query; usually finds nothing)."""
    todo = await db.products.find({"business_id": business_id, "search_keys": {"$exists": False}},
                                  {"_id": 0, "id": 1, "name": 1, "botanical_name": 1, "sku": 1, "library_id": 1,
                                   "deleted_at": 1}).to_list(UNKEYED_BATCH)
    if not todo:
        return 0
    entries = await library_images(db, todo)
    await db.products.bulk_write([
        UpdateOne({"id": p["id"], "business_id": business_id, "search_keys": {"$exists": False}},
                  {"$set": keys_for(p, entries.get(p.get("library_id")))}) for p in todo], ordered=False)
    return len(todo)


async def search_products(db, products: list[dict], query: str, lib: dict | None = None) -> list[dict]:
    """These products ranked for what was typed, in English, Hindi or Hinglish; best matches first. `lib`: their
    library entries when the caller has them (library_images), else they are read here.
    search_score for each product, with the query's forms worked out once (search.score_of)."""
    if lib is None:
        lib = await library_images(db, products)
    qk, qp = query_forms(query)
    scored = [(s, p) for p in products
              if (s := score_of(qk, qp, term_forms(product_terms(p, lib.get(p.get("library_id"))))))]
    scored.sort(key=lambda sp: (-sp[0], sp[1]["name"].lower()))
    return [p for _, p in scored]


async def find_products(db, business_id: str, query: str, limit: int = LIMIT, where: dict | None = None,
                        lib: dict | None = None) -> list[dict]:
    """The shop's best `limit` products for what was typed. `where` adds filters (category, low stock, ids).
    Pass a dict as `lib` to get the library entries of the products found (for products.views, so it does not
    read them again)."""
    qk, qp = query_forms(query)
    if not qk:
        return []
    if unkeyed_checked.due(business_id):
        try:
            await index_unkeyed(db, business_id)
        except BaseException:
            unkeyed_checked.forget(business_id)  # try again on the next search
            raise
    base = {**(where or {}), "business_id": business_id, "deleted_at": None}
    found: dict[str, dict] = {}

    async def take(keys: list):
        async for p in db.products.find({**base, "search_keys": {"$in": keys}}, FIELDS).limit(limit):
            found.setdefault(p["id"], p)

    await take(exact_keys(qk, qp))
    await take([Regex("^" + p) for p in prefixes(qk, qp, own=True)])
    if len(found) < limit:
        await take([Regex("^" + p) for p in prefixes(qk, qp, own=False)])
    candidates = list(found.values())
    entries = await library_images(db, candidates)
    if lib is not None:
        lib.update(entries)
    return (await search_products(db, candidates, query, entries))[:limit]


async def suggest(db, business_id: str, q: str) -> tuple[list[dict], list[dict]]:
    """(the shop's own best 5 products, up to 5 library entries not already among them) for what was typed."""
    if len(name_key(q)) < 2:
        return [], []
    own = (await find_products(db, business_id, q))[:5]
    taken_ids = {p.get("library_id") for p in own}
    taken_names = {name_key(p["name"]) for p in own}
    lib = [e for e in await library.search(db, q, business_id, limit=8)
           if e["id"] not in taken_ids and name_key(e["name"]) not in taken_names][:5]
    return own, lib
