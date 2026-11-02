"""Sign up, log in, wrong-password waits and rate limits, Google sign-in, tokens, PINs, admins, audit log."""
import asyncio
import importlib
import time
from datetime import datetime, timezone

import bcrypt
import jwt
import pytest
from pymongo.errors import DuplicateKeyError

import auth
import db as db_module
import manage_users
from conftest import Shop, run
from routers import auth as auth_router

PIN = "482913"


def add_worker(shop, phone="9822233445", pin=PIN, **perms):
    r = shop.post("/api/staff", {"name": "Ramesh", "phone": phone, "password": pin,
                                 "permissions": {"sell": True, "stock": True, "customers": True, "reports": False,
                                                 **perms}})
    assert r.status_code == 200, r.text
    login = shop.c.post("/api/auth/login", json={"phone": phone, "password": pin})
    assert login.status_code == 200, login.text
    return r.json()["id"], {"Authorization": f"Bearer {login.json()['token']}"}


def actions():
    return [e["action"] for e in run(db_module.get_db().audit_log.find({}, {"_id": 0}).sort("at", 1).to_list(500))]


def test_register_creates_owner_and_empty_shop(client):
    r = client.post("/api/auth/register", json={"name": "Suresh", "phone": "+91 98111-22334", "password": "secret123"})
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert user["role"] == "owner" and user["phone"] == "+919811122334"
    assert user["business"]["onboarding_done"] is False
    assert all(user["permissions"].values()) and user["is_admin"] is False
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.json()["id"] == user["id"]
    assert client.get("/api/products", headers={"Authorization": f"Bearer {r.json()['token']}"}).json() == []
    assert "auth.register" in actions()


def test_register_rejects_bad_input(client):
    ok = {"name": "A", "phone": "9811122334", "password": "secret123"}
    assert client.post("/api/auth/register", json={**ok, "phone": "12345"}).status_code == 400
    assert client.post("/api/auth/register", json={**ok, "password": "short"}).status_code == 422
    assert client.post("/api/auth/register", json=ok).status_code == 200
    assert client.post("/api/auth/register", json=ok).status_code == 409  # same phone again


def test_wrong_passwords_mean_a_growing_wait_not_a_lock(client):
    Shop(client)
    db = db_module.get_db()
    good = {"phone": "98111 22334", "password": "secret123"}
    assert client.post("/api/auth/login", json=good).status_code == 200
    for _ in range(auth.FREE_TRIES):
        assert client.post("/api/auth/login", json={**good, "password": "nope"}).status_code == 401
    r = client.post("/api/auth/login", json=good)  # waiting, even with the right password
    assert r.status_code == 429 and r.json()["code"] == "login_wait"
    assert 0 < int(r.headers["Retry-After"]) <= auth.FIRST_WAIT
    assert r.json()["key"] == "Too many incorrect attempts. Please try again in {{seconds}} seconds."
    # the wait is over: the right password works and the count starts again
    run(db.login_attempts.update_one({"_id": "+919811122334"}, {"$set": {"wait_until": datetime.now(timezone.utc)}}))
    assert client.post("/api/auth/login", json=good).status_code == 200
    assert run(db.login_attempts.find_one({"_id": "+919811122334"})) is None
    assert {"auth.login_failed", "auth.login_wait", "auth.login"} <= set(actions())


def test_wait_doubles_up_to_a_cap():
    assert [auth.wait_after(n) for n in (1, 4, 5, 6, 7)] == [0, 0, 30, 60, 120]
    assert auth.wait_after(50) == auth.MAX_WAIT
    assert auth.too_many_tries(600).params == {"minutes": 10}


def test_unknown_numbers_look_the_same(client, monkeypatch):
    checks = []
    real = bcrypt.checkpw
    monkeypatch.setattr(bcrypt, "checkpw", lambda pw, h: checks.append(1) or real(pw, h))
    r = client.post("/api/auth/login", json={"phone": "9999999999", "password": "x"})
    assert r.status_code == 401 and r.json()["detail"] == "Incorrect phone number or password."
    assert checks  # a throwaway hash was checked, so the answer took as long as for a real account
    for _ in range(auth.FREE_TRIES - 1):
        client.post("/api/auth/login", json={"phone": "9999999999", "password": "x"})
    assert client.post("/api/auth/login", json={"phone": "9999999999", "password": "x"}).status_code == 429


def test_brute_force_is_throttled_per_phone_and_per_ip(client):
    Shop(client)
    # a script going fast at one number: after 5 wrong ones every try is refused before a password is checked,
    # and past the per-number limit even that check is skipped
    answers = [client.post("/api/auth/login", json={"phone": "9811122334", "password": f"guess{i}"}).json()
               for i in range(12)]
    assert [a.get("code") for a in answers[5:10]] == ["login_wait"] * 5
    assert [a.get("code") for a in answers[10:]] == ["rate_limited"] * 2
    assert auth_router.LOGIN_PHONE_RATE == "10/minute"
    # sign-ups from one address
    codes = [client.post("/api/auth/register", json={"name": "X", "phone": f"98200000{i:02d}", "password": "secret123"})
             for i in range(11)]
    assert [c.status_code for c in codes[:9]] == [200] * 9  # the Shop above was the 1st this hour
    assert codes[-1].status_code == 429 and codes[-1].json()["code"] == "rate_limited"
    assert int(codes[-1].headers["Retry-After"]) > 0


