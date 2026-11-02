"""Phone notifications to the shop owner, sent through Expo's push service.

When an owner logs in on a phone, the app sends its Expo push token (routers/notifications.py). It is kept in
`push_tokens`, one row per token: whose phone it is, their shop, and the app language on that phone, so each
phone gets the text in its own language. A token belongs to the last owner who logged in on that phone.

Sending is a durable background job (jobs.py): `send_to_owners` queues `deliver_push` and returns, so a sale
never waits for Expo or fails because of it. With Redis the job sits in the arq queue until the worker has
done it, so a restart of the web process or the worker does not lose it (arq runs an interrupted job again;
a phone may then get the same notification twice, never none). Without Redis (a laptop, tests) it runs as a
task in this process, as before.

- One request to Expo per job (at most 100 messages, Expo's limit), through one shared httpx client per process.
- Network errors, 429 and 5xx answers are tried again later (BACKOFF: 30 s, 2 min, 10 min, 30 min), then given up.
  Messages Expo turns down for MessageRateExceeded are tried again the same way; other per-message errors are
  logged.
- Expo's tickets only say a message was accepted. About 15 minutes later `check_push_receipts` asks Expo whether
  it reached Google/Apple; a phone that no longer has the app (DeviceNotRegistered, in a ticket or a receipt) is
  forgotten. Credential errors (InvalidCredentials, MismatchSenderId) are logged as errors, so they reach Sentry.

PUSH_ENABLED (default off) switches real sending on; tests and the local dev server never call out.
EXPO_ACCESS_TOKEN is needed only if "enhanced push security" is switched on for the Expo project.
"""
import asyncio
import logging
import os

import httpx
from arq import Retry

import jobs

log = logging.getLogger(__name__)

PUSH_URL = "https://exp.host/--/api/v2/push/send"
RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
BATCH = 100  # Expo takes at most 100 messages per request
RECEIPTS_BATCH = 1000  # ... and at most 1000 receipt ids
CHANNEL = "alerts"  # the Android notification channel the app creates (src/push.ts)
BACKOFF = (30, 120, 600, 1800)  # seconds to wait before try 2, 3, 4 and 5
MAX_TRIES = len(BACKOFF) + 1
RECEIPT_DELAY = 15 * 60  # Expo: receipts are usually ready within 15 minutes, and kept for 24 hours
RECEIPT_TRIES = 3  # receipts not ready yet are asked for again, RECEIPT_DELAY apart
GONE = "DeviceNotRegistered"
CONFIG_ERRORS = {"InvalidCredentials", "MismatchSenderId"}  # our Expo/Firebase setup is wrong: an error, not a warning

_http: dict = {"loop": None, "client": None}


def enabled() -> bool:
    return os.environ.get("PUSH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def http() -> httpx.AsyncClient:
    """The process's one HTTP client for Expo (and the worker's heartbeat): connections are kept and reused.
    An httpx client belongs to one event loop, so a new loop (tests) gets a new client."""
    loop = asyncio.get_running_loop()
    client = _http["client"]
    if client is None or client.is_closed or _http["loop"] is not loop:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5),
                                   limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        _http.update(loop=loop, client=client)
    return client


async def close():
    client = _http["client"]
    if client is not None and _http["loop"] is asyncio.get_running_loop():
        await client.aclose()
    _http.update(loop=None, client=None)


async def owner_tokens(db, business_id: str) -> list[dict]:
    """The phones of the shop's owner(s). Employees never get these notifications."""
    owners = await db.users.find({"business_id": business_id, "role": "owner", "active": {"$ne": False}},
                                 {"_id": 0, "id": 1}).to_list(20)
    if not owners:
        return []
    return await db.push_tokens.find({"business_id": business_id, "user_id": {"$in": [o["id"] for o in owners]}},
                                     {"_id": 0}).to_list(50)


async def send_to_owners(db, business_id: str, build, data: dict, tokens: list[dict] | None = None,
                         key: str | None = None) -> int:
    """`build(lang)` -> (title, body) in that language. Returns how many phones it was sent to.
    `key` names this notification (e.g. "summary:<shop>:<day>"): queued twice, it is still sent once."""
    if tokens is None:
        tokens = await owner_tokens(db, business_id)
    messages = []
    for t in tokens:
        title, body = build(t.get("lang") or "en")
        messages.append({"to": t["token"], "title": title, "body": body, "data": data, "sound": "default",
                         "channelId": CHANNEL, "priority": "high"})
    if messages:
        await dispatch(db, messages, key)
    return len(messages)


async def dispatch(db, messages: list[dict], key: str | None = None):
    """Queue the messages for sending (one job per 100). Tests replace this to see what would be sent."""
    if not enabled():
        log.info("push is off (PUSH_ENABLED): %d message(s) not sent", len(messages))
        return
    for i in range(0, len(messages), BATCH):
        await jobs.enqueue("deliver_push", messages[i:i + BATCH], _job_id=f"push:{key}:{i // BATCH}" if key else None)


def _headers() -> dict:
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate", "Content-Type": "application/json"}
    if key := os.environ.get("EXPO_ACCESS_TOKEN", "").strip():
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def send(messages: list[dict], client: httpx.AsyncClient | None = None) -> list[dict]:
    """POST to Expo; one ticket per message, in the same order. Raises on a network error or a 4xx/5xx answer."""
    client = client or http()
    tickets: list[dict] = []
    for i in range(0, len(messages), BATCH):
        r = await client.post(PUSH_URL, json=messages[i:i + BATCH], headers=_headers())
        r.raise_for_status()
        tickets.extend(r.json().get("data") or [])
    return tickets


