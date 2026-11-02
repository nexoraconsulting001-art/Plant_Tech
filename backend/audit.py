"""Audit log: who did what, kept apart from the data it is about (collection `audit_log`, append only).

    await audit.log(db, user, "staff.disable", {"type": "user", "id": staff_id}, {"reason": "left"})

`actor` is the logged-in user (or None for the system); `action` is "area.verb"; `target` says what it was
done to; `details` holds the rest (no passwords, no full phone numbers). Pass `session` inside a transaction
so the entry is written only if the change is. Each entry also gets the request_id of the request.
The owner reads the shop's entries at GET /api/audit-log (routers/exports.py).

Append only, on three levels:
- Code: this module is the only one that touches the collection, and it only inserts and finds
  (tests/test_exports.py fails if anything else in backend/ updates or deletes it).
- Database: with AUDIT_DB set, the log lives in a database of its own, where the app's database user has only
  the `auditAppend` role (find + insert on audit_log). MongoDB roles add up and cannot leave one collection out
  of readWrite, which is why the log needs its own database. `python -m audit role` prints the role and how to
  give it (self-hosted: createRole; Atlas: a custom role). Without AUDIT_DB (a laptop, tests, staging today) the
  log stays in the app's database, where the app's user could still change it.
- Outside the database: the worker copies each India day's entries to object storage once the day is over
  (`archive_audit_day`: gzip of JSON lines; its SHA-256 is kept in `audit_archives`). AUDIT_ARCHIVE_BUCKET should
  be an R2 bucket with a bucket lock (retention) rule, so not even the API keys can change a copy.

Retention (decision D9): 8 years, then the TTL index from migration 1001_retention removes an entry.
"""
import gzip
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone

import jobs
from common import IST, new_id
from observability import request_context

logger = logging.getLogger(__name__)

ROLE = "auditAppend"
KEEP_DAYS = 8 * 366  # D9: 8 years (GST records); the TTL index in 1001_retention
PAGE_MAX = 200


def audit_db_name() -> str:
    """The database that holds the log: AUDIT_DB, or "" for the app's own database."""
    return os.environ.get("AUDIT_DB", "").strip()


def collection(db):
    """The audit_log collection, in AUDIT_DB when that is set (same client and replica set, so a transaction
    that writes to both databases still commits as one)."""
    name = audit_db_name()
    if name and name != db.name:
        return db.client[name].audit_log
    return db.audit_log


async def log(db, actor: dict | None, action: str, target: dict | str | None = None, details: dict | None = None,
              *, session=None, business_id: str | None = None) -> dict:
    actor = actor or {}
    ctx = request_context()
    entry = {
        "id": new_id(), "at": datetime.now(timezone.utc), "action": action,
        "business_id": business_id or actor.get("business_id"),
        "actor": {"id": actor.get("id"), "role": actor.get("role", "system")} if actor else {"role": "system"},
        "target": {"id": target} if isinstance(target, str) else (target or {}), "details": details or {},
        **({"request_id": ctx["request_id"]} if ctx.get("request_id") else {}),
    }
    await collection(db).insert_one(dict(entry), session=session)
    return entry


def _iso(at) -> str | None:
    if isinstance(at, datetime):
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat()
    return at


def shown(entry: dict) -> dict:
    """An entry as JSON gives it: `at` as an ISO time."""
    return {**entry, "at": _iso(entry.get("at"))}


async def find(db, business_id: str, *, action: str = "", actor_id: str = "", target_id: str = "",
               before: datetime | None = None, limit: int = 50) -> list[dict]:
    """A shop's entries, newest first. `action` "staff" also matches every "staff.…"; `before` pages back (the
    `at` of the last entry you got). Uses the {business_id, at} index."""
    q: dict = {"business_id": business_id}
    if action:
        q["action"] = action if "." in action else {"$regex": f"^{re.escape(action)}\\."}
    if actor_id:
        q["actor.id"] = actor_id
    if target_id:
        q["target.id"] = target_id
    if before:
        q["at"] = {"$lt": before}
    limit = max(1, min(limit, PAGE_MAX))
    rows = await collection(db).find(q, {"_id": 0}).sort("at", -1).limit(limit).to_list(limit)
    return [shown(r) for r in rows]


