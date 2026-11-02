"""Audit log: who did what, kept apart from the data it is about (collection `audit_log`, append only).

    await audit.log(db, user, "staff.disable", {"type": "user", "id": staff_id}, {"reason": "left"})

`actor` is the logged-in user (or None for the system); `action` is "area.verb"; `target` says what it was
done to; `details` holds the rest (no passwords, no full phone numbers). Pass `session` inside a transaction
so the entry is written only if the change is. Each entry also gets the request_id of the request.
WP-10 adds the insert-only database role, retention and the daily export; WP-8 and WP-4 write the entries.
"""
from datetime import datetime, timezone

from common import new_id
from observability import request_context


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
    await db.audit_log.insert_one(dict(entry), session=session)
    return entry
