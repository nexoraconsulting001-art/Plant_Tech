"""The owner's phone notifications: signing a phone up for them, and the owner's choices (More -> Notifications).

A phone's Expo push token is kept once (`push_tokens`, unique by token) and belongs to the owner who last
signed it up, with the app language on that phone. Only owners can sign a phone up. Signing up, and changing
the summary settings, also keeps the shop's row in `summary_schedule` (notify.py) up to date.

Removing a token needs a login: the caller's own phone, or a phone of the caller's shop (an employee logging
in on a phone an owner used stops that owner's notifications there). One narrow exception for apps already in
testers' hands, whose logout sends the old login even when it has expired: a login that has expired (or was
ended by a new password) but is genuinely ours still removes that same person's phones, for LEGACY_GRACE after
it expired. PUSH_REMOVE_LEGACY=0 turns the exception off once those apps are retired. The answer is always
{"ok": true} (plus how many were removed) when a login was sent, so the app's logout never sees an error;
no login at all is a 401.
A new password (or an employee switched off) forgets all of that person's phones; the phone the owner changed
it on signs up again. What is sent is in notify.py, how in push.py.
"""
import os
import time

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import auth
import notify
import push
from common import now_iso
from db import get_db

router = APIRouter(prefix="/api", tags=["notifications"])

TOKEN_PATTERN = r"^Expo(nent)?PushToken\[[A-Za-z0-9_\-]{8,200}\]$"
MAX_PHONES = 10  # per owner: older phones are forgotten first
LEGACY_GRACE = 180 * 86400  # seconds after a login expired that it still removes its own phones


class TokenIn(BaseModel):
    token: str = Field(pattern=TOKEN_PATTERN)
    lang: str = Field("en", pattern=r"^(en|hi)$")
    platform: str = Field("", max_length=20)


class TokenOut(BaseModel):
    token: str = Field(max_length=300)


class SettingsIn(BaseModel):
    low_stock: bool | None = None
    summary: bool | None = None
    summary_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # India time, 24-hour


@router.post("/push-tokens")
async def add_token(body: TokenIn, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    now = now_iso()
    await db.push_tokens.update_one(
        {"token": body.token},
        {"$set": {"user_id": owner["id"], "business_id": owner["business_id"], "lang": body.lang,
                  "platform": body.platform, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True)
    mine = await db.push_tokens.find({"user_id": owner["id"]}, {"_id": 0, "token": 1}).sort("updated_at", -1).to_list(100)
    if len(mine) > MAX_PHONES:
        await db.push_tokens.delete_many({"token": {"$in": [t["token"] for t in mine[MAX_PHONES:]]}})
    await notify.schedule(db, owner["business_id"], keep=True)
    return {"ok": True}


def legacy_remove_allowed() -> bool:
    return os.environ.get("PUSH_REMOVE_LEGACY", "1").strip().lower() not in ("0", "false", "no", "off")


def _our_expired_login(token: str) -> str | None:
    """The user id of a login this server signed that is no longer valid (expired, or ended by a new password),
    within LEGACY_GRACE of its expiry. None for anything not signed by us."""
    try:
        claims = jwt.decode(token, auth._secret(), algorithms=["HS256"],
                            options={"verify_exp": False, "verify_aud": False})
    except jwt.InvalidTokenError:
        return None
    if not isinstance(claims.get("sub"), str) or time.time() - float(claims.get("exp") or 0) > LEGACY_GRACE:
        return None
    return claims["sub"]


@router.post("/push-tokens/remove")
async def remove_token(body: TokenOut, authorization: str | None = Header(None)):
    """Take a phone off (logging out, or someone else logged in on it). Needs a login; see the module notes."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please log in.")
    db = get_db()
    try:
        user = await auth.current_user(authorization)
    except HTTPException as e:
        if e.status_code not in (401, 403):
            raise
        user = None
    if user is not None:
        match = {"token": body.token, "$or": [{"user_id": user["id"]}, {"business_id": user["business_id"]}]}
    else:
        user_id = _our_expired_login(authorization[7:].strip())
        if user_id is None:
            raise HTTPException(401, "Please log in.")
        if not legacy_remove_allowed():
            return {"ok": True, "removed": 0}
        match = {"token": body.token, "user_id": user_id}
    removed = (await db.push_tokens.delete_many(match)).deleted_count
    return {"ok": True, "removed": removed}


async def forget_phones(db, user_id: str):
    """Logged out everywhere (new password, employee switched off): none of their phones get notifications."""
    await db.push_tokens.delete_many({"user_id": user_id})


async def _settings_view(db, owner: dict) -> dict:
    s = await notify.shop_settings(db, owner["business_id"])
    phones = await db.push_tokens.count_documents({"user_id": owner["id"]})
    return {**s, "phones": phones, "push_on": push.enabled()}


@router.get("/notifications/settings")
async def get_settings(owner: dict = Depends(auth.require("owner"))):
    """`phones`: how many of this owner's phones get notifications. `push_on`: the server sends them at all."""
    return await _settings_view(get_db(), owner)


@router.put("/notifications/settings")
async def update_settings(body: SettingsIn, owner: dict = Depends(auth.require("owner"))):
    db = get_db()
    changes = body.model_dump(exclude_none=True)
    update = {f"notify.{k}": v for k, v in changes.items()}
    if update:
        await db.businesses.update_one({"id": owner["business_id"]}, {"$set": {**update, "updated_at": now_iso()}})
    if {"summary", "summary_time"} & changes.keys():
        await notify.schedule(db, owner["business_id"])
    return await _settings_view(db, owner)


@router.post("/notifications/test")
async def test_notification(owner: dict = Depends(auth.require("owner"))):
    """A test notification to this owner's own phones. `sent` = phones it went to (0 when push is off)."""
    db = get_db()
    tokens = await db.push_tokens.find({"user_id": owner["id"]}, {"_id": 0}).to_list(MAX_PHONES)
    if not push.enabled():
        return {"sent": 0, "phones": len(tokens), "push_on": False}
    sent = await push.send_to_owners(db, owner["business_id"], notify.test_message, {"type": "test"}, tokens)
    return {"sent": sent, "phones": len(tokens), "push_on": True}
