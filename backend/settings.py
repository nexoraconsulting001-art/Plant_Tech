"""Server settings, read once from environment variables (and backend/.env when there is one).

Every setting has a plain upper-case environment variable of the same name (`MONGO_URL`, `REDIS_URL`, ...).
The names the VM's deploy/.env already uses keep working: `DB_NAME` (= `MONGO_DB`), `CLAUDE_MODEL`,
`PUSH_ENABLED=1`, `STORAGE_BACKEND`, `R2_*`.

ENV is dev (a laptop, tests), staging or production. Staging and production refuse to start on settings that
would only fail later (a short JWT_SECRET; in production also photos on local disk, missing R2 keys, a
non-https PUBLIC_BASE_URL or CORS open to every site): `python -m settings` checks them and exits non-zero,
and gunicorn.conf.py runs the same check before it starts any worker.

Other modules still read a few variables straight from os.environ (ai_client, auth, push, storage_client);
they read the same names, so moving them here later changes nothing for the VM.

Owner decisions still open (docs/PRODUCTION-PLAN.md section 4) are flags here, set to today's behaviour:
BILL_WITHOUT_GST (D6), OTP_REQUIRED (D8), INVOICE_NUMBER_SCHEME (D7), the per-route Claude models (D5).
"""
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/.env (a laptop): into os.environ, so the modules that read os.environ directly see it too.
load_dotenv(Path(__file__).parent / ".env")

DEFAULT_MODEL = "claude-sonnet-5-5"
Env = Literal["dev", "staging", "production"]