def test_password_checks_run_off_the_event_loop(client, monkeypatch):
    Shop(client)
    on_loop = []
    real = bcrypt.checkpw

    def spy(pw, h):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(pw, h)

    monkeypatch.setattr(bcrypt, "checkpw", spy)
    assert client.post("/api/auth/login", json={"phone": "9811122334", "password": "secret123"}).status_code == 200
    assert on_loop == [False]


def test_too_many_waiting_logins_get_busy(client, monkeypatch):
    monkeypatch.setattr(auth, "MAX_WAITING", 0)
    r = client.post("/api/auth/login", json={"phone": "9811122334", "password": "secret123"})
    assert r.status_code == 503 and r.json()["code"] == "busy" and r.headers["Retry-After"] == "5"


def test_requests_need_a_valid_token(client):
    assert client.get("/api/products").status_code == 401
    assert client.get("/api/products", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/health").json() == {"ok": True}


def _legacy_token(user: dict, **extra) -> str:
    """A token as 1.1.0-era servers made them: no iss, no aud."""
    now = int(time.time())
    payload = {"sub": user["id"], "bid": user["business"]["id"], "role": user["role"], "tv": 0,
               "iat": now, "exp": now + 30 * 86400, **extra}
    return jwt.encode(payload, auth._secret(), algorithm="HS256")


def test_tokens_carry_issuer_and_audience_and_old_ones_still_work(client, monkeypatch):
    shop = Shop(client)
    claims = jwt.decode(shop.token, options={"verify_signature": False})
    assert claims["iss"] == auth.JWT_ISSUER and claims["aud"] == auth.JWT_AUDIENCE
    user = shop.get("/api/auth/me").json()

    def me(token):
        return client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code

    assert me(_legacy_token(user)) == 200  # a 1.1.0 login keeps working
    assert me(_legacy_token(user, aud="someone-else", iss=auth.JWT_ISSUER)) == 401
    assert me(_legacy_token(user, iss="someone-else")) == 401  # half of the new claims is not an old token
    old = int(time.time()) - 31 * 86400
    assert me(_legacy_token(user, iat=old)) == 401  # older than any token we ever issued for 30 days
    monkeypatch.setattr(auth, "JWT_REQUIRE_AUD", True)  # after 1.1.0 is retired
    assert me(_legacy_token(user)) == 401
    assert me(shop.token) == 200


def test_google_sign_in_creates_then_reuses_owner(client, monkeypatch):
    monkeypatch.setattr(auth, "verify_google_id_token",
                        lambda t: {"sub": "g-123", "email": "owner@example.com", "name": "Asha"})
    first = client.post("/api/auth/google", json={"id_token": "x"}).json()
    second = client.post("/api/auth/google", json={"id_token": "x"}).json()
    assert first["user"]["id"] == second["user"]["id"]
    assert first["user"]["name"] == "Asha" and first["user"]["has_password"] is False


def test_google_not_configured_is_clear(client):
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 503


class _Response:
    def __init__(self, headers):
        self.status, self.headers, self.data = 200, headers, b"{}"


def test_google_certificates_are_cached():
    calls = []

    def inner(url, method="GET", **kw):
        calls.append((url, method))
        return _Response({"Cache-Control": "public, max-age=19000, must-revalidate"})

    request = auth.CachingRequest(inner)
    first = request("https://www.googleapis.com/oauth2/v1/certs")
    assert request("https://www.googleapis.com/oauth2/v1/certs") is first
    assert len(calls) == 1
    request("https://example.com/token", method="POST")
    request("https://example.com/token", method="POST")
    assert len(calls) == 3  # only GETs are kept
    assert auth._max_age({}) == 3600 and auth._max_age({"cache-control": "max-age=999999"}) == 86400


def test_password_change_logs_out_old_sessions(client):
    shop = Shop(client)
    r = shop.post("/api/auth/password", {"current_password": "secret123", "new_password": "newsecret1"})
    assert r.status_code == 200
    assert shop.get("/api/auth/me").status_code == 401  # old token no longer works
    new = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/auth/me", headers=new).status_code == 200
    assert client.post("/api/auth/login", json={"phone": "9811122334", "password": "newsecret1"}).status_code == 200
    assert "auth.password_changed" in actions()


def test_new_pins_need_six_digits_old_ones_keep_working(client):
    shop = Shop(client)
    assert shop.post("/api/auth/password", {"current_password": "secret123", "new_password": "1234"}).status_code == 400
    r = shop.post("/api/staff", {"name": "Ramesh", "phone": "9822233445", "password": "4821"})
    assert r.status_code == 400 and r.json()["code"] == "pin_too_short" and r.json()["params"] == {"n": 6}
    assert r.json()["detail"] == "Please use a PIN of at least 6 digits."
    for easy in ("123456", "111111", "121212", "654321"):
        r = shop.post("/api/staff", {"name": "Ramesh", "phone": "9822233445", "password": easy})
        assert r.status_code == 400 and r.json()["code"] == "pin_too_easy", easy
    wid, wh = add_worker(shop)
    r = client.post("/api/auth/password", headers=wh, json={"current_password": PIN, "new_password": "9090"})
    assert r.status_code == 400 and r.json()["code"] == "pin_too_short"
    r = client.post("/api/auth/password", headers=wh, json={"current_password": PIN, "new_password": "730214"})
    assert r.status_code == 200
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "730214"}).status_code == 200
    # a PIN set before the 6-digit rule (4 digits) still logs in
    run(db_module.get_db().users.update_one({"id": wid}, {"$set": {"password_hash": auth.hash_password("4821")}}))
    assert client.post("/api/auth/login", json={"phone": "9822233445", "password": "4821"}).status_code == 200


