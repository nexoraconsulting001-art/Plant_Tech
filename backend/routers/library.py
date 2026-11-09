"""Shared product picture library, used by every shop.

Each entry is one product (mostly plants) with a name, Hindi name and default category.
A product without a shared picture gets the first clear photo a shop takes of it
(status "new" until an app admin checks it). Admins can add, replace or remove pictures.
Other shops only see a picture once an admin has checked it ("live").

Entries are `public` (the starter ones and those an admin approved) or private: an entry a shop starts by
naming a new product is found only by that shop until an app admin approves it. Its name is cleaned first
(no web or e-mail addresses, no phone numbers, at most 80 characters). One live entry per name (unique
name_key): a second shop typing the same name is linked to the same entry.

Search (search.py keys): public entries are held in memory in each web process (`PublicIndex`) and reloaded
when the version stamp in `counters` ("library:version") changes. Every write that changes a public entry
calls `changed()`; other processes see it within CACHE_SECONDS. A shop's private entries are looked up in the
database ({source_business_id, search_keys} index). Products carry their entry's names in their own search
keys, so a change to an entry's names is copied to the products linked to it (`refresh_products`).
"""
import asyncio
import bisect
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from bson.regex import Regex
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

import ai_client
import auth
import jobs
import storage_client
from common import name_key, new_id, now_iso
from db import get_db
from search import (EXACT, exact_keys, keys_of, level_of, library_keys, library_terms, prefixes, query_forms,
                    score_of, term_forms)
from settings import get_settings

router = APIRouter(prefix="/api", tags=["library"])
logger = logging.getLogger(__name__)

NAME_MAX = 80  # the admin's LibraryPatch allows the same
ALIAS_MAX = 60
ALIASES_MAX = 25  # other names per entry
NAMES_VERSION = 2  # the name look-up's version (2: botanical and regional names, 10 Nov 2026); older entries are
# looked up again by refresh_names
NAMES_BATCH = 40  # entries looked up per hourly run
NAMES_TRIES = 3  # look-ups without a usable answer before an entry is left alone (a day apart)
CACHE_SECONDS = 30  # how stale another process's public entries can be
VERSION_ID = "library:version"
CANDIDATES = 300  # public entries ranked per search at most (exact matches are always among them)
PRIVATE_LIMIT = 30
FIELDS = {"_id": 0, "search_keys": 0}

