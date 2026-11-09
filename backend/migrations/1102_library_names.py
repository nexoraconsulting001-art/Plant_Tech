"""The bigger starter list of plants (routers/library.py STARTER, 70 plants with Hindi, botanical and other names):
the ones this database lacks are added, and a starter entry it already has gets the new names (its aliases grow,
an empty Hindi or botanical name is filled; nothing an admin wrote is replaced). Products linked to those entries
get the names in their search keys. Claude's names come on top from the worker's hourly look-up (refresh_names)."""
from common import name_key, now_iso
from routers import library
from search import library_keys


async def up(db):
    await library.seed_library(db)
    touched = []
    for name, hi, botanical, _category, aliases in library.STARTER:
        e = await db.library.find_one({"name_key": name_key(name), "removed": False, "source": "starter"},
                                      library.FIELDS)
        if not e:
            continue
        own = {name_key(name), name_key(e.get("botanical") or botanical)}
        merged = list(dict.fromkeys(a for a in [*e.get("aliases", []), *aliases]
                                    if name_key(a) and name_key(a) not in own))[:library.ALIASES_MAX]
        update = {"aliases": merged}
        if not e.get("name_hi"):
            update["name_hi"] = hi
        if not e.get("botanical"):
            update["botanical"] = botanical
        if all(e.get(k) == v for k, v in update.items()):
            continue
        update["search_keys"] = library_keys({**e, **update})
        update["updated_at"] = now_iso()
        await db.library.update_one({"id": e["id"]}, {"$set": update})
        touched.append(e["id"])
    for entry_id in touched:
        await library.refresh_products(db, entry_id)
    if touched:
        await library.changed(db)
