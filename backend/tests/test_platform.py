"""Platform: settings checks, migrations, health and config routes, route order, logs, Sentry, metrics, the
transaction helper, jobs, rate limits and the audit log."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sentry_sdk
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo import AsyncMongoClient, monitoring
from sentry_sdk.transport import Transport

import audit
import db as db_module
import jobs
import migrations
import observability
import ratelimit
import server
from conftest import MOCK, MONGO_URL, TEST_DB, Shop, empty, run
from errors import AppError, app_error_handler
from routers import reports, sales
from settings import Settings

BACKEND = Path(__file__).resolve().parent.parent
PROD = dict(env="production", jwt_secret="x" * 32, storage_backend="r2", r2_account_id="acc", r2_access_key_id="key",
            r2_secret_access_key="secret", r2_bucket="photos", public_base_url="https://bill.example.com",
            auto_migrate=False)
needs_mongo = pytest.mark.skipif(MOCK, reason="needs a real MongoDB")


def _python(*args, **env):
    full = {**os.environ, "AUTO_MIGRATE": "0", **env}
    return subprocess.run([sys.executable, *args], cwd=BACKEND, env=full, capture_output=True, text=True, timeout=120)


# --- settings ----------------------------------------------------------------------------------------------
def test_production_settings_are_checked():
    Settings(**PROD)
    for bad, says in [({"jwt_secret": "short"}, "JWT_SECRET"), ({"storage_backend": "local"}, "STORAGE_BACKEND"),
                      ({"r2_bucket": ""}, "R2_BUCKET"), ({"public_base_url": "http://bill.example.com"}, "https"),
                      ({"cors_origins": "*"}, "CORS_ORIGINS"), ({"auto_migrate": True}, "AUTO_MIGRATE")]:
        with pytest.raises(ValidationError, match=says):
            Settings(**{**PROD, **bad})
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(env="staging", jwt_secret="short", auto_migrate=False)
    prod, dev = Settings(**PROD), Settings(env="dev", jwt_secret="", auto_migrate=True)
    assert prod.cors_allow_origins == [] and not prod.docs_enabled
    assert dev.cors_allow_origins == ["*"] and dev.docs_enabled


def test_settings_read_the_vm_variable_names(monkeypatch):
    monkeypatch.delenv("MONGO_DB")
    monkeypatch.setenv("DB_NAME", "nurserypos")
    monkeypatch.setenv("CLAUDE_MODEL", "model-a")
    monkeypatch.setenv("CLAUDE_MODEL_PLANT", "model-b")
    monkeypatch.setenv("PUSH_ENABLED", "1")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("GOOGLE_CLIENT_IDS", "web.apps,android.apps")
    s = Settings()
    assert s.mongo_db == "nurserypos" and s.push_enabled and s.google_client_ids == ["web.apps", "android.apps"]
    assert s.model_for("plant") == "model-b" and s.model_for("invoice") == s.model_for("names") == "model-a"
    assert s.cors_allow_origins == ["https://a.example", "https://b.example"]


def test_bad_production_settings_stop_the_process():
    r = _python("-m", "settings", ENV="production", JWT_SECRET="short", STORAGE_BACKEND="local")
    assert r.returncode != 0
    assert "JWT_SECRET" in r.stderr and "STORAGE_BACKEND must be r2" in r.stderr
    good = {k.upper(): v for k, v in PROD.items() if k not in ("env", "auto_migrate")}
    assert _python("-m", "settings", ENV="production", **good).returncode == 0


def test_production_hides_the_api_docs():
    good = {k.upper(): v for k, v in PROD.items() if k not in ("env", "auto_migrate")}
    r = _python("-c", "import server; print(server.app.openapi_url, server.app.docs_url)", ENV="production", **good)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "None None"


# --- migrations and start-up -------------------------------------------------------------------------------
def test_migrations_are_applied_once(client):
    db = db_module.state.db
    assert run(migrations.up(db)) == []  # start-up (AUTO_MIGRATE) applied them all
    assert all(row["applied_at"] for row in run(migrations.status(db)))
    library = run(db.library.count_documents({}))
    run(db.migrations.delete_many({}))  # as if never recorded: running them again changes nothing
    assert run(migrations.up(db)) == [m.id for m in migrations.discover()]
    assert run(migrations.up(db)) == []
    assert run(db.library.count_documents({})) == library
    assert run(db.migration_lock.count_documents({})) == 0


def test_one_migration_runner_at_a_time(client):
    db = db_module.state.db
    now = datetime.now(timezone.utc)
    run(db.migrations.delete_many({}))
    run(db.migration_lock.insert_one({"_id": "lock", "owner": "other", "at": now, "expires_at": now + timedelta(minutes=5)}))
    with pytest.raises(migrations.LockBusy):
        run(migrations.up(db, wait=0))
    run(db.migration_lock.update_one({"_id": "lock"}, {"$set": {"expires_at": now - timedelta(seconds=1)}}))
    assert run(migrations.up(db, wait=0))  # the dead runner's lock is taken over
    assert run(db.migration_lock.count_documents({})) == 0


@needs_mongo
def test_migrations_command(client):
    r = _python("-m", "migrations", "up")
    assert r.returncode == 0, r.stderr
    assert "all up to date" in r.stdout
    status = _python("-m", "migrations", "status")
    assert status.returncode == 0 and "0001_baseline" in status.stdout and "pending" not in status.stdout


@needs_mongo
def test_start_up_writes_nothing(mongo, monkeypatch):
    class Commands(monitoring.CommandListener):
        def __init__(self):
            self.names = []

        def started(self, event):
            self.names.append(event.command_name)

        def succeeded(self, event):
            pass

        def failed(self, event):
            pass

    empty(mongo)
    with TestClient(server.app):  # a deploy ran the migrations
        pass
    seen = Commands()
    monkeypatch.setattr(server.settings, "auto_migrate", False)
    db_module.state.client = AsyncMongoClient(MONGO_URL, event_listeners=[seen], **db_module.client_options())
    db_module.state.db = db_module.state.client[TEST_DB]
    with TestClient(server.app) as c:
        assert c.get("/livez").status_code == 200
    writes = {"insert", "update", "delete", "findAndModify", "createIndexes", "drop", "dropDatabase", "create"}
    assert not writes & set(seen.names), seen.names


# --- health, config, routes --------------------------------------------------------------------------------
def test_health_routes_and_app_config(client):
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/livez").json() == {"ok": True}
    assert client.get("/readyz").json() == {"ok": True, "checks": {"mongo": "ok", "storage": "ok"}}
    config = client.get("/api/config").json()
    assert config["min_app_version"] == "1.1.0"
    assert config["features"]["bill_without_gst"] is True and config["features"]["otp_required"] is False


@needs_mongo
def test_readyz_is_503_when_mongo_is_down(client):
    real = db_module.state.db
    dead = AsyncMongoClient("mongodb://127.0.0.1:1/", serverSelectionTimeoutMS=300, timeoutMS=500)
    db_module.state.db = dead["nothing"]
    try:
        r = client.get("/readyz")
    finally:
        db_module.state.db = real
        run(dead.close())
    assert r.status_code == 503 and r.json()["checks"]["mongo"] == "down"


def test_invoices_summary_is_not_taken_for_a_bill_id(client):
    included = [getattr(r, "original_router", None) for r in server.app.routes]
    assert included.index(reports.router) < included.index(sales.router)
    r = Shop(client).get("/api/invoices/summary")
    assert r.status_code == 200 and {"orders", "sales", "by_method", "by_day"} <= set(r.json())


def test_metrics_only_for_the_vm(client, monkeypatch):
    assert client.get("/metrics").status_code == 404  # the test client is not on the VM
    monkeypatch.setattr(server.settings, "metrics_token", "m" * 24)
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    r = client.get("/metrics", headers={"Authorization": "Bearer " + "m" * 24})
    assert r.status_code == 200 and "http_requests_total" in r.text
    assert observability._local("127.0.0.1") and observability._local("172.17.0.1") and not observability._local("8.8.8.8")


def test_responses_carry_a_request_id(client):
    r = client.get("/api/")
    assert len(r.headers["x-request-id"]) == 32
    sent = "0b8f5a3e9c1d4f2a8b7c6d5e4f3a2b1c"
    assert client.get("/api/", headers={"X-Request-ID": sent}).headers["x-request-id"] == sent


# --- logs and Sentry ---------------------------------------------------------------------------------------
def test_logs_are_json_with_request_and_login(client, capsys):
    shop = Shop(client)
    observability.setup_logging(Settings(log_format="json"))
    try:
        capsys.readouterr()
        shop.get("/api/products")
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    finally:
        observability.setup_logging()
    line = [x for x in lines if x.get("message") == "request"][-1]
    assert line["route"] == "/api/products" and line["status"] == 200 and line["level"] == "info"
    assert len(line["request_id"]) == 32 and line["business_id"] and line["user_id"]


def test_errors_reach_sentry_with_the_release():
    events = []
    phone, secret = "98111" + "22334", "top-" + "secret"  # not written out: Sentry sends the source lines around the error

    class Capture(Transport):
        def capture_envelope(self, envelope):
            if (event := envelope.get_event()) is not None:
                events.append(event)

    s = Settings(env="staging", jwt_secret="x" * 32, auto_migrate=False, git_sha="abc1234",
                 sentry_dsn="https://public@o0.ingest.sentry.io/1")
    assert observability.init_sentry(s, transport=Capture)
    try:
        app = FastAPI()
        app.add_middleware(observability.RequestContextMiddleware)

        @app.get("/boom/{token}")
        async def boom(token: str, phone: str):
            raise RuntimeError(f"bill for {phone[:5]} {phone[5:]} failed")  # personal data in the message

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get(f"/boom/x?phone={phone}", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 500
        sentry_sdk.flush()
    finally:
        sentry_sdk.init()  # Sentry off again for the other tests
    event = events[0]
    assert event["release"] == "abc1234" and event["environment"] == "staging"
    sent = json.dumps(event, default=str)
    assert phone not in sent and f"{phone[:5]} {phone[5:]}" not in sent and secret not in sent


# --- transactions, jobs, rate limits, audit ----------------------------------------------------------------
@needs_mongo
def test_transaction_is_all_or_nothing(client):
    db = db_module.state.db

    async def go():
        async def two(session):
            await db.txn_check.insert_one({"n": 1}, session=session)
            await db.txn_check.insert_one({"n": 2}, session=session)
            return "saved"

        async def broken(session):
            await db.txn_check.insert_one({"n": 3}, session=session)
            raise RuntimeError("stop half-way")

        assert await db_module.supports_transactions(db)
        assert await db_module.in_transaction(two) == "saved"
        with pytest.raises(RuntimeError):
            await db_module.in_transaction(broken)
        return sorted([d["n"] async for d in db.txn_check.find({})])
    assert run(go()) == [1, 2]


def test_transaction_helper_without_a_replica_set():
    class Standalone:  # e.g. mongomock: no sessions
        pass

    async def callback(session):
        return session

    assert run(db_module.in_transaction(callback, Standalone())) is None


def test_jobs_run_in_the_process_without_redis(client):
    seen = []

    @jobs.job
    async def remember(ctx, value):
        seen.append((value, ctx["db"] is not None))

    async def go():
        await jobs.enqueue("remember", 5)
        await jobs.drain()
    try:
        run(go())
    finally:
        jobs.REGISTRY.pop("remember")
    assert seen == [(5, True)]
    with pytest.raises(KeyError):
        run(jobs.enqueue("no_such_job"))


def test_rate_limit_answers_429_with_retry_after(client):
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)

    @app.post("/try", dependencies=[Depends(ratelimit.hit(ratelimit.by_ip, "2/minute", scope="try"))])
    async def attempt():
        return {"ok": True}

    with TestClient(app) as c:
        assert [c.post("/try").status_code for _ in range(2)] == [200, 200]
        r = c.post("/try")
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 61
    assert r.json()["code"] == "rate_limited" and r.json()["params"] == {"seconds": int(r.headers["retry-after"])}


def test_audit_log(client):
    shop = Shop(client)
    me = shop.get("/api/auth/me").json()
    db = db_module.state.db
    user = {"id": me["id"], "business_id": me["business"]["id"], "role": "owner"}
    run(audit.log(db, user, "staff.disable", {"type": "user", "id": "u1"}, {"reason": "left"}))
    entry = run(db.audit_log.find_one({"action": "staff.disable"}, {"_id": 0}))
    assert entry["action"] == "staff.disable" and entry["business_id"] == user["business_id"]
    assert entry["actor"] == {"id": me["id"], "role": "owner"} and entry["target"]["id"] == "u1"