# The plants most Indian nurseries sell, with their Hindi name, botanical name and other common names (English,
# Hinglish, older botanical names). Migration 1102 adds the ones a database lacks and merges new names into the
# ones it has; the hourly name look-up (refresh_names) then adds what Claude knows.
STARTER = [
    ("Money Plant", "मनी प्लांट", "Epipremnum aureum", "Indoor Plants", ["pothos", "golden pothos", "devil's ivy"]),
    ("Snake Plant", "स्नेक प्लांट", "Dracaena trifasciata", "Indoor Plants",
     ["sansevieria", "sansevieria trifasciata", "mother-in-law's tongue"]),
    ("Areca Palm", "सुपारी पाम", "Dypsis lutescens", "Indoor Plants", ["butterfly palm", "golden cane palm", "areca"]),
    ("Peace Lily", "पीस लिली", "Spathiphyllum wallisii", "Indoor Plants", ["spathiphyllum", "white sail plant"]),
    ("Jade Plant", "जेड प्लांट", "Crassula ovata", "Succulents", ["crassula", "money tree", "kuber plant", "kubera plant"]),
    ("Aloe Vera", "एलोवेरा", "Aloe vera", "Succulents", ["aloe", "ghritkumari", "gwarpatha", "ghee kunwar"]),
    ("Tulsi", "तुलसी", "Ocimum tenuiflorum", "Herbs",
     ["holy basil", "tulasi", "ocimum sanctum", "rama tulsi", "krishna tulsi"]),
    ("Rose", "गुलाब", "Rosa", "Flowering Plants", ["gulab", "rose plant"]),
    ("Hibiscus", "गुड़हल", "Hibiscus rosa-sinensis", "Flowering Plants", ["gudhal", "china rose", "jaswand", "jasud"]),
    ("Spider Plant", "स्पाइडर प्लांट", "Chlorophytum comosum", "Indoor Plants", ["chlorophytum", "airplane plant"]),
    ("Marigold", "गेंदा", "Tagetes erecta", "Flowering Plants",
     ["genda", "gainda", "genda phool", "african marigold", "tagetes"]),
    ("Mogra", "मोगरा", "Jasminum sambac", "Flowering Plants", ["arabian jasmine", "motia", "bela", "mogra jasmine"]),
    ("Chameli", "चमेली", "Jasminum grandiflorum", "Flowering Plants", ["spanish jasmine", "royal jasmine", "jai"]),
    ("Raat Ki Rani", "रात की रानी", "Cestrum nocturnum", "Flowering Plants",
     ["raat rani", "night blooming jasmine", "lady of the night"]),
    ("Parijat", "पारिजात", "Nyctanthes arbor-tristis", "Flowering Plants",
     ["harsingar", "night jasmine", "shiuli", "coral jasmine"]),
    ("Champa", "चंपा", "Plumeria rubra", "Flowering Plants", ["plumeria", "frangipani", "temple tree", "champa phool"]),
    ("Kaner", "कनेर", "Nerium oleander", "Shrubs", ["oleander", "kaner phool"]),
    ("Bougainvillea", "बोगनविलिया", "Bougainvillea glabra", "Flowering Plants",
     ["kagaz phool", "paper flower", "booganvel"]),
    ("Adenium", "एडेनियम", "Adenium obesum", "Succulents", ["desert rose"]),
    ("Ixora", "इक्सोरा", "Ixora coccinea", "Flowering Plants", ["rugmini", "jungle geranium", "flame of the woods"]),
    ("Croton", "क्रोटन", "Codiaeum variegatum", "Outdoor Plants", ["garden croton"]),
    ("Aparajita", "अपराजिता", "Clitoria ternatea", "Flowering Plants", ["butterfly pea", "blue pea", "gokarna"]),
    ("Curry Leaf", "कड़ी पत्ता", "Murraya koenigii", "Herbs", ["kadi patta", "curry patta", "meetha neem", "kari patta"]),
    ("Mint", "पुदीना", "Mentha spicata", "Herbs", ["pudina", "spearmint"]),
    ("Neem", "नीम", "Azadirachta indica", "Trees", ["neem tree", "margosa"]),
    ("Ashoka Tree", "अशोक", "Polyalthia longifolia", "Trees", ["ashoka", "false ashoka", "mast tree", "asopalav"]),
    ("Lemon Grass", "लेमन ग्रास", "Cymbopogon citratus", "Herbs", ["lemongrass", "hari chai", "gavati chaha"]),
    ("Ajwain Plant", "अजवाइन", "Coleus amboinicus", "Herbs",
     ["indian borage", "patharchur", "karpuravalli", "plectranthus amboinicus"]),
    ("Stevia", "स्टीविया", "Stevia rebaudiana", "Herbs", ["meethi tulsi", "sweet leaf"]),
    ("Giloy", "गिलोय", "Tinospora cordifolia", "Herbs", ["guduchi", "amrita"]),
    ("Ashwagandha", "अश्वगंधा", "Withania somnifera", "Herbs", ["indian ginseng"]),
    ("Brahmi", "ब्राह्मी", "Bacopa monnieri", "Herbs", ["bacopa", "water hyssop"]),
    ("Insulin Plant", "इंसुलिन प्लांट", "Costus igneus", "Herbs", ["costus", "fiery costus"]),
    ("Lemon", "नींबू", "Citrus limon", "Trees", ["nimbu", "lemon plant"]),
    ("Guava", "अमरूद", "Psidium guajava", "Trees", ["amrood", "amrud", "peru"]),
    ("Mango", "आम", "Mangifera indica", "Trees", ["aam", "mango plant"]),
    ("Pomegranate", "अनार", "Punica granatum", "Trees", ["anar"]),
    ("Papaya", "पपीता", "Carica papaya", "Trees", ["papita"]),
    ("Chiku", "चीकू", "Manilkara zapota", "Trees", ["chikoo", "sapota", "sapodilla"]),
    ("Jamun", "जामुन", "Syzygium cumini", "Trees", ["java plum", "black plum", "jambul"]),
    ("Amla", "आंवला", "Phyllanthus emblica", "Trees", ["indian gooseberry", "aonla"]),
    ("Bel", "बेल", "Aegle marmelos", "Trees", ["bael", "bilva", "wood apple"]),
    ("Peepal", "पीपल", "Ficus religiosa", "Trees", ["pipal", "sacred fig", "bodhi tree"]),
    ("Banyan", "बरगद", "Ficus benghalensis", "Trees", ["bargad", "vat vriksh"]),
    ("Lucky Bamboo", "लकी बैम्बू", "Dracaena sanderiana", "Indoor Plants", ["bamboo plant", "lucky bamboo plant"]),
    ("Rubber Plant", "रबर प्लांट", "Ficus elastica", "Indoor Plants", ["rubber tree", "india rubber plant"]),
    ("ZZ Plant", "ज़ेड ज़ेड प्लांट", "Zamioculcas zamiifolia", "Indoor Plants", ["zamioculcas", "zanzibar gem"]),
    ("Syngonium", "सिंगोनियम", "Syngonium podophyllum", "Indoor Plants", ["arrowhead plant", "arrowhead vine"]),
    ("Fiddle Leaf Fig", "फिडल लीफ फिग", "Ficus lyrata", "Indoor Plants", ["fiddle leaf"]),
    ("Monstera", "मॉन्स्टेरा", "Monstera deliciosa", "Indoor Plants", ["swiss cheese plant", "split leaf philodendron"]),
    ("Dracaena Marginata", "ड्रेसीना", "Dracaena marginata", "Indoor Plants",
     ["dragon tree", "madagascar dragon tree", "dracaena"]),
    ("Aglaonema", "एग्लोनिमा", "Aglaonema commutatum", "Indoor Plants", ["chinese evergreen"]),
    ("Boston Fern", "बोस्टन फर्न", "Nephrolepis exaltata", "Indoor Plants", ["fern", "sword fern"]),
    ("Ficus Benjamina", "फाइकस", "Ficus benjamina", "Indoor Plants", ["weeping fig", "ficus", "benjamin fig"]),
    ("Lady Palm", "लेडी पाम", "Rhapis excelsa", "Indoor Plants", ["rhapis palm", "rhapis"]),
    ("Christmas Tree", "क्रिसमस ट्री", "Araucaria heterophylla", "Outdoor Plants", ["araucaria", "norfolk island pine"]),
    ("Thuja", "मोरपंखी", "Platycladus orientalis", "Outdoor Plants", ["morpankhi", "golden cypress", "oriental arborvitae"]),
    ("Kamini", "कामिनी", "Murraya paniculata", "Shrubs", ["orange jasmine", "mock orange"]),
    ("Duranta", "डुरंटा", "Duranta erecta", "Shrubs", ["golden duranta", "sky flower"]),
    ("Lantana", "लैंटाना", "Lantana camara", "Shrubs", ["raimuniya"]),
    ("Tecoma", "टिकोमा", "Tecoma stans", "Flowering Plants", ["yellow bells", "yellow trumpet bush"]),
    ("Vinca", "सदाबहार", "Catharanthus roseus", "Flowering Plants", ["sadabahar", "periwinkle", "nayantara"]),
    ("Portulaca", "पोर्टुलाका", "Portulaca grandiflora", "Flowering Plants",
     ["moss rose", "nau baje ka phool", "table rose"]),
    ("Chrysanthemum", "गुलदाउदी", "Chrysanthemum morifolium", "Flowering Plants", ["guldaudi", "guldawari", "shevanti"]),
    ("Petunia", "पेटूनिया", "Petunia hybrida", "Flowering Plants", []),
    ("Dahlia", "डहलिया", "Dahlia pinnata", "Flowering Plants", []),
    ("Sunflower", "सूरजमुखी", "Helianthus annuus", "Flowering Plants", ["surajmukhi"]),
    ("Cactus", "कैक्टस", "Cactaceae", "Cacti", ["cacti", "nagphani"]),
    ("Echeveria", "एचेवेरिया", "Echeveria elegans", "Succulents", []),
    ("Haworthia", "हॉवर्थिया", "Haworthiopsis attenuata", "Succulents", ["zebra plant", "zebra haworthia"]),
]