async def get_receipts(ids: list[str], client: httpx.AsyncClient | None = None) -> dict[str, dict]:
    """Expo's receipts for these ticket ids: {id: {"status": "ok"} or {"status": "error", ...}}. Ids whose
    receipt is not ready yet are missing."""
    client = client or http()
    receipts: dict[str, dict] = {}
    for i in range(0, len(ids), RECEIPTS_BATCH):
        r = await client.post(RECEIPTS_URL, json={"ids": ids[i:i + RECEIPTS_BATCH]}, headers=_headers())
        r.raise_for_status()
        receipts.update(r.json().get("data") or {})
    return receipts


def _retry_after(e: Exception) -> float | None:
    """Seconds to wait before trying again, or None when trying again cannot help (a 400, a bad token...)."""
    if isinstance(e, httpx.TransportError):  # no answer, a timeout, a dropped connection
        return 0
    if isinstance(e, httpx.HTTPStatusError) and (e.response.status_code == 429 or e.response.status_code >= 500):
        try:
            return float(e.response.headers.get("Retry-After") or 0)
        except ValueError:
            return 0
    return None


def _tries(ctx: dict, attempt: int) -> int:
    """This is try number ... (arq counts its own retries in job_try; a job queued again passes `attempt`)."""
    return attempt + (ctx.get("job_try") or 1) - 1


async def _later(ctx: dict, name: str, payload, tries: int, wait: float = 0):
    """Run this job again after the back-off: arq's Retry keeps the same job; in-process it is queued anew."""
    delay = max(BACKOFF[min(tries, len(BACKOFF)) - 1], wait)
    if ctx.get("in_process"):
        await jobs.enqueue(name, payload, attempt=tries + 1, _defer_by=delay)
        return
    raise Retry(defer=delay)


async def forget(db, tokens: list[str], why: str):
    if tokens:
        await db.push_tokens.delete_many({"token": {"$in": tokens}})
        log.info("push: dropped %d phone(s): %s", len(tokens), why)


def _error(ticket: dict) -> str:
    return (ticket.get("details") or {}).get("error", "") or "error"


@jobs.job
async def deliver_push(ctx, messages: list[dict], attempt: int = 1):
    """Send one batch to Expo, try again later if Expo could not take it, forget phones without the app, and
    queue the receipt check. Never raises after Expo accepted the batch (a retry would send it twice)."""
    db, tries = ctx["db"], _tries(ctx, attempt)
    try:
        tickets = await send(messages)
    except Exception as e:
        wait = _retry_after(e)
        if wait is None or tries >= MAX_TRIES:
            log.error("push: giving up on %d message(s) after try %d: %r", len(messages), tries, e)
            return
        log.warning("push: sending %d message(s) failed (try %d), trying again: %r", len(messages), tries, e)
        await _later(ctx, "deliver_push", messages, tries, wait)
        return
    try:
        gone, again, receipts = [], [], {}
        for m, ticket in zip(messages, tickets):
            if ticket.get("status") == "ok":
                if ticket.get("id"):
                    receipts[ticket["id"]] = m["to"]
                continue
            error = _error(ticket)
            if error == GONE:
                gone.append(m["to"])
            elif error == "MessageRateExceeded" and tries < MAX_TRIES:
                again.append(m)
            else:
                (log.error if error in CONFIG_ERRORS else log.warning)(
                    "push: %s for one phone: %s", error, ticket.get("message", ""))
        await forget(db, gone, "the app is no longer on them")
        if again:
            await jobs.enqueue("deliver_push", again, attempt=tries + 1, _defer_by=BACKOFF[tries - 1])
        if receipts and not ctx.get("in_process"):  # no queue on a laptop: nothing would come back for them
            await jobs.enqueue("check_push_receipts", receipts, _defer_by=RECEIPT_DELAY)
    except Exception:
        log.exception("push: sent, but handling Expo's answer failed")


@jobs.job
async def check_push_receipts(ctx, tickets: dict[str, str], attempt: int = 1):
    """`tickets`: Expo ticket id -> the phone's token. Forgets phones Expo could not deliver to because the app
    is gone; asks again later for receipts that are not ready yet."""
    db, tries = ctx["db"], _tries(ctx, attempt)
    try:
        receipts = await get_receipts(list(tickets))
    except Exception as e:
        wait = _retry_after(e)
        if wait is None or tries >= RECEIPT_TRIES:
            log.warning("push: receipts for %d message(s) not checked: %r", len(tickets), e)
            return
        await _later(ctx, "check_push_receipts", tickets, tries, wait)
        return
    gone, missing = [], {}
    for ticket_id, token in tickets.items():
        receipt = receipts.get(ticket_id)
        if receipt is None:
            missing[ticket_id] = token
        elif receipt.get("status") == "error":
            error = _error(receipt)
            if error == GONE:
                gone.append(token)
            else:
                (log.error if error in CONFIG_ERRORS else log.warning)(
                    "push receipt: %s for one phone: %s", error, receipt.get("message", ""))
    await forget(db, gone, "Expo says the app is no longer on them")
    if missing and tries < RECEIPT_TRIES:
        await jobs.enqueue("check_push_receipts", missing, attempt=tries + 1, _defer_by=RECEIPT_DELAY)
