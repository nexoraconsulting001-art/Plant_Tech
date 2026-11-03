"""Backfill migrations read each document once (migrations.walk), however many batches they write.

The MongoDB profiler counts the documents each query read (docsExamined). The old 0401 looked for "the next
batch without fields" from the start every time, so each batch read past every customer done before it: the work
grows with the square of the collection (2 million customers took 48 minutes; the 5,000 here already cost 10,000
reads). A walk reads each document about once.
"""
import asyncio
import importlib
from datetime import datetime, timezone

import pytest
from bson import ObjectId

import db as db_module
import migrations
from conftest import MOCK, run
from routers.customers import search_fields

pytestmark = pytest.mark.skipif(MOCK, reason="needs a real MongoDB (profiler)")
N, BATCH = 5000, 500
m0401 = importlib.import_module("migrations.0401_customer_search_fields")


def _customers(db, n: int, start: int = 0):
    return [{"id": f"c{i}", "business_id": f"b{i % 7}", "name": f"Customer {i}", "phone": f"+9198{i:08d}",
             "created_at": "2026-11-03T00:00:00+00:00"} for i in range(start, start + n)]


async def _examined(db, coll: str, work) -> tuple[int, int]:
    """(documents read by queries on `coll`, number of those queries) while `work()` runs."""
    await db.command("profile", 2)
    try:
        await work()
    finally:
        await db.command("profile", 0)
    rows = await db.system.profile.find({"ns": f"{db.name}.{coll}", "op": {"$in": ["query", "getmore"]}}).to_list(None)
    await db.system.profile.drop()
    return sum(r.get("docsExamined", 0) for r in rows), len(rows)


def test_0401_reads_each_customer_once(client, monkeypatch):
    db = db_module.state.db
    monkeypatch.setattr(m0401, "BATCH", BATCH)
    run(db.customers.insert_many(_customers(db, N)))

    read, queries = run(_examined(db, "customers", lambda: m0401.up(db)))
    assert read <= N * 1.05, read  # the old loop read 10,000 here, growing with the square of N
    assert queries <= N // BATCH + 2
    done = run(db.customers.find({}, {"_id": 0, "name": 1, "phone": 1, "name_key": 1, "name_words": 1,
                                      "phone10": 1, "phone10_rev": 1}).to_list(None))
    assert len(done) == N
    assert all({k: c[k] for k in ("name_key", "name_words", "phone10", "phone10_rev")}
               == search_fields(c["name"], c["phone"]) for c in done)

    # Again (a runner that stopped half-way, or a rerun): nothing left to write, still one read per customer.
    before = run(db.customers.find({}, {"_id": 1, "name_key": 1}).to_list(None))
    read, _ = run(_examined(db, "customers", lambda: m0401.up(db)))
    assert read <= N * 1.05
    assert run(db.customers.find({}, {"_id": 1, "name_key": 1}).to_list(None)) == before


def test_0401_finishes_a_partly_done_collection(client, monkeypatch):
    """Customers with fields already (an earlier run, or saved by the new app) are left as they are."""
    db = db_module.state.db
    monkeypatch.setattr(m0401, "BATCH", 300)
    docs = _customers(db, 2000)
    for c in docs[::2]:
        c.update(search_fields(c["name"], c["phone"]), name_key="kept as it was")
    run(db.customers.insert_many(docs))
    run(m0401.up(db))
    rows = {c["id"]: c for c in run(db.customers.find({}, {"_id": 0}).to_list(None))}
    assert all(rows[c["id"]]["name_key"] == "kept as it was" for c in docs[::2])
    assert all(rows[c["id"]]["name_key"] == c["name"].lower() for c in docs[1::2])



def test_0401_fills_a_customer_saved_behind_the_walk(client, monkeypatch):
    """Load-test fix review: a customer saved without the fields while 0401 runs, with an _id below where the walk
    has got to (another process's or machine's ObjectId), is filled by the last pass instead of being passed by."""
    db = db_module.state.db
    monkeypatch.setattr(m0401, "BATCH", 300)
    run(db.customers.insert_many(_customers(db, 1000)))
    behind = ObjectId.from_datetime(datetime(2020, 1, 1, tzinfo=timezone.utc))  # sorts before every _id above
    real_walk = m0401.walk

    async def walk_with_a_late_insert(*args, **kwargs):
        first = True
        async for docs in real_walk(*args, **kwargs):
            yield docs
            if first:
                first = False
                await db.customers.insert_one({"_id": behind, **_customers(db, 1, start=5000)[0]})

    monkeypatch.setattr(m0401, "walk", walk_with_a_late_insert)
    run(m0401.up(db))
    late = run(db.customers.find_one({"_id": behind}))
    fields = ("name_key", "name_words", "phone10", "phone10_rev")
    assert {k: late[k] for k in fields} == search_fields(late["name"], late["phone"])
    assert run(db.customers.count_documents({"name_key": None})) == 0

def test_walk_visits_every_match_once_in_id_order(client):
    db = db_module.state.db
    run(db.walk_check.insert_many([{"n": i, "odd": i % 2 == 1} for i in range(2345)]))

    async def collect(query):
        seen = []
        async for batch in migrations.walk(db.walk_check, query, {"n": 1}, batch=100):
            assert len(batch) <= 100
            seen += [d["n"] for d in batch]
            # what the caller writes meanwhile doesn't change the walk
            await db.walk_check.update_many({"_id": {"$in": [d["_id"] for d in batch]}}, {"$set": {"odd": None}})
        return seen

    assert run(collect({"odd": True})) == list(range(1, 2345, 2))
    assert run(collect({"odd": True})) == []  # every match was changed by the first walk
    assert run(collect({})) == list(range(2345))
    assert run(collect({"n": -1})) == []


def test_lock_is_renewed_while_a_long_migration_runs(client, monkeypatch):
    """A migration longer than LOCK_TTL keeps the lock, so a second runner never starts the same work."""
    db = db_module.state.db
    monkeypatch.setattr(migrations, "RENEW_EVERY", 0.05)
    seen = []

    async def slow(db):
        seen.append((await db.migration_lock.find_one({"_id": migrations.LOCK_ID}))["expires_at"])
        await asyncio.sleep(0.4)
        seen.append((await db.migration_lock.find_one({"_id": migrations.LOCK_ID}))["expires_at"])

    monkeypatch.setattr(migrations, "discover", lambda: [migrations.Migration("9999_slow", 9999, slow, "slow")])
    assert run(migrations.up(db)) == ["9999_slow"]
    assert seen[1] > seen[0]
    assert run(db.migration_lock.count_documents({})) == 0  # released at the end