async def seed_library(db):
    """Add the starter entries that aren't there yet (safe to run again; an entry an admin removed stays removed)."""
    added = 0
    for name, hi, botanical, category, aliases in STARTER:
        entry = {"id": new_id(), "name": name, "name_hi": hi, "botanical": botanical, "default_category": category,
                 "aliases": aliases, "image_path": None, "image_status": "none", "image_source_business_id": None,
                 "source": "starter", "public": True, "removed": False, "created_at": now_iso(),
                 "updated_at": now_iso()}
        entry["search_keys"] = library_keys(entry)
        res = await db.library.update_one({"name_key": name_key(name)}, {"$setOnInsert": entry}, upsert=True)
        added += res.upserted_id is not None
    if added:
        await changed(db)


# ---- names a shop typed --------------------------------------------------------------------------

_EMAIL = re.compile(r"\S+@\S+")
_URL = re.compile(r"(?i)(?:https?://|www\.)\S*"
                  r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|in|co|io|app|info|biz|shop|store|online|site|xyz|me|ly|link)\b\S*")
_DIGITS = re.compile(r"\+?\d[\d\s().-]*\d")


def clean_name(text: str | None, limit: int = NAME_MAX) -> str:
    """A shop-typed name made fit to share: no web or e-mail address, no phone number (7+ digits), at most
    `limit` characters (cut at a word)."""
    s = _URL.sub(" ", _EMAIL.sub(" ", text or ""))
    s = _DIGITS.sub(lambda m: " " if sum(c.isdigit() for c in m.group()) >= 7 else m.group(), s)
    s = re.sub(r"\s+", " ", s).strip(" -,.;:/|")
    if len(s) > limit:
        s = s[:limit + 1].rsplit(" ", 1)[0][:limit].rstrip(" -,.;:/|")
    return s


