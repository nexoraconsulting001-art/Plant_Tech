"""Sign up, log in (phone + password, or Google for owners), current user, change password.

Limits against guessing and scripts (ratelimit.py; 429 with Retry-After): per IP on sign-up, login and Google
sign-in, per phone number on login (bursts) and per login on password changes. Wrong passwords make that phone
number wait longer each time (auth.py), never a hard lock. Logins, failed logins, waits and password changes
go to the audit log.

The account routes (routers/account.py, DELETE /api/account) are served through this module's `router`.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

import audit
import auth
import ratelimit
from common import new_id, normalize_in_phone, now_iso
from db import get_db, in_transaction
from routers import account, notifications
from settings import get_settings

LOGIN_IP_RATE = "30/minute"
LOGIN_PHONE_RATE = "10/minute"  # bursts on one number; the growing wait in auth.py does the rest
REGISTER_IP_RATE = "10/hour"
GOOGLE_IP_RATE = "30/minute"
PASSWORD_RATE = "10/hour"  # per login: guesses of the current password

routes = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str
    password: str = Field(min_length=8, max_length=128)
    otp: str | None = Field(None, max_length=10)  # decision D8: not checked yet (see _check_phone_otp)


class LoginIn(BaseModel):
    phone: str
    password: str = Field(min_length=1, max_length=128)


class GoogleIn(BaseModel):
    id_token: str = Field(max_length=4096)


class PasswordIn(BaseModel):
    current_password: str = Field("", max_length=128)
    new_password: str = Field(min_length=1, max_length=128)  # owners need 8+, employees 6+, checked below


def _check_phone_otp(phone: str, otp: str | None):
    """Hook for decision D8 (which SMS OTP provider). Nothing is checked while OTP_REQUIRED is off, and it must
    stay off while 1.1.0 (which cannot send a code) is in use. Switched on before a provider is wired in here,
    sign-ups are refused rather than let through unchecked."""
    if get_settings().otp_required:
        raise HTTPException(503, "Phone verification is not available yet.")


async def _new_owner(db, *, name: str, phone: str | None = None, password: str | None = None,
                     google_sub: str | None = None, email: str | None = None, is_admin: bool = False) -> dict:
    """A new owner gets a new, empty shop; the setup wizard fills it in. Raises DuplicateKeyError when the phone
    or Google account already has a login (unique indexes, migration 0802)."""
    business = {"id": new_id(), "name": "", "address": "", "phone": phone or "", "gst_registered": False,
                "gstin": "", "upi_id": "", "upi_ids": [], "default_upi": None, "whatsapp_bill": True,
                "logo_path": None, "footer_note": "", "currency": "₹",
                "onboarding_done": False, "profit_percent": 60, "created_at": now_iso()}
    user = {"id": new_id(), "business_id": business["id"], "role": "owner", "name": name.strip(),
            "phone": phone, "email": email, "google_sub": google_sub,
            "password_hash": await auth.hash_password_async(password) if password else None,
            "permissions": {}, "active": True, "token_version": 0, "is_admin": is_admin,
            "created_at": now_iso(), "last_login_at": now_iso()}

    async def insert(session):
        await db.businesses.insert_one(dict(business), session=session)
        await db.users.insert_one(dict(user), session=session)

    await in_transaction(insert, db)
    return user


async def _session(db, user: dict, business: dict | None = None, *, deletion_cancelled: bool = False) -> dict:
    if business is None:
        business = await db.businesses.find_one({"id": user["business_id"]}, {"_id": 0})
    out = {"token": auth.make_token(user), "user": auth.public_user(user, business)}
    if deletion_cancelled:
        out["deletion_cancelled"] = True
    return out


async def _logged_in(db, user: dict, method: str) -> dict:
    """A right password or Google sign-in: the shop's deletion check, then the session."""
    business = await db.businesses.find_one({"id": user["business_id"]}, {"_id": 0})
    cancelled = await account.on_login(db, user, business)
    await db.users.update_one({"id": user["id"]}, {"$set": {"last_login_at": now_iso()}})
    await audit.log(db, user, "auth.login", {"type": "user", "id": user["id"]}, {"method": method})
    return await _session(db, user, business, deletion_cancelled=cancelled)


