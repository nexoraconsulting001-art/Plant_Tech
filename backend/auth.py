"""Logins: password hashing, login tokens (JWT), Google sign-in, and permission checks.

Roles: an owner can do everything in their shop. A worker can do only what the owner
switched on: sell (billing), stock (add products/stock), customers, reports.
App admins (`is_admin` on the user, set with `python -m manage_users admin grant <phone>`) can also manage the
shared picture library. Admin rights no longer come from a phone number: ADMIN_PHONES is read once, by
migration 0801, and never at run time.

Slow work stays off the event loop: bcrypt (about 0.25 s of CPU per check) and Google's token check run in
threads, at most AUTH_THREADS at a time per process, so a burst of logins never stalls billing; when too many
are already waiting the login gets a 503 with Retry-After instead of queueing. Google's signing certificates
are kept for as long as Google's Cache-Control allows (hours), not fetched on every sign-in.

Logged-in users are cached per process for USER_CACHE_SECONDS (30 s), and an entry is only used while its
token_version matches the token's: a new password or a switched-off login takes effect at once in the process
that made the change, and within that time everywhere else.

Login tokens carry `iss` and `aud` (JWT_ISSUER, JWT_AUDIENCE). Tokens issued before they existed (1.1.0
logins) have neither and are accepted until they expire (30 days); JWT_REQUIRE_AUD=1 stops that once 1.1.0 is
retired.
"""
import asyncio
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import anyio
import anyio.to_thread
import bcrypt
import google.auth.exceptions
import google.auth.transport
import google.auth.transport.requests
import jwt
import requests
from cachetools import TTLCache
from fastapi import Depends, Header, HTTPException
from pymongo import ReturnDocument

import observability
from db import get_db
from errors import AppError

PERMS = ("sell", "stock", "customers", "reports")
WORKER_DEFAULT_PERMS = {"sell": True, "stock": True, "customers": True, "reports": False}
TOKEN_DAYS = 30
MIN_PIN = 6  # new or changed employee PINs; older 4-digit PINs keep working until they are changed
MIN_OWNER_PASSWORD = 8


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


JWT_ISSUER = os.environ.get("JWT_ISSUER", "hara-hisaab")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "hara-hisaab-app")
JWT_REQUIRE_AUD = _flag("JWT_REQUIRE_AUD")  # after 1.1.0 is retired: refuse tokens without iss/aud
AUTH_THREADS = max(1, int(os.environ.get("AUTH_THREADS", "2")))  # per process; bcrypt is pure CPU
MAX_WAITING = 64  # logins waiting for one of those threads; more than this get a 503 (try again)
USER_CACHE_SECONDS = float(os.environ.get("USER_CACHE_SECONDS", "30"))


# ---- slow work in threads -----------------------------------------------------------------------------------

_limiter: tuple | None = None  # (event loop, CapacityLimiter): a limiter belongs to the loop that made it


def _auth_limiter() -> anyio.CapacityLimiter:
    global _limiter
    loop = asyncio.get_running_loop()
    if _limiter is None or _limiter[0] is not loop:
        _limiter = (loop, anyio.CapacityLimiter(AUTH_THREADS))
    return _limiter[1]


async def off_loop(fn, *args):
    """`fn(*args)` in a thread, sharing AUTH_THREADS threads with every other login in this process."""
    limiter = _auth_limiter()
    if limiter.statistics().tasks_waiting >= MAX_WAITING:
        err = AppError(503, "The server is busy. Please try again in a few seconds.", code="busy")
        err.headers = {"Retry-After": "5"}
        raise err
    return await anyio.to_thread.run_sync(fn, *args, limiter=limiter)


# ---- passwords and PINs -------------------------------------------------------------------------------------

def _pw(password: str) -> bytes:
    # bcrypt only ever used the first 72 bytes; bcrypt 5 refuses longer ones instead of cutting them, so cut
    # them here and passwords set before keep working.
    return password.encode()[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw(password), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_pw(password), hashed.encode())
    except ValueError:
        return False


_dummy = {"hash": None}
_dummy_lock = threading.Lock()


def _check_or_dummy(password: str, hashed: str | None) -> bool:
    """No account (or no password): check against a throwaway hash anyway, so the time taken does not tell
    which phone numbers have an account."""
    if hashed:
        return check_password(password, hashed)
    with _dummy_lock:
        if _dummy["hash"] is None:
            _dummy["hash"] = hash_password(secrets.token_hex(16))
    check_password(password, _dummy["hash"])
    return False


async def hash_password_async(password: str) -> str:
    return await off_loop(hash_password, password)