def is_public(entry: dict) -> bool:
    return entry.get("public", not entry.get("hidden"))  # no flag yet (before migration 0301): as it was


def picture_for(entry: dict, business_id: str | None) -> str | None:
    """The shared picture this shop may see: checked ones, or the one its own photo filled. None = admin."""
    if entry.get("removed") or not entry.get("image_path"):
        return None
    if business_id is None or entry.get("image_status") == "live" or entry.get("image_source_business_id") == business_id:
        return entry["image_path"]
    return None


def view(entry: dict, business_id: str | None = None) -> dict:
    v = {k: entry.get(k) for k in ("id", "name", "name_hi", "botanical", "default_category", "image_status", "source")}
    v["image_path"] = picture_for(entry, business_id)
    if v["image_path"] is None and business_id is not None:
        v["image_status"] = "none"
    return v


def visible_to(entry: dict, business_id: str) -> bool:
    return not entry.get("removed") and (is_public(entry) or entry.get("source_business_id") == business_id)


# ---- the public entries, in memory ---------------------------------------------------------------

class PublicIndex:
    """One database's public entries with their search keys sorted, for prefix look-ups without a query."""

    def __init__(self, db=None):
        self.db = db
        self.version: str | None = None
        self.loaded = False
        self.checked = 0.0
        self.lock = asyncio.Lock()
        self.fill([])

    def fill(self, entries: list[dict]):
        self.entries = {e["id"]: e for e in entries}
        self.forms = {e["id"]: term_forms(library_terms(e)) for e in entries}
        pairs = sorted((k, e["id"]) for e in entries for k in (e.get("search_keys") or library_keys(e)))
        self.keys = [k for k, _ in pairs]
        self.owners = [i for _, i in pairs]

    def starting(self, prefix: str, into: dict, cap: int):
        """Add the ids of entries with a key starting with `prefix` to `into`, until it holds `cap`."""
        i = bisect.bisect_left(self.keys, prefix)
        while i < len(self.keys) and len(into) < cap and self.keys[i].startswith(prefix):
            into.setdefault(self.owners[i])
            i += 1

    def equal(self, key: str) -> list[str]:
        i = bisect.bisect_left(self.keys, key)
        found = []
        while i < len(self.keys) and self.keys[i] == key:
            found.append(self.owners[i])
            i += 1
        return found


_index = PublicIndex()


async def public_index(db) -> PublicIndex:
    """The public entries, reloaded when another process (or this one) changed them."""
    global _index
    if _index.db is not db:  # another database (tests): start again
        _index = PublicIndex(db)
    idx = _index
    if idx.loaded and time.monotonic() - idx.checked < CACHE_SECONDS:
        return idx
    async with idx.lock:
        if idx.loaded and time.monotonic() - idx.checked < CACHE_SECONDS:
            return idx
        stamp = await db.counters.find_one({"_id": VERSION_ID})  # read before the entries: never older than them
        version = (stamp or {}).get("v")
        if not (idx.loaded and version is not None and version == idx.version):
            idx.fill(await db.library.find({"public": True, "removed": False}, {"_id": 0}).to_list(None))
            idx.version, idx.loaded = version, True
        idx.checked = time.monotonic() if version is not None else 0.0  # no stamp yet: do not trust the copy
    return idx


