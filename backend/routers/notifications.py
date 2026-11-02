"""The owner's phone notifications: signing a phone up for them, and the owner's choices (More -> Notifications).

A phone's Expo push token is kept once (`push_tokens`, unique by token) and belongs to the owner who last
signed it up, with the app language on that phone. Only owners can sign a phone up. Removing a token needs
any login, because holding the token proves it is this phone: an employee or another shop logging in on a
phone an owner used stops that owner's notifications there. What is sent is in notify.py, how in push.py.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import auth
import notify
import push
from common import now_iso
from db import get_db

router = APIRouter(prefix="/api", tags=["notifications"])

TOKEN_PATTERN = r"^Expo(nent)?PushToken\[[A-Za-z0-9_\-]{8,200}\]$"
MAX_PHONES = 10  # per owner: older phones are forgotten first


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
    return {"ok": True}


@router.post("/push-tokens/remove")
async def remove_token(body: TokenOut, _user: dict = Depends(auth.current_user)):
    await get_db().push_tokens.delete_many({"token": body.token})
    return {"ok": True}


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
    update = {f"notify.{k}": v for k, v in body.model_dump(exclude_none=True).items()}
    if update:
        await db.businesses.update_one({"id": owner["business_id"]}, {"$set": {**update, "updated_at": now_iso()}})
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
