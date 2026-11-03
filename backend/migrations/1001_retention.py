"""Retention and compliance indexes (WP-10, decision D9): TTLs for audit log, saved orders and exports; month index on bills.

- audit_log: entries go after 8 years (audit.KEEP_DAYS). With AUDIT_DB set the log's indexes are made in that
  database too; if the app's user may not (the auditAppend role has no createIndex), it is logged and skipped:
  an administrator makes them (`python -m audit role`).
- carts: saved orders untouched for 7 days, on the `saved_at` date (WP-1; updated_at stays text for the previous
  version). The same index WP-1 makes; a differently made one is left as it is. retention.apply_retention removes
  old orders by their text updated_at as well.
- exports: the shop's export records, removed at `delete_after` (routers/exports.py).
- invoices {business_id, date_key}: a month of one shop's bills for the GST returns (gstr.collect).
An index that already exists with other options is logged and skipped, so this never blocks a deploy.
"""
import logging

import audit
from routers import carts

log = logging.getLogger("migrations")


async def _index(coll, keys, **options):
    try:
        await coll.create_index(keys, **options)
    except Exception as e:  # IndexOptionsConflict, or no createIndex right on the audit database
        log.warning("index %s %s: %s", coll.full_name, keys, e)


async def up(db):
    log_coll = audit.collection(db)
    if log_coll.database.name != db.name:  # AUDIT_DB: the indexes 0002 made in the app database, there too
        await _index(log_coll, [("business_id", 1), ("at", -1)])
        await _index(log_coll, [("actor.id", 1), ("at", -1)])
    await _index(log_coll, "at", expireAfterSeconds=audit.KEEP_DAYS * 86400)
    await _index(db.carts, "saved_at", expireAfterSeconds=carts.KEEP_DAYS * 86400)
    await _index(db.exports, "id", unique=True)
    await _index(db.exports, [("business_id", 1), ("created_at", -1)])
    await _index(db.exports, [("status", 1), ("expires_at", 1)])
    await _index(db.exports, "delete_after", expireAfterSeconds=0)
    await _index(db.invoices, [("business_id", 1), ("date_key", 1)])