async def changed(db):
    """Call after a write that changes a public entry, or makes one public or private: every process reloads."""
    await db.counters.update_one({"_id": VERSION_ID}, {"$set": {"v": uuid.uuid4().hex}}, upsert=True)
    if _index.db is db:
        _index.loaded = False


def _patterns(qk: str, qp: str) -> list[Regex]:
    """Anchored prefixes for the index (keys hold only letters, digits, spaces and the tag, nothing to escape)."""
    return [Regex("^" + p) for own in (True, False) for p in prefixes(qk, qp, own)]


async def search(db, q: str, business_id: str, limit: int = 5) -> list[dict]:
    """Entries this shop can see matching what was typed (English, Hindi or Hinglish name, or another name)."""
    qk, qp = query_forms(q)
    if len(qk) < 2:
        return []
    idx = await public_index(db)
    ids: dict[str, None] = dict.fromkeys(i for k in exact_keys(qk, qp) for i in idx.equal(k))
    for own in (True, False):
        for p in prefixes(qk, qp, own):
            idx.starting(p, ids, CANDIDATES)
    scored = [(score_of(qk, qp, idx.forms[i]), idx.entries[i]) for i in ids]
    async for e in db.library.find({"source_business_id": business_id, "public": False, "removed": False,
                                    "search_keys": {"$in": _patterns(qk, qp)}}, FIELDS).limit(PRIVATE_LIMIT):
        scored.append((score_of(qk, qp, term_forms(library_terms(e))), e))
    scored = [se for se in scored if se[0]]
    scored.sort(key=lambda se: (-se[0], se[1]["name"]))
    return [view(e, business_id) for _, e in scored[:limit]]


async def _same_plant(db, name: str, business_id: str) -> dict | None:
    """An entry this shop can see that has exactly this name in another language or spelling."""
    qk, qp = query_forms(name)
    keys = exact_keys(qk, qp)
    idx = await public_index(db)
    found = sorted((idx.entries[i] for i in dict.fromkeys(i for k in keys for i in idx.equal(k))),
                   key=lambda e: e["name"])
    found += await db.library.find({"source_business_id": business_id, "public": False, "removed": False,
                                    "search_keys": {"$in": keys}}, FIELDS).limit(10).to_list(10)
    for e in found:
        if any(level_of(qk, qp, tk, tp) == EXACT for tk, tp in term_forms(library_terms(e))):
            return e
    return None


async def entry_for_new_product(db, name: str, category: str | None, business_id: str,
                                botanical: str = "") -> str | None:
    """Find the shared entry for a product name, or start one (private to this shop until an admin approves it)."""
    clean = clean_name(name)
    key = name_key(clean)
    if len(key) < 3:
        return None
    existing = await db.library.find_one({"name_key": key, "removed": False}, {"_id": 0, "id": 1})
    if not existing:  # the same plant under its Hindi or another common name ("गुलाब" is Rose)
        existing = await _same_plant(db, clean, business_id)
    if not existing and len(name_key(botanical)) >= 3:  # or under its botanical name (a scan's "Ocimum sanctum")
        existing = await _same_plant(db, clean_name(botanical, ALIAS_MAX), business_id)
    if existing:
        return existing["id"]
    entry = {"id": new_id(), "name": clean, "name_key": key, "name_hi": "", "botanical": "",
             "default_category": category or "", "aliases": [], "image_path": None, "image_status": "none",
             "image_source_business_id": None, "source": "shop", "source_business_id": business_id,
             "public": False, "removed": False, "created_at": now_iso(), "updated_at": now_iso()}
    entry["search_keys"] = library_keys(entry)
    try:
        await db.library.insert_one(entry)
    except DuplicateKeyError:  # another shop started the same name a moment ago
        existing = await db.library.find_one({"name_key": key, "removed": False}, {"_id": 0, "id": 1})
        return existing["id"] if existing else None
    if get_settings().ai_enrich_names:
        try:
            await jobs.enqueue("look_up_names", entry["id"], _job_id=f"look_up_names:{entry['id']}")
        except Exception:  # the job queue is down: the product is saved anyway; enrich_library.py fills the names in
            logger.warning("could not queue the name look-up for %s", entry["name"], exc_info=True)
    return entry["id"]