async def check_password_async(password: str, hashed: str | None) -> bool:
    return await off_loop(_check_or_dummy, password, hashed)


def _easy_pin(pin: str) -> bool:
    if not pin.isdigit():
        return False
    runs = "01234567890123456789"
    if pin in runs or pin[::-1] in runs:  # 123456, 654321, 890123
        return True
    return any(len(pin) % n == 0 and pin == pin[:n] * (len(pin) // n) for n in (1, 2, 3))  # 111111, 121212, 123123


def check_new_pin(pin: str):
    """A new or changed employee PIN (or password): at least MIN_PIN characters and not an obvious one."""
    if len(pin) < MIN_PIN:
        raise AppError(400, "Please use a PIN of at least {{n}} digits.", code="pin_too_short", n=MIN_PIN)
    if _easy_pin(pin):
        raise AppError(400, "This PIN is too easy to guess. Please choose another.", code="pin_too_easy")


# ---- wrong passwords: a growing wait instead of a lock ---------------------------------------------------------
# Counted per phone number in `login_attempts` (_id = the phone), whether or not it has an account, so the
# answers never tell which numbers do. Forgotten a day after the last wrong one (TTL index, migration 0803),
# after a right one, or when the owner sets a new PIN.

FREE_TRIES = 5  # wrong passwords in a row before the first wait
FIRST_WAIT = 30  # seconds after the 5th; doubles with each further wrong one
MAX_WAIT = 15 * 60


def wait_after(failed: int) -> int:
    return 0 if failed < FREE_TRIES else min(MAX_WAIT, FIRST_WAIT * 2 ** (failed - FREE_TRIES))


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def login_wait(db, phone: str) -> int:
    """Seconds this phone must still wait before the next try (0: go ahead)."""
    doc = await db.login_attempts.find_one({"_id": phone}, {"wait_until": 1})
    if not doc or not doc.get("wait_until"):
        return 0
    return max(0, math.ceil((_utc(doc["wait_until"]) - datetime.now(timezone.utc)).total_seconds()))


async def login_failed(db, phone: str) -> tuple[int, int]:
    """Count a wrong password; returns (wrong ones in a row, seconds to wait now)."""
    now = datetime.now(timezone.utc)
    doc = await db.login_attempts.find_one_and_update(
        {"_id": phone}, {"$inc": {"failed": 1}, "$set": {"last_failed_at": now}},
        upsert=True, return_document=ReturnDocument.AFTER)
    wait = wait_after(doc["failed"])
    if wait:
        await db.login_attempts.update_one({"_id": phone}, {"$max": {"wait_until": now + timedelta(seconds=wait)}})
    return doc["failed"], wait


async def clear_login_failures(db, phone: str | None):
    if phone:
        await db.login_attempts.delete_one({"_id": phone})


def too_many_tries(seconds: int) -> AppError:
    if seconds > 90:
        err = AppError(429, "Too many incorrect attempts. Please try again in {{minutes}} minutes.",
                       code="login_wait", minutes=math.ceil(seconds / 60))
    else:
        err = AppError(429, "Too many incorrect attempts. Please try again in {{seconds}} seconds.",
                       code="login_wait", seconds=seconds)
    err.headers = {"Retry-After": str(seconds)}
    return err


# ---- login tokens ----------------------------------------------------------------------------------------------

def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(500, "Sign-in is not configured on the server.")
    return secret


def make_token(user: dict) -> str:
    now = int(time.time())
    payload = {"sub": user["id"], "bid": user["business_id"], "role": user["role"],
               "tv": user.get("token_version", 0), "iat": now, "exp": now + TOKEN_DAYS * 86400,
               "iss": JWT_ISSUER, "aud": JWT_AUDIENCE}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> dict:
    """The token's claims; raises jwt.InvalidTokenError (ExpiredSignatureError when it has expired)."""
    secret = _secret()
    try:
        return jwt.decode(token, secret, algorithms=["HS256"], issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
                          options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    except jwt.MissingRequiredClaimError as e:
        if JWT_REQUIRE_AUD or e.claim not in ("iss", "aud"):
            raise
    # A token from before iss/aud (1.1.0 and the 1.2.0 tests): accepted while it lasts, if it has neither.
    claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "iat", "sub"],
                                                                      "verify_aud": False, "verify_iss": False})
    if "iss" in claims or "aud" in claims or claims["iat"] < time.time() - TOKEN_DAYS * 86400:
        raise jwt.InvalidTokenError("token without iss/aud")
    return claims


# ---- Google sign-in --------------------------------------------------------------------------------------------

