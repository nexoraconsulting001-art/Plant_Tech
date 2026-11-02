"""Finding products as the user types: the shop's own products, then the shared library.

Moved from routers/products.py unchanged (WP-3 replaces the in-Python scoring with indexed search keys).
"""
from common import name_key
from routers import library
from search import product_terms, search_score


async def library_images(db, products: list[dict]) -> dict:
    ids = list({p["library_id"] for p in products if p.get("library_id")})
    if not ids:
        return {}
    return {e["id"]: e for e in await db.library.find({"id": {"$in": ids}}, {"_id": 0}).to_list(len(ids))}


async def search_products(db, products: list[dict], query: str) -> list[dict]:
    """Products matching what was typed, in English, Hindi or Hinglish; best matches first."""
    lib = await library_images(db, products)
    scored = [(s, p) for p in products if (s := search_score(query, product_terms(p, lib.get(p.get("library_id")))))]
    scored.sort(key=lambda sp: (-sp[0], sp[1]["name"].lower()))
    return [p for _, p in scored]


async def suggest(db, business_id: str, q: str) -> tuple[list[dict], list[dict]]:
    """(the shop's own best 5 products, up to 5 library entries not already among them) for what was typed."""
    if len(name_key(q)) < 2:
        return [], []
    own = await search_products(db, await db.products.find({"business_id": business_id, "deleted_at": None},
                                                           {"_id": 0}).to_list(5000), q)
    own = own[:5]
    taken_ids = {p.get("library_id") for p in own}
    taken_names = {name_key(p["name"]) for p in own}
    lib = [e for e in await library.search(db, q, business_id, limit=8)
           if e["id"] not in taken_ids and name_key(e["name"]) not in taken_names][:5]
    return own, lib