@jobs.job
async def look_up_names(ctx, entry_id: str):
    """Background job for a new shop-made entry (see enrich). Safe to run twice: done entries are skipped."""
    await enrich(ctx["db"], entry_id)


def _names_version(entry: dict) -> int:
    return entry.get("names_version", 1 if entry.get("enriched_at") else 0)


async def enrich(db, entry_id: str) -> bool:
    """Look up every name of an entry with Claude (ai_client.name_terms): its Hindi name, Hinglish and regional
    spellings, other common and trade names, and its botanical names, so search finds it in any language and the
    plant scan's stock list carries them. A shop-made entry that is not a nursery item is marked hidden (a hint
    for the admin; the AI never makes an entry public), and one that turns out to be a plant another entry already
    has (the same botanical name) is merged into it. Not counted as a shop's scan. True when it was looked up."""
    entry = await db.library.find_one({"id": entry_id}, {"_id": 0})
    if not entry or entry.get("removed") or _names_version(entry) >= NAMES_VERSION:
        return False
    try:
        terms = await ai_client.name_terms(entry["name"], entry.get("default_category") or "",
                                           entry.get("botanical") or "")
    except Exception as e:  # no names today; refresh_names tries again a day later (NAMES_TRIES in all)
        logger.warning("name look-up failed for %s: %s", entry["name"], e)
        terms = None
    if terms is None:
        await db.library.update_one({"id": entry_id}, {"$set": {"names_tried_at": now_iso()},
                                                       "$inc": {"names_tries": 1}})
        return False
    botanical = clean_name(terms.botanical_name, ALIAS_MAX)
    extra = [clean_name(a, ALIAS_MAX) for a in [terms.english_name, *terms.hinglish, *terms.aliases,
                                                 *terms.botanical_synonyms]]
    if entry.get("botanical") and name_key(botanical) != name_key(entry["botanical"]):
        extra.append(botanical)  # the accepted name next to an older one the entry has
    own = {entry["name_key"], name_key(entry.get("botanical") or botanical)}
    aliases = list(dict.fromkeys(a for a in [*entry.get("aliases", []), *extra]
                                 if len(name_key(a)) >= 2 and name_key(a) not in own))[:ALIASES_MAX]
    update = {"aliases": aliases, "enriched_at": now_iso(), "names_version": NAMES_VERSION}
    if entry.get("source") == "shop":
        update["hidden"] = not terms.is_nursery_item
    if not entry.get("botanical") and botanical:
        update["botanical"] = botanical
    hindi = clean_name(terms.hindi_name)
    if not entry.get("name_hi") and hindi:
        update["name_hi"] = hindi
    was_public = is_public(entry)
    if update.get("hidden") and was_public and not entry.get("approved_at"):
        update["public"] = False  # an older shop entry that is not a nursery item: back to its own shop only
    update["search_keys"] = library_keys({**entry, **update})
    await db.library.update_one({"id": entry_id}, {"$set": update})
    merged_into = None if update.get("hidden") else await _merge_same_plant(db, {**entry, **update})
    await refresh_products(db, merged_into or entry_id)
    if was_public or merged_into:
        await changed(db)
    return True


async def _merge_same_plant(db, entry: dict) -> str | None:
    """A shop-made entry with the same botanical name as another entry the shop can see ("Holy Basil Plant" and the
    starter "Tulsi", both Ocimum tenuiflorum) joins it: its products move to that entry (so they carry its names
    and picture) and it is marked removed (`merged_into`), as migration 0301 merges same-named entries. The one
    kept: a starter entry, else a public one, else the older one. Returns the kept entry's id, or None."""
    botanical = name_key(entry.get("botanical") or "")
    if entry.get("source") != "shop" or len(botanical) < 3:
        return None
    pattern = Regex("^" + re.escape(entry["botanical"].strip()) + "$", "i")
    rank = {"starter": 0}
    others = [e for e in await db.library.find(
        {"botanical": pattern, "removed": False, "id": {"$ne": entry["id"]}}, FIELDS).to_list(20)
        if name_key(e.get("botanical") or "") == botanical
        and (is_public(e) or e.get("source_business_id") == entry.get("source_business_id"))]
    if not others:
        return None
    keep = min(others, key=lambda e: (rank.get(e.get("source"), 1), not is_public(e), e.get("created_at") or ""))
    await db.products.update_many({"library_id": entry["id"]}, {"$set": {"library_id": keep["id"]}})
    await db.library.update_one({"id": entry["id"]}, {"$set": {"removed": True, "merged_into": keep["id"],
                                                               "updated_at": now_iso()}})
    logger.info("library: %r merged into %r (both %s)", entry["name"], keep["name"], entry["botanical"])
    return keep["id"]


