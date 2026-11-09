"""Look up every name of shared catalogue entries now (Hindi, Hinglish, regional, trade and botanical names), so
search and the plant scan find them in any language. The worker does this by itself every hour (library.
refresh_names, NAMES_BATCH at a time); this runs it at once. Entries already looked up at NAMES_VERSION are skipped.

On the VM: docker compose exec web python enrich_library.py --limit 50
"""
import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from db import get_db  # noqa: E402
from routers import library  # noqa: E402


async def run(limit: int) -> int:
    db = get_db()
    todo = await db.library.find({"removed": False, "$or": [{"names_version": {"$lt": library.NAMES_VERSION}},
                                                             {"names_version": {"$exists": False}}]},
                                 {"_id": 0, "id": 1, "name": 1}).to_list(limit)
    done = 0
    for e in todo:
        ok = await library.enrich(db, e["id"])
        done += ok
        print("looked up:" if ok else "no answer:", e["name"], flush=True)
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=50, help="how many entries at most (one small AI call each)")
    print("done:", asyncio.run(run(ap.parse_args().limit)), "entries", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
