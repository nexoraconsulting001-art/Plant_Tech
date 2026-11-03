"""How long data is kept (decision D9, the recommended defaults until the owner changes them).

| Data | Kept | How |
|---|---|---|
| Bills, purchases, stock history, customers' bills | 8 years (GST, CGST s.36) | never removed here; closed years archived later (P2) |
| Audit log | 8 years | TTL index on `at` (migration 1001_retention; audit.py) |
| Saved orders (carts) | 7 days untouched | TTL index on `saved_at` (WP-1), and `apply_retention` on the text `updated_at` |
| Logs in the database (`ai_calls`, WP-6) | 180 days (CERT-In asks for at least 180) | `apply_retention` |
| Shop exports | link for EXPORT_LINK_HOURS, file removed after, record after 30 days | routers/exports.py, TTL on `delete_after` |
| Customers with no purchase for 3 years | anonymised | not done yet: `apply_retention` only counts them (the erase is WP-4's) |
| Orphaned photo uploads | 30 days | WP-7 (storage) |

`apply_retention` is a worker job (daily; WP-5 schedules it). Dates are text (ISO) in older collections and BSON dates
in newer ones; a TTL index only works on dates, so the job removes old rows of either kind.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pymongo

import jobs

logger = logging.getLogger(__name__)

CERT_IN_MIN_DAYS = 180


def _days(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Rule:
    collection: str
    field: str
    days: int
    why: str


def rules() -> list[Rule]:
    return [
        Rule("carts", "updated_at", 7, "D9: saved orders untouched for 7 days (carts.KEEP_DAYS)"),
        Rule("ai_calls", "at", _days("RETENTION_LOG_DAYS", CERT_IN_MIN_DAYS, CERT_IN_MIN_DAYS),
             "D9: logs 180 days (CERT-In minimum)"),
    ]


def older_than(field: str, cutoff: datetime) -> dict:
    """Rows whose `field` is before `cutoff`, whether it is a BSON date or ISO text in UTC (now_iso())."""
    return {"$or": [{field: {"$lt": cutoff}}, {field: {"$type": "string", "$lt": cutoff.isoformat()}}]}


async def apply(db, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    removed = {}
    with pymongo.timeout(600):
        for rule in rules():
            r = await db[rule.collection].delete_many(older_than(rule.field, now - timedelta(days=rule.days)))
            removed[rule.collection] = r.deleted_count
        stale = await db.customers.count_documents(older_than("last_purchase_at", now - timedelta(days=3 * 365)))
    removed["customers_to_anonymise"] = stale  # counted only (owner decision; WP-4 does the erase)
    return removed


@jobs.job
async def apply_retention(ctx):
    """Worker cron, daily: removes what D9 says is no longer kept."""
    removed = await apply(ctx["db"])
    logger.info("retention: %s", removed)
    return removed