async def refresh_names(ctx):
    """Hourly (worker.py): look up the names of up to NAMES_BATCH entries whose names are older than NAMES_VERSION,
    starter and public ones first. Stops when AI_ENRICH_NAMES is off or the month's AI budget is reached."""
    import ai_usage

    if not get_settings().ai_enrich_names:
        return 0
    db = ctx["db"] if ctx and ctx.get("db") is not None else get_db()
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old = {"removed": False, "names_tries": {"$not": {"$gte": NAMES_TRIES}},
           "$and": [{"$or": [{"names_version": {"$lt": NAMES_VERSION}}, {"names_version": {"$exists": False}}]},
                    {"$or": [{"names_tried_at": {"$exists": False}}, {"names_tried_at": {"$lt": day_ago}}]}]}
    todo = await db.library.find({**old, "public": True}, {"_id": 0, "id": 1}).sort("created_at", 1).to_list(NAMES_BATCH)
    if len(todo) < NAMES_BATCH:
        todo += await db.library.find({**old, "public": {"$ne": True}}, {"_id": 0, "id": 1}).sort(
            "created_at", 1).to_list(NAMES_BATCH - len(todo))
    done = 0
    for e in todo:
        if await ai_usage.over_budget(db):
            break
        done += await enrich(db, e["id"])
    if done:
        logger.info("refresh_names: looked up %s entries", done)
    return done


async def refresh_products(db, entry_id: str):
    """Products linked to this entry are found by its names too: copy its current names into their search keys
    (one update for all shops; products not indexed yet get theirs on the next search, catalog_search.py)."""
    entry = await db.library.find_one({"id": entry_id}, FIELDS)
    keys = keys_of(library_terms(entry), first_is_own=False)
    await db.products.update_many(
        {"library_id": entry_id, "deleted_at": None, "own_keys": {"$exists": True}},
        [{"$set": {"search_keys": {"$setUnion": ["$own_keys", {"$literal": keys}]}}}])


async def _store_library_picture(db, entry_id: str, jpeg: bytes) -> str:
    path = f"{storage_client.APP_NAME}/library/{entry_id}-{new_id()[:8]}.jpg"
    await run_in_threadpool(storage_client.put_object, path, jpeg, "image/jpeg")
    await db.uploads.insert_one({"path": path, "business_id": None, "created_at": now_iso()})
    return path


async def contribute_photo(db, library_id: str | None, photo_path: str | None, business_id: str):
    """A shop's product photo becomes the shared picture if the entry has none yet."""
    if not library_id or not photo_path:
        return
    entry = await db.library.find_one({"id": library_id, "removed": False}, FIELDS)
    if not entry or entry.get("image_status") != "none":
        return
    try:
        jpeg, _ = await run_in_threadpool(storage_client.get_object, photo_path)
        square = await run_in_threadpool(ai_client.square_thumbnail, jpeg)
    except Exception:  # a missing or unreadable photo just means no contribution
        return
    path = await _store_library_picture(db, library_id, square)
    res = await db.library.update_one({"id": library_id, "image_status": "none"}, {"$set": {
        "image_path": path, "image_status": "new", "image_source_business_id": business_id,
        "updated_at": now_iso()}})
    if res.modified_count and is_public(entry):
        await changed(db)


# ---- admin -------------------------------------------------------------------

class LibraryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    name_hi: str | None = Field(None, max_length=80)
    botanical: str | None = Field(None, max_length=120)
    default_category: str | None = Field(None, max_length=60)


class PhotoIn(BaseModel):
    image_base64: str


async def _entry_view(db, entry_id: str) -> dict:
    entry = await db.library.find_one({"id": entry_id}, FIELDS)
    if not entry:
        raise HTTPException(404, "Not found.")
    return view(entry)


@router.get("/admin/library")
async def admin_list(status: str = "all", visibility: str = "all", admin: dict = Depends(auth.require("admin"))):
    """`visibility` = private: shop-made entries waiting for approval; public: those every shop can find."""
    q: dict = {"removed": False}
    if status in ("none", "new", "live"):
        q["image_status"] = status
    if visibility in ("public", "private"):
        q["public"] = visibility == "public"
    entries = await get_db().library.find(q, FIELDS).sort("name", 1).to_list(2000)
    return [{**view(e), "image_source_business_id": e.get("image_source_business_id"), "public": is_public(e),
             "hidden": bool(e.get("hidden"))} for e in entries]