@routes.post("/register", dependencies=[Depends(ratelimit.hit(ratelimit.by_ip, REGISTER_IP_RATE, scope="register-ip"))])
async def register(body: RegisterIn):
    db = get_db()
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
    _check_phone_otp(phone, body.otp)
    taken = HTTPException(409, "An account with this phone number already exists. Please log in instead.")
    if await db.users.find_one({"phone": phone}, {"_id": 1}):
        raise taken
    try:
        user = await _new_owner(db, name=body.name, phone=phone, password=body.password)
    except DuplicateKeyError:  # the same number signing up twice at once
        raise taken from None
    await audit.log(db, user, "auth.register", {"type": "user", "id": user["id"]}, {"method": "phone"})
    return await _session(db, user)


@routes.post("/login", dependencies=[Depends(ratelimit.hit(ratelimit.by_ip, LOGIN_IP_RATE, scope="login-ip"))])
async def login(body: LoginIn):
    db = get_db()
    wrong = HTTPException(401, "Incorrect phone number or password.")
    phone = normalize_in_phone(body.phone)
    if not phone:
        raise wrong
    await ratelimit.consume(f"login-phone:{phone}", LOGIN_PHONE_RATE)
    if wait := await auth.login_wait(db, phone):
        raise auth.too_many_tries(wait)
    user = await db.users.find_one({"phone": phone}, {"_id": 0})
    # Same work for a number with no account (a throwaway hash), so the answer time gives nothing away.
    if not await auth.check_password_async(body.password, (user or {}).get("password_hash")):
        failed, wait = await auth.login_failed(db, phone)
        if user:
            target = {"type": "user", "id": user["id"]}
            await audit.log(db, None, "auth.login_failed", target, {"failed": failed}, business_id=user["business_id"])
            if wait:
                await audit.log(db, None, "auth.login_wait", target, {"failed": failed, "seconds": wait},
                                business_id=user["business_id"])
        raise wrong
    if not user.get("active", True):
        raise HTTPException(403, "This login has been turned off. Please contact the shop owner.")
    await auth.clear_login_failures(db, phone)
    return await _logged_in(db, user, "phone")


@routes.post("/google", dependencies=[Depends(ratelimit.hit(ratelimit.by_ip, GOOGLE_IP_RATE, scope="google-ip"))])
async def google(body: GoogleIn):
    """Owners only. First Google sign-in creates the account and an empty shop."""
    db = get_db()
    info = await auth.google_claims(body.id_token)
    user = await db.users.find_one({"google_sub": info["sub"]}, {"_id": 0})
    if user is None:
        try:
            user = await _new_owner(db, name=info.get("name") or info.get("email", "Owner").split("@")[0],
                                    google_sub=info["sub"], email=info.get("email"))
            await audit.log(db, user, "auth.register", {"type": "user", "id": user["id"]}, {"method": "google"})
        except DuplicateKeyError:  # the same first sign-in twice at once: the other one made the account
            user = await db.users.find_one({"google_sub": info["sub"]}, {"_id": 0})
    if not user.get("active", True):
        raise HTTPException(403, "This login has been turned off.")
    return await _logged_in(db, user, "google")


@routes.get("/me")
async def me(user: dict = Depends(auth.current_user)):
    business = await get_db().businesses.find_one({"id": user["business_id"]}, {"_id": 0})
    return auth.public_user(user, business)


@routes.post("/password")
async def change_password(body: PasswordIn, user: dict = Depends(auth.current_user)):
    """Change your own password. Google-only owners can set one without the current password.
    Logs out every other phone using this login."""
    await ratelimit.consume(f"password:{user['id']}", PASSWORD_RATE)
    if user.get("password_hash") and not await auth.check_password_async(body.current_password, user["password_hash"]):
        raise HTTPException(401, "The current password is incorrect.")
    if user["role"] == "owner":
        if len(body.new_password) < auth.MIN_OWNER_PASSWORD:
            raise HTTPException(400, "Please use at least 8 characters.")
    else:
        auth.check_new_pin(body.new_password)
    db = get_db()
    tv = user.get("token_version", 0) + 1
    await db.users.update_one({"id": user["id"]}, {"$set": {
        "password_hash": await auth.hash_password_async(body.new_password), "token_version": tv}})
    auth.forget_user(user["id"])
    await notifications.forget_phones(db, user["id"])  # this phone signs up again (the app does that after this)
    await auth.clear_login_failures(db, user.get("phone"))
    await audit.log(db, user, "auth.password_changed", {"type": "user", "id": user["id"]})
    user = {**user, "token_version": tv, "password_hash": "set"}
    return await _session(db, user)


# server.py includes `router`: the /api/auth routes above and the /api/account routes.
router = APIRouter()
router.include_router(routes)
router.include_router(account.router)
