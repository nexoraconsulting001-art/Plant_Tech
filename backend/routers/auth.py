"""Sign up, log in (phone + password, or Google for owners), current user, change password."""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
from common import new_id, normalize_in_phone, now_iso
from db import get_db
from routers import notifications

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    phone: str
    password: str = Field(min_length=1, max_length=128)


class GoogleIn(BaseModel):
    id_token: str


class PasswordIn(BaseModel):
    current_password: str = ""
    new_password: str = Field(min_length=4, max_length=128)  # owners need 8+, checked below


async def _new_owner(db, *, name: str, phone: str | None = None, password: str | None = None,
                     google_sub: str | None = None, email: str | None = None) -> dict:
    """A new owner gets a new, empty shop; the setup wizard fills it in."""
    business = {"id": new_id(), "name": "", "address": "", "phone": phone or "", "gst_registered": False,
                "gstin": "", "upi_id": "", "upi_ids": [], "default_upi": None, "whatsapp_bill": True,
                "logo_path": None, "footer_note": "", "currency": "₹",
                "onboarding_done": False, "profit_percent": 60, "created_at": now_iso()}
    user = {"id": new_id(), "business_id": business["id"], "role": "owner", "name": name.strip(),
            "phone": phone, "email": email, "google_sub": google_sub,
            "password_hash": auth.hash_password(password) if password else None,
            "permissions": {}, "active": True, "token_version": 0,
            "failed_logins": 0, "locked_until": 0, "created_at": now_iso(), "last_login_at": now_iso()}
    await db.businesses.insert_one(business)
    await db.users.insert_one(user)
    return user


async def _session(db, user: dict) -> dict:
    business = await db.businesses.find_one({"id": user["business_id"]}, {"_id": 0})
    return {"token": auth.make_token(user), "user": auth.public_user(user, business)}


@router.post("/register")
async def register(body: RegisterIn):
    db = get_db()
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
    if await db.users.find_one({"phone": phone}):
        raise HTTPException(409, "An account with this phone number already exists. Please log in instead.")
    user = await _new_owner(db, name=body.name, phone=phone, password=body.password)
    return await _session(db, user)


@router.post("/login")
async def login(body: LoginIn):
    db = get_db()
    phone = normalize_in_phone(body.phone)
    user = await db.users.find_one({"phone": phone}, {"_id": 0}) if phone else None
    wrong = HTTPException(401, "Incorrect phone number or password.")
    if not user:
        raise wrong
    now = time.time()
    if user.get("locked_until", 0) > now:
        raise HTTPException(429, "Too many incorrect attempts. Please wait 10 minutes, or ask the shop owner to reset your PIN.")
    if not user.get("active", True):
        raise HTTPException(403, "This login has been turned off. Please contact the shop owner.")
    if not auth.check_password(body.password, user.get("password_hash")):
        failed = user.get("failed_logins", 0) + 1
        update = {"failed_logins": failed}
        if failed >= auth.MAX_FAILED_LOGINS:
            update = {"failed_logins": 0, "locked_until": now + auth.LOCK_SECONDS}
        await db.users.update_one({"id": user["id"]}, {"$set": update})
        raise wrong
    await db.users.update_one({"id": user["id"]},
                              {"$set": {"failed_logins": 0, "locked_until": 0, "last_login_at": now_iso()}})
    return await _session(db, user)


@router.post("/google")
async def google(body: GoogleIn):
    """Owners only. First Google sign-in creates the account and an empty shop."""
    db = get_db()
    info = auth.verify_google_id_token(body.id_token)
    user = await db.users.find_one({"google_sub": info["sub"]}, {"_id": 0})
    if user is None:
        user = await _new_owner(db, name=info.get("name") or info.get("email", "Owner").split("@")[0],
                                google_sub=info["sub"], email=info.get("email"))
    elif not user.get("active", True):
        raise HTTPException(403, "This login has been turned off.")
    await db.users.update_one({"id": user["id"]}, {"$set": {"last_login_at": now_iso()}})
    return await _session(db, user)


@router.get("/me")
async def me(user: dict = Depends(auth.current_user)):
    business = await get_db().businesses.find_one({"id": user["business_id"]}, {"_id": 0})
    return auth.public_user(user, business)


@router.post("/password")
async def change_password(body: PasswordIn, user: dict = Depends(auth.current_user)):
    """Change your own password. Google-only owners can set one without the current password.
    Logs out every other phone using this login."""
    if user.get("password_hash") and not auth.check_password(body.current_password, user["password_hash"]):
        raise HTTPException(401, "The current password is incorrect.")
    if user["role"] == "owner" and len(body.new_password) < 8:
        raise HTTPException(400, "Please use at least 8 characters.")
    db = get_db()
    tv = user.get("token_version", 0) + 1
    await db.users.update_one({"id": user["id"]},
                              {"$set": {"password_hash": auth.hash_password(body.new_password), "token_version": tv}})
    await notifications.forget_phones(db, user["id"])  # this phone signs up again (the app does that after this)
    user = {**user, "token_version": tv, "password_hash": "set"}
    return await _session(db, user)