def _csv(value) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value or [])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, populate_by_name=True)

    env: Env = Field("dev", validation_alias=AliasChoices("ENV", "APP_ENV"))
    git_sha: str = ""  # the release, for Sentry and logs (set by the image build or the deploy)

    # MongoDB. Staging: the compose replica set; production: an Atlas mongodb+srv:// URL with its own user.
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = Field("nurserypos", validation_alias=AliasChoices("MONGO_DB", "DB_NAME"))
    mongo_max_pool_size: int = Field(25, ge=1, le=500)  # per process; 4 workers x 25 per VM
    mongo_server_selection_timeout_ms: int = Field(5000, ge=100)
    # Whole-operation limit (CSOT); a whole transaction with its retries gets twice this (db.in_transaction).
    # It includes the wait for a free pooled connection: with timeoutMS set PyMongo ignores waitQueueTimeoutMS
    # (pool._get_conn takes the CSOT deadline first), so this is also how long a request can wait for one.
    # 5 s: a healthy bill transaction is about 10 ms of MongoDB time. Long jobs set their own pymongo.timeout()
    # (migrations, rollups rebuild, retention, shop erase); export cursors get the limit per batch.
    mongo_timeout_ms: int = Field(5000, ge=100)
    mongo_appname: str = "hara-hisaab"
    # Requests one API worker process works on at once (overload.py); more get 503 "busy" at once. 0 = twice
    # MONGO_MAX_POOL_SIZE: a request holds a pooled connection only while one of its database calls runs, so
    # twice the pool keeps the connections busy while the other half of the requests run Python, and caps the
    # wait for a connection at about one other request's call. Above that, requests only queue (and time out).
    max_in_flight: int = Field(0, ge=0)

    jwt_secret: str = ""

    # Photos: "local" (disk) or "r2" (Cloudflare R2); production must use R2.
    storage_backend: Literal["local", "r2"] = "local"
    local_storage_dir: str = "/data/uploads"
    storage_prefix: str = "nurserypos"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""

    # Redis: rate limits, caches and the job queue. Unset (a laptop, tests): rate limits and caches live in the
    # process's memory and background jobs run as asyncio tasks in the web process.
    redis_url: str | None = None

    # Errors and metrics
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = Field(0.0, ge=0, le=1)
    log_level: str = "INFO"
    log_format: Literal["json", "console", ""] = ""  # "" = console in dev, JSON elsewhere
    metrics_token: str = ""  # /metrics also answers a scraper that sends "Authorization: Bearer <this>"

    # Addresses. No host names in code: bill links and photo links are built from these.
    public_base_url: str = ""  # e.g. https://bill.<domain>; empty = the address the request came to
    public_image_base_url: str = ""  # e.g. https://img.<domain> (WP-7); empty = photos through the API
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)  # empty: "*" in dev and staging

    # Claude: one model per kind of call, all defaulting to CLAUDE_MODEL (decision D5 keeps Sonnet for now).
    claude_model: str = DEFAULT_MODEL
    claude_model_plant: str = ""
    claude_model_invoice: str = ""
    claude_model_names: str = ""
    claude_model_order: str = ""  # paper orders read into a sale (routers/ai.py /ai/scan-order)
    claude_effort_plant: str = "low"
    claude_effort_invoice: str = "medium"
    claude_effort_order: str = "medium"
    ai_daily_limit: int = 300
    ai_enrich_names: bool = True

    google_client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    admin_phones: Annotated[list[str], NoDecode] = Field(default_factory=list)

    push_enabled: bool = False
    expo_access_token: str = ""

    # GET /api/config: phones on an older version are asked to update (WP-13). 1.1.0 never asks.
    min_app_version: str = "1.1.0"
    # Open owner decisions: today's behaviour until the owner decides.
    bill_without_gst: bool = True  # D6: the "Bill without GST" toggle stays available
    otp_required: bool = False  # D8: no OTP provider chosen; never enforced while 1.1.0 is in use
    invoice_number_scheme: Literal["legacy"] = "legacy"  # D7: INV-0001, one series per shop

    # Dev and tests only: run `python -m migrations up` at start-up. Never in staging or production.
    auto_migrate: bool = False

    @field_validator("cors_origins", "google_client_ids", "admin_phones", mode="before")
    @classmethod
    def _split(cls, v):
        return _csv(v)

    @field_validator("push_enabled", "ai_enrich_names", "auto_migrate", "bill_without_gst", "otp_required",
                     mode="before")
    @classmethod
    def _flag(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return v

    @field_validator("public_base_url", "public_image_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def _check(self):
        problems = []
        if self.env in ("staging", "production"):
            if len(self.jwt_secret) < 32:
                problems.append("JWT_SECRET must be at least 32 characters (openssl rand -hex 32)")
            if self.auto_migrate:
                problems.append("AUTO_MIGRATE is for dev and tests; run `python -m migrations up` instead")
        if self.env == "production":
            if self.storage_backend != "r2":
                problems.append("STORAGE_BACKEND must be r2 in production")
            missing = [n for n in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket")
                       if not getattr(self, n)]
            if missing:
                problems.append("missing " + ", ".join(n.upper() for n in missing))
            if not self.public_base_url.startswith("https://"):
                problems.append("PUBLIC_BASE_URL must be an https:// address in production")
            if "*" in self.cors_origins:
                problems.append("CORS_ORIGINS cannot be * in production")
        if problems:
            raise ValueError(f"bad {self.env} settings: " + "; ".join(problems))
        return self

    # --- derived ---
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def in_flight_limit(self) -> int:
        """MAX_IN_FLIGHT, or twice the MongoDB pool when it is 0 (the default)."""
        return self.max_in_flight or 2 * self.mongo_max_pool_size

    @property
    def docs_enabled(self) -> bool:
        return not self.is_production

    @property
    def cors_allow_origins(self) -> list[str]:
        """Browsers allowed to call the API. The phone app sends no Origin, so this only matters for the web
        build: everything in dev and staging (the owner's browser test app), only CORS_ORIGINS in production."""
        if self.cors_origins:
            return self.cors_origins
        return [] if self.is_production else ["*"]

    def model_for(self, kind: Literal["plant", "invoice", "names", "order"]) -> str:
        """The Claude model for one kind of call (CLAUDE_MODEL_PLANT / _INVOICE / _NAMES / _ORDER, else
        CLAUDE_MODEL)."""
        return getattr(self, f"claude_model_{kind}") or self.claude_model

    def feature_flags(self) -> dict:
        """What GET /api/config tells the app. Additive only: older apps ignore what they don't know."""
        return {"push": self.push_enabled, "google_sign_in": bool(self.google_client_ids),
                "bill_without_gst": self.bill_without_gst, "otp_required": self.otp_required,
                "invoice_number_scheme": self.invoice_number_scheme}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check() -> Settings:
    """Load the settings or stop the process with a non-zero exit code and a clear message."""
    try:
        return get_settings()
    except ValidationError as e:
        print(f"Configuration error, not starting:\n{e}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None


if __name__ == "__main__":  # python -m settings: check the environment, e.g. in a deploy before switching over
    s = check()
    print(f"settings ok: ENV={s.env} MONGO_DB={s.mongo_db} STORAGE_BACKEND={s.storage_backend} "
          f"REDIS={'on' if s.redis_url else 'off'} SENTRY={'on' if s.sentry_dsn else 'off'}")
