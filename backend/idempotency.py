"""Idempotency-Key: a request that makes a record (a bill, a correction, a supplier bill) may be sent again with the
same key, e.g. after the app got no answer, and gets the record the first one made instead of a second one.

    key = IdempotencyKey                          # the route's optional header parameter
    fp = fingerprint(body)                        # what the request is; the same key with another body is refused
    if done := await previous(db, user, key, "purchase") ...   # an earlier request with this key: answer with its record
    await in_transaction(keyed(db, user, key, "purchase", new_id, fp, work, id_field="purchase_id"), db)
    except DuplicateKeyError as e: if is_key_clash(e): ...     # the same key arrived at the same moment: replay

The key is the first write of the record's transaction (`keyed`): a second request with the same key while the
first is still running write-conflicts on it and is retried by MongoDB, and once the first has committed it gets
a duplicate key and answers with the first one's record. A request that fails takes its key back with the
transaction; on a laptop without transactions the key is removed by hand. Keys are kept KEEP_DAYS (TTL index on
idempotency_keys.created_at, migration 0103), one per shop and key (unique index). Apps that send no key (1.1.0)
work as before.
"""
import hashlib
import json
from datetime import datetime, timezone

from fastapi import Header
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from errors import AppError

# Optional header; printable ASCII without spaces, like a UUID. 1.1.0 never sends it.
IdempotencyKey = Header(None, alias="Idempotency-Key", min_length=8, max_length=100, pattern=r"^[\x21-\x7e]+$")
KEEP_DAYS = 7


def fingerprint(body: BaseModel, include: set[str] | None = None) -> str:
    """A hash of what the request is (the `include` fields of its body, or all of them)."""
    seen = body.model_dump(include=include)
    return hashlib.sha256(json.dumps(seen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def keyed(db, user: dict, key: str | None, route: str, record_id: str, fp: str, work, *, id_field: str):
    """work(session) behind the key: the key is saved first, in the same transaction, naming the record it makes
    in `id_field` (invoice_id for bills, purchase_id for supplier bills)."""
    async def run(session):
        if key:
            await db.idempotency_keys.insert_one({"business_id": user["business_id"], "key": key, "route": route,
                                                  id_field: record_id, "user_id": user["id"], "fingerprint": fp,
                                                  "created_at": datetime.now(timezone.utc)}, session=session)
        try:
            return await work(session)
        except BaseException:
            if key and session is None:
                await db.idempotency_keys.delete_one({"business_id": user["business_id"], "key": key,
                                                      id_field: record_id})
            raise
    return run


async def previous(db, user: dict, key: str | None, route: str) -> dict | None:
    """The saved key of an earlier request with this key in this shop, or None (a new key, or no key). A key used
    for another kind of request is a 409 `idempotency_conflict`."""
    if not key:
        return None
    k = await db.idempotency_keys.find_one({"business_id": user["business_id"], "key": key}, {"_id": 0})
    if k and k["route"] != route:
        raise conflict()
    return k


def conflict() -> AppError:
    return AppError(409, "This request was already used for another bill.", "idempotency_conflict")


def changed(saved: dict, fp: str | None) -> bool:
    """The same key with a different request (changed after a try that got no answer). Keys saved before
    fingerprints existed have none and count as the same."""
    return bool(fp and saved.get("fingerprint") and saved["fingerprint"] != fp)


def is_key_clash(e: DuplicateKeyError) -> bool:
    return "key" in ((e.details or {}).get("keyPattern") or {})