def _max_age(headers) -> int:
    m = re.search(r"max-age=(\d+)", headers.get("Cache-Control") or headers.get("cache-control") or "")
    return min(int(m.group(1)), 86400) if m else 3600


class CachingRequest(google.auth.transport.Request):
    """google-auth's HTTP transport that keeps Google's certificate responses for as long as their Cache-Control
    says (the caching google-auth's documentation recommends for verify_oauth2_token)."""

    def __init__(self, inner: google.auth.transport.Request | None = None):
        self._inner = inner or google.auth.transport.requests.Request(session=requests.Session())
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def __call__(self, url, method="GET", body=None, headers=None, timeout=10, **kwargs):
        if method != "GET":
            return self._inner(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
        with self._lock:
            hit = self._cache.get(url)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            response = self._inner(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
            if response.status == 200:
                self._cache[url] = (time.monotonic() + _max_age(response.headers), response)
            return response


_google_request: CachingRequest | None = None


def verify_google_id_token(token: str) -> dict:
    """Check a Google sign-in token; returns its claims (sub, email, name). Blocking: routes call it through
    google_claims(). Tests replace this."""
    from google.oauth2 import id_token

    global _google_request
    client_ids = [c.strip() for c in os.environ.get("GOOGLE_CLIENT_IDS", "").split(",") if c.strip()]
    if not client_ids:
        raise HTTPException(503, "Google sign-in is not available yet.")
    if _google_request is None:
        _google_request = CachingRequest()
    try:
        info = id_token.verify_oauth2_token(token, _google_request, audience=client_ids, clock_skew_in_seconds=10)
    except google.auth.exceptions.TransportError as e:
        raise HTTPException(503, "Google sign-in is not available right now. Please try again later.") from e
    except ValueError as e:
        raise HTTPException(401, "Google sign-in failed. Please try again.") from e
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Google sign-in failed. Please try again.")
    return info


async def google_claims(token: str) -> dict:
    return await off_loop(lambda: verify_google_id_token(token))  # looked up at call time (tests replace it)


# ---- who is asking ------------------------------------------------------------------------------------------

def is_admin(user: dict) -> bool:
    return bool(user.get("is_admin"))


def can(user: dict, perm: str) -> bool:
    if perm == "admin":
        return is_admin(user)
    if user["role"] == "owner":
        return True
    if perm == "owner":
        return False
    return bool((user.get("permissions") or {}).get(perm))


_users: TTLCache = TTLCache(maxsize=20_000, ttl=USER_CACHE_SECONDS)


def forget_user(user_id: str):
    """Drop a cached login after changing the user (permissions, admin, switched off): the next request reads it."""
    _users.pop(user_id, None)


async def current_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please log in.")
    try:
        claims = decode_token(authorization[7:].strip())
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(401, "Your session has expired. Please log in again.") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, "Please log in.") from e
    user = _users.get(claims["sub"])
    if user is None or user.get("token_version", 0) != claims.get("tv"):
        user = await get_db().users.find_one({"id": claims["sub"]}, {"_id": 0})
        if user:
            _users[user["id"]] = user
    if not user or user.get("token_version", 0) != claims.get("tv"):
        raise HTTPException(401, "Please log in again.")
    if not user.get("active", True):
        raise HTTPException(403, "This login has been turned off. Please contact the shop owner.")
    observability.bind_user(user)  # business_id and user_id on this request's log lines and errors
    return dict(user)


def require(*perms: str):
    """Dependency: the logged-in user must have at least one of `perms`."""
    async def dep(user: dict = Depends(current_user)) -> dict:
        if not any(can(user, p) for p in perms):
            raise HTTPException(403, "You do not have permission for this. Please ask the shop owner.")
        return user
    return dep


def masked_phone(phone: str | None) -> str | None:
    """For the audit log: the last four digits only."""
    return f"******{phone[-4:]}" if phone else None


def public_user(user: dict, business: dict | None) -> dict:
    perms = {p: can(user, p) for p in PERMS}
    return {
        "id": user["id"],
        "name": user.get("name", ""),
        "phone": user.get("phone"),
        "email": user.get("email"),
        "role": user["role"],
        "permissions": perms,
        "is_admin": is_admin(user),
        "has_password": bool(user.get("password_hash")),
        "business": {
            "id": business["id"],
            "name": business.get("name", ""),
            "onboarding_done": business.get("onboarding_done", False),
            "gst_registered": business.get("gst_registered", False),
            "logo_path": business.get("logo_path"),
            "profit_percent": business.get("profit_percent", 60),
        } if business else None,
    }