def role_definition(database: str) -> dict:
    """The createRole command for the audit database: read and add entries, nothing else. Not even createIndex
    (a new TTL index could expire entries): an administrator makes the indexes (`python -m audit role`)."""
    return {"createRole": ROLE,
            "privileges": [{"resource": {"db": database, "collection": "audit_log"}, "actions": ["find", "insert"]}],
            "roles": []}


# ---- the daily copy to object storage ----

def _archive_key(day: date) -> str:
    import storage_client

    return f"{storage_client.APP_NAME}/audit/{day:%Y/%m}/{day.isoformat()}.jsonl.gz"


def _put_archive(key: str, data: bytes):
    import storage_client

    s3 = storage_client.init_storage()
    if s3 is None:  # a laptop: the local storage folder
        storage_client.put_object(key, data, "application/gzip")
        return
    bucket = os.environ.get("AUDIT_ARCHIVE_BUCKET", "").strip() or storage_client.R2_BUCKET
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/gzip")


async def archive_day(db, day: date) -> dict:
    """Copy every entry of one India day to object storage, once: a day already copied is left alone (in a
    locked bucket it could not be replaced anyway), so running it twice is safe."""
    from fastapi.concurrency import run_in_threadpool

    day_id = day.isoformat()
    if done := await db.audit_archives.find_one({"_id": day_id}):
        return {**done, "skipped": True}
    start = datetime.combine(day, time(0), tzinfo=IST).astimezone(timezone.utc)
    lines = [json.dumps(shown(e), ensure_ascii=False, default=str, sort_keys=True)
             async for e in collection(db).find({"at": {"$gte": start, "$lt": start + timedelta(days=1)}},
                                                {"_id": 0}).sort("at", 1)]
    data = gzip.compress("".join(line + "\n" for line in lines).encode(), mtime=0)
    key = _archive_key(day)
    await run_in_threadpool(_put_archive, key, data)
    record = {"_id": day_id, "path": key, "count": len(lines), "bytes": len(data),
              "sha256": hashlib.sha256(data).hexdigest(), "at": datetime.now(timezone.utc)}
    await db.audit_archives.update_one({"_id": day_id}, {"$setOnInsert": record}, upsert=True)
    return record


@jobs.job
async def archive_audit_day(ctx, day: str | None = None):
    """Worker cron, daily after midnight India time: yesterday's entries to object storage (WP-5 schedules it)."""
    when = date.fromisoformat(day) if day else datetime.now(IST).date() - timedelta(days=1)
    record = await archive_day(ctx["db"], when)
    logger.info("audit archive %s: %s entries%s", when, record.get("count"), " (already done)" if record.get("skipped") else "")
    return record.get("path")


def _print_role(app_db: str):
    database = audit_db_name() or app_db
    if not audit_db_name():
        print("# AUDIT_DB is not set: the log is in the app database, where the app's user can change it.")
    print(f"# 1. As a user administrator (mongosh), once: the role and the indexes in {database}")
    print(f"use {database}")
    print(f"db.runCommand({json.dumps(role_definition(database))})")
    print("db.audit_log.createIndex({business_id: 1, at: -1})")
    print('db.audit_log.createIndex({"actor.id": 1, at: -1})')
    print(f"db.audit_log.createIndex({{at: 1}}, {{expireAfterSeconds: {KEEP_DAYS * 86400}}})")
    print(f"# 2. The app's user: readWrite on {app_db}, and on {database} only {ROLE}:")
    print(f'db.getSiblingDB("admin").grantRolesToUser("<app user>", [{{role: "{ROLE}", db: "{database}"}}])')
    print(f"# Atlas: Database Access > Custom Roles: role {ROLE} with FIND and INSERT on {database}.audit_log;")
    print(f"#   the app's database user gets readWrite@{app_db} and {ROLE}, nothing more on {database}.")


def _main() -> int:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(prog="python -m audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("role", help="print the auditAppend role and how to set it up")
    arch = sub.add_parser("archive", help="copy one day's entries to object storage now")
    arch.add_argument("--day", required=True, help="YYYY-MM-DD (India day)")
    args = ap.parse_args()

    from settings import check

    settings = check()
    if args.cmd == "role":
        _print_role(settings.mongo_db)
        return 0

    async def run():
        import db as db_module

        try:
            return await archive_day(db_module.get_db(), date.fromisoformat(args.day))
        finally:
            await db_module.close()

    print(asyncio.run(run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
