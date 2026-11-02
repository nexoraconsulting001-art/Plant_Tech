"""Phone notifications to the shop owner, sent through Expo's push service.

When an owner logs in on a phone, the app sends its Expo push token (routers/notifications.py). It is kept in
`push_tokens`, one row per token: whose phone it is, their shop, and the app language on that phone, so each
phone gets the text in its own language. A token belongs to the last owner who logged in on that phone.

Sending is fire and forget: `send_to_owners` returns at once and the HTTP call runs in the background, so a
sale never waits for it or fails because of it. Errors are logged; tokens Expo reports as DeviceNotRegistered
(the app was removed) are dropped.

PUSH_ENABLED (default off) switches real sending on; tests and the local dev server never call out.
EXPO_ACCESS_TOKEN is needed only if "enhanced push security" is switched on for the Expo project.
"""
import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

PUSH_URL = "https://exp.host/--/api/v2/push/send"
BATCH = 100  # Expo takes at most 100 messages per request
CHANNEL = "alerts"  # the Android notification channel the app creates (src/push.ts)
_running: set[asyncio.Task] = set()  # keeps background sends alive until they finish


def enabled() -> bool:
    return os.environ.get("PUSH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


async def owner_tokens(db, business_id: str) -> list[dict]:
    """The phones of the shop's owner(s). Employees never get these notifications."""
    owners = await db.users.find({"business_id": business_id, "role": "owner", "active": {"$ne": False}},
                                 {"_id": 0, "id": 1}).to_list(20)
    if not owners:
        return []
    return await db.push_tokens.find({"business_id": business_id, "user_id": {"$in": [o["id"] for o in owners]}},
                                     {"_id": 0}).to_list(50)


async def send_to_owners(db, business_id: str, build, data: dict, tokens: list[dict] | None = None) -> int:
    """`build(lang)` -> (title, body) in that language. Returns how many phones it was sent to."""
    if tokens is None:
        tokens = await owner_tokens(db, business_id)
    messages = []
    for t in tokens:
        title, body = build(t.get("lang") or "en")
        messages.append({"to": t["token"], "title": title, "body": body, "data": data, "sound": "default",
                         "channelId": CHANNEL, "priority": "high"})
    if messages:
        dispatch(db, messages)
    return len(messages)


def dispatch(db, messages: list[dict]):
    """Send in the background. Tests replace this to see what would be sent."""
    if not enabled():
        log.info("push is off (PUSH_ENABLED): %d message(s) not sent", len(messages))
        return
    task = asyncio.get_running_loop().create_task(deliver(db, messages))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def send(messages: list[dict], client: httpx.AsyncClient | None = None) -> list[dict]:
    """POST to Expo; one ticket per message, in the same order."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key := os.environ.get("EXPO_ACCESS_TOKEN", "").strip():
        headers["Authorization"] = f"Bearer {key}"
    tickets: list[dict] = []
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        for i in range(0, len(messages), BATCH):
            r = await client.post(PUSH_URL, json=messages[i:i + BATCH], headers=headers)
            r.raise_for_status()
            tickets.extend(r.json().get("data") or [])
    finally:
        if own:
            await client.aclose()
    return tickets


async def deliver(db, messages: list[dict]):
    """Send, log what failed, and forget phones that no longer have the app."""
    try:
        tickets = await send(messages)
    except Exception:
        log.exception("push: sending %d message(s) failed", len(messages))
        return
    gone = []
    for m, ticket in zip(messages, tickets):
        if ticket.get("status") != "error":
            continue
        error = (ticket.get("details") or {}).get("error", "")
        if error == "DeviceNotRegistered":
            gone.append(m["to"])
        else:
            log.warning("push: %s for one phone: %s", error or "error", ticket.get("message", ""))
    if gone:
        await db.push_tokens.delete_many({"token": {"$in": gone}})
        log.info("push: dropped %d phone(s) that no longer have the app", len(gone))