def test_long_passwords_are_cut_like_before():
    hashed = auth.hash_password("x" * 100)
    assert auth.check_password("x" * 100, hashed) and auth.check_password("x" * 72, hashed)


def test_admin_is_a_flag_not_a_phone_number(client):
    admin = Shop(client, phone="9000000001")  # in ADMIN_PHONES (conftest), which is no longer read
    assert admin.get("/api/auth/me").json()["is_admin"] is False
    assert admin.get("/api/admin/library").status_code == 403
    db = db_module.get_db()
    run(manage_users.set_admin(db, "90000 00001", True))
    assert admin.get("/api/auth/me").json()["is_admin"] is True
    assert admin.get("/api/admin/library").status_code == 200
    assert [u["phone"] for u in run(manage_users.admins(db))] == ["+919000000001"]
    run(manage_users.set_admin(db, "9000000001", False))
    assert admin.get("/api/admin/library").status_code == 403
    assert actions().count("admin.grant") == 1 and "admin.revoke" in actions()
    with pytest.raises(LookupError):
        run(manage_users.set_admin(db, "9811100000", True))


def test_admin_migration_flags_admin_phones_once(client):
    Shop(client, phone="9000000001")
    Shop(client, phone="9811122334")
    db = db_module.get_db()
    migration = importlib.import_module("migrations.0801_admin_flag")
    run(migration.up(db))
    run(migration.up(db))
    admins = run(db.users.find({"is_admin": True}, {"_id": 0, "phone": 1}).to_list(10))
    assert admins == [{"phone": "+919000000001"}]
    assert actions().count("admin.grant") == 1


def test_one_login_per_phone_and_google_account(client):
    db = db_module.get_db()
    shop = Shop(client)
    user = shop.get("/api/auth/me").json()
    with pytest.raises(DuplicateKeyError):
        run(db.users.insert_one({"id": "x", "phone": "+919811122334"}))
    run(db.users.insert_many([{"id": "g1", "phone": None, "google_sub": "g-1"}, {"id": "g2", "phone": None}]))
    with pytest.raises(DuplicateKeyError):
        run(db.users.insert_one({"id": "g3", "phone": None, "google_sub": "g-1"}))
    assert user["phone"] == "+919811122334"


def test_unique_index_migration_moves_duplicates_aside(client):
    db = db_module.get_db()
    run(db.users.drop_index("phone_1"))
    run(db.users.insert_many([
        {"id": "old", "business_id": "b1", "phone": "+919811122334", "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "new", "business_id": "b2", "phone": "+919811122334", "created_at": "2026-02-01T00:00:00+00:00"},
    ]))
    run(importlib.import_module("migrations.0802_users_unique").up(db))
    kept = run(db.users.find_one({"id": "old"}))
    moved = run(db.users.find_one({"id": "new"}))
    assert kept["phone"] == "+919811122334"
    assert "phone" not in moved and moved["phone_duplicate"] == "+919811122334"
    index = run(db.users.index_information())["phone_1"]
    assert index["unique"] and index["partialFilterExpression"] == {"phone": {"$type": "string"}}
    assert "user.dedupe" in actions()


def test_logins_are_cached_but_changes_apply_at_once(client):
    shop = Shop(client)
    wid, wh = add_worker(shop)
    assert client.post("/api/products", headers=wh, json={"name": "Rose", "selling_price": 100}).status_code == 200
    r = client.patch(f"/api/staff/{wid}", headers=shop.h, json={"permissions": {"sell": True, "stock": False}})
    assert r.status_code == 200
    assert client.post("/api/products", headers=wh, json={"name": "Lily", "selling_price": 100}).status_code == 403
    assert auth._users.get(wid) is None or auth._users[wid]["permissions"]["stock"] is False


def test_otp_is_a_hook_until_decided(client, monkeypatch):
    from settings import get_settings

    monkeypatch.setattr(get_settings(), "otp_required", True)
    r = client.post("/api/auth/register", json={"name": "A", "phone": "9811122334", "password": "secret123"})
    assert r.status_code == 503  # switched on before a provider exists: refuses, never lets through unchecked
