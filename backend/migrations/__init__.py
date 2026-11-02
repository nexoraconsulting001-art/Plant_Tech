"""Database migrations: numbered files in this folder, applied in order, each one once.

    python -m migrations up       apply the ones not applied yet (from backend/; the deploy runs it first)
    python -m migrations status   list them, applied or pending

A migration is a file `NNNN_short_name.py` with `async def up(db)` and a docstring saying what it does. The
runner finds the files by name, so there is no list to edit. Numbers per work package (docs/PRODUCTION-PLAN.md):
00xx platform, 01xx sales, 02xx reports, 03xx catalogue, 04xx customers and suppliers, 06xx AI, 07xx files,
08xx auth, 09xx GST, 10xx retention. Two files with the same number stop the runner.

- Applied migrations are recorded in the `migrations` collection (_id = the file name without .py).
- A lock document (`migration_lock`) lets only one runner work at a time; a second one waits for it, and a
  lock left by a runner that died is taken over once it expires.
- A migration must be idempotent: if a runner stops half-way, the next run repeats it from the start.
- Migrations run without the 10 s per-operation limit the app uses (index builds on big collections are slow).
"""
import asyncio
import importlib
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import pymongo
from pymongo.errors import DuplicateKeyError

from settings import get_settings

log = logging.getLogger("migrations")

NAME = re.compile(r"^(\d{4})_[a-z0-9_]+$")
LOCK_ID = "lock"
LOCK_TTL = timedelta(minutes=15)  # renewed after each migration; a dead runner's lock is free after this
STEP_TIMEOUT = 3600  # seconds one migration may take


@dataclass(frozen=True)
class Migration:
    id: str
    number: int
    up: Callable[..., Awaitable]
    about: str


class LockBusy(RuntimeError):
    pass


def discover() -> list[Migration]:
    """Every migration file in this folder, in number order."""
    stems = sorted(p.stem for p in Path(__file__).parent.glob("*.py") if NAME.match(p.stem))
    found, numbers = [], {}
    for stem in stems:
        number = int(stem[:4])
        if number in numbers:
            raise RuntimeError(f"two migrations numbered {number:04d}: {numbers[number]} and {stem}")
        numbers[number] = stem
        module = importlib.import_module(f"{__name__}.{stem}")
        if not callable(getattr(module, "up", None)):
            raise RuntimeError(f"migration {stem} has no up(db)")
        doc = (module.__doc__ or "").strip()
        found.append(Migration(stem, number, module.up, doc.splitlines()[0] if doc else ""))
    return found


async def applied(db) -> dict[str, dict]:
    return {d["_id"]: d async for d in db.migrations.find({})}


async def status(db) -> list[dict]:
    done = await applied(db)
    return [{"id": m.id, "about": m.about, "applied_at": (done.get(m.id) or {}).get("applied_at")}
            for m in discover()]


async def _acquire(db, owner: str, wait: float):
    deadline = time.monotonic() + wait
    while True:
        now = datetime.now(timezone.utc)
        lock = {"owner": owner, "at": now, "expires_at": now + LOCK_TTL}
        try:
            await db.migration_lock.insert_one({"_id": LOCK_ID, **lock})
            return
        except DuplicateKeyError:
            stale = await db.migration_lock.find_one_and_update(
                {"_id": LOCK_ID, "expires_at": {"$lt": now}}, {"$set": lock})
            if stale:
                log.warning("took over an expired migration lock held by %s", stale.get("owner"))
                return
        if time.monotonic() >= deadline:
            holder = await db.migration_lock.find_one({"_id": LOCK_ID}) or {}
            raise LockBusy(f"another migration runner is working ({holder.get('owner', '?')})")
        await asyncio.sleep(1)


async def _renew(db, owner: str):
    now = datetime.now(timezone.utc)
    await db.migration_lock.update_one({"_id": LOCK_ID, "owner": owner}, {"$set": {"expires_at": now + LOCK_TTL}})


async def _release(db, owner: str):
    await db.migration_lock.delete_one({"_id": LOCK_ID, "owner": owner})


async def up(db, *, wait: float = 300) -> list[str]:
    """Apply every pending migration in order, under the lock. Returns the ids applied (none: already done)."""
    migrations = discover()
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    with pymongo.timeout(STEP_TIMEOUT):
        await _acquire(db, owner, wait)
    ran = []
    try:
        for m in migrations:
            with pymongo.timeout(STEP_TIMEOUT):
                if await db.migrations.find_one({"_id": m.id}):
                    continue
                log.info("migration %s: running", m.id)
                start = time.monotonic()
                await m.up(db)
                took = round((time.monotonic() - start) * 1000)
                await db.migrations.update_one({"_id": m.id}, {"$set": {
                    "applied_at": datetime.now(timezone.utc), "duration_ms": took, "by": owner,
                    "git_sha": get_settings().git_sha}}, upsert=True)
                await _renew(db, owner)
            log.info("migration %s: done in %d ms", m.id, took)
            ran.append(m.id)
    finally:
        with pymongo.timeout(30):
            await _release(db, owner)
    return ran
