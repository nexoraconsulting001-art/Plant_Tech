"""Logins: password hashing, login tokens (JWT), Google sign-in, and permission checks.

Roles: an owner can do everything in their shop. A worker can do only what the owner
switched on: sell (billing), stock (add products/stock), customers, reports.
App admins (phones in ADMIN_PHONES) can also manage the shared picture library.
"""
import os
import time

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

import observability
from common import normalize_in_phone
from db import get_db

PERMS = ("sell", "stock", "customers", "reports")
WORKER_DEFAULT_PERMS = {"sell": True, "stock": True, "customers": True, "reports": False}
TOKEN_DAYS = 30
MAX_FAILED_LOGINS = 5
LOCK_SECONDS = 10 * 60


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(500, "Sign-in is not configured on the server.")
    return secret


def make_token(user: dict) -> str:
    now = int(time.time())
    payload = {"sub": user["id"], "bid": user["business_id"], "role": user["role"],
               "tv": user.get("token_version", 0), "iat": now, "exp": now + TOKEN_DAYS * 86400}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def verify_google_id_token(token: str) -> dict:
    """Check a Google sign-in token; returns its claims (sub, email, name). Tests replace this."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    client_ids = [c.strip() for c in os.environ.get("GOOGLE_CLIENT_IDS", "").split(",") if c.strip()]
    if not client_ids:
        raise HTTPException(503, "Google sign-in is not available yet.")
    try:
        info = id_token.verify_oauth2_token(token, google_requests.Request(), audience=client_ids)
    except ValueError as e:
        raise HTTPException(401, "Google sign-in failed. Please try again.") from e
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Google sign-in failed. Please try again.")
    return info


def is_admin(user: dict) -> bool:
    admins = {normalize_in_phone(p) for p in os.environ.get("ADMIN_PHONES", "").split(",")}
    return bool(user.get("phone")) and user["phone"] in admins


def can(user: dict, perm: str) -> bool:
    if perm == "admin":
        return is_admin(user)
    if user["role"] == "owner":
        return True
    if perm == "owner":
        return False
    return bool((user.get("permissions") or {}).get(perm))


async def current_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please log in.")
    try:
        claims = jwt.decode(authorization[7:].strip(), _secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(401, "Your session has expired. Please log in again.") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, "Please log in.") from e
    user = await get_db().users.find_one({"id": claims.get("sub")}, {"_id": 0})
    if not user or user.get("token_version", 0) != claims.get("tv"):
        raise HTTPException(401, "Please log in again.")
    if not user.get("active", True):
        raise HTTPException(403, "This login has been turned off. Please contact the shop owner.")
    observability.bind_user(user)  # business_id and user_id on this request's log lines and errors
    return user


def require(*perms: str):
    """Dependency: the logged-in user must have at least one of `perms`."""
    async def dep(user: dict = Depends(current_user)) -> dict:
        if not any(can(user, p) for p in perms):
            raise HTTPException(403, "You do not have permission for this. Please ask the shop owner.")
        return user
    return dep


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