@router.patch("/admin/library/{entry_id}")
async def admin_update(entry_id: str, body: LibraryPatch, admin: dict = Depends(auth.require("admin"))):
    db = get_db()
    update = {k: v.strip() for k, v in body.model_dump(exclude_none=True).items()}
    if "name" in update:
        update["name_key"] = name_key(update["name"])
    entry = await db.library.find_one({"id": entry_id}, FIELDS)
    if not entry:
        raise HTTPException(404, "Not found.")
    update["search_keys"] = library_keys({**entry, **update})
    update["updated_at"] = now_iso()
    try:
        await db.library.update_one({"id": entry_id}, {"$set": update})
    except DuplicateKeyError:
        raise HTTPException(409, "Another catalogue item already has this name.")
    await refresh_products(db, entry_id)
    await changed(db)
    return await _entry_view(db, entry_id)


@router.post("/admin/library/{entry_id}/photo")
async def admin_set_photo(entry_id: str, body: PhotoIn, admin: dict = Depends(auth.require("admin"))):
    db = get_db()
    if not await db.library.find_one({"id": entry_id}):
        raise HTTPException(404, "Not found.")
    try:
        jpeg = await run_in_threadpool(ai_client.normalize_photo_b64, body.image_base64)
        square = await run_in_threadpool(ai_client.square_thumbnail, jpeg)
    except ai_client.BadImage as e:
        raise HTTPException(400, str(e))
    path = await _store_library_picture(db, entry_id, square)
    await db.library.update_one({"id": entry_id}, {"$set": {
        "image_path": path, "image_status": "live", "image_source_business_id": None, "updated_at": now_iso()}})
    await changed(db)
    return await _entry_view(db, entry_id)


async def _publish(db, entry_id: str, admin: dict):
    await db.library.update_one({"id": entry_id, "public": {"$ne": True}}, {"$set": {
        "public": True, "approved_at": now_iso(), "approved_by": admin["id"], "updated_at": now_iso()}})


@router.post("/admin/library/{entry_id}/approve")
async def admin_approve(entry_id: str, admin: dict = Depends(auth.require("admin"))):
    """The admin checked the entry: its new picture goes live, and a shop-made entry becomes public."""
    db = get_db()
    await db.library.update_one({"id": entry_id, "image_status": "new"},
                                {"$set": {"image_status": "live", "updated_at": now_iso()}})
    await _publish(db, entry_id, admin)
    await changed(db)
    return await _entry_view(db, entry_id)


@router.post("/admin/library/{entry_id}/publish")
async def admin_publish(entry_id: str, admin: dict = Depends(auth.require("admin"))):
    """Every shop can find this entry (its picture still needs its own check)."""
    db = get_db()
    await _publish(db, entry_id, admin)
    await changed(db)
    return await _entry_view(db, entry_id)


@router.post("/admin/library/{entry_id}/unpublish")
async def admin_unpublish(entry_id: str, admin: dict = Depends(auth.require("admin"))):
    """Back to the shop that made it (products already linked to it keep working)."""
    db = get_db()
    await db.library.update_one({"id": entry_id}, {"$set": {"public": False, "updated_at": now_iso()},
                                                   "$unset": {"approved_at": "", "approved_by": ""}})
    await changed(db)
    return await _entry_view(db, entry_id)


@router.delete("/admin/library/{entry_id}/photo")
async def admin_remove_photo(entry_id: str, admin: dict = Depends(auth.require("admin"))):
    """Take the picture down; the next shop photo can fill it again."""
    db = get_db()
    await db.library.update_one({"id": entry_id}, {"$set": {
        "image_path": None, "image_status": "none", "image_source_business_id": None, "updated_at": now_iso()}})
    await changed(db)
    return await _entry_view(db, entry_id)


@router.delete("/admin/library/{entry_id}")
async def admin_remove_entry(entry_id: str, admin: dict = Depends(auth.require("admin"))):
    db = get_db()
    await db.library.update_one({"id": entry_id}, {"$set": {"removed": True, "updated_at": now_iso()}})
    await refresh_products(db, entry_id)
    await changed(db)
    return {"ok": True}
