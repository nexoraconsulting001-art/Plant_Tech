"""Logs, errors and metrics.

Logs: one JSON object per line on stdout (console text in dev), from structlog and from every module's plain
`logging.getLogger(__name__)` alike. Each line carries `request_id` (asgi-correlation-id; the X-Request-ID
header, made up when the client sent none, and returned in the response), plus `business_id` and `user_id` once
the request's login is known (`bind_user`, called by auth.current_user). Every request ends with one "request"
line: method, route template (never the raw path, so bill tokens stay out of logs), status and duration.

Errors: Sentry when SENTRY_DSN is set; release = GIT_SHA, environment = ENV. No personal data is sent: no
request bodies, query strings, cookies or local variables; tokens, passwords and phone numbers are scrubbed.

Metrics: Prometheus at /metrics (prometheus-fastapi-instrumentator), only for a scraper on the VM itself or one
that sends METRICS_TOKEN. With several gunicorn workers set PROMETHEUS_MULTIPROC_DIR (the Docker image does).
"""
import hmac
import ipaddress
import logging
import re
import sys
import time
from contextvars import ContextVar

import orjson
import sentry_sdk
import structlog
from asgi_correlation_id import correlation_id
from fastapi import Depends, HTTPException, Request
from sentry_sdk.scrubber import DEFAULT_DENYLIST, DEFAULT_PII_DENYLIST, EventScrubber

from settings import Settings, get_settings

_request: ContextVar[dict | None] = ContextVar("request_context", default=None)
access_log = structlog.get_logger("access")
QUIET_ROUTES = {"/livez", "/readyz", "/health", "/metrics"}  # load balancer and scraper calls: not logged


# --- logging -----------------------------------------------------------------------------------------------
def _add_request(_logger, _method, event: dict) -> dict:
    if rid := correlation_id.get():
        event.setdefault("request_id", rid)
    for k, v in (_request.get() or {}).items():
        event.setdefault(k, v)
    return event


def _dumps(obj, **_kw) -> str:
    return orjson.dumps(obj, default=str).decode()


def setup_logging(s: Settings | None = None):
    """JSON lines (or console text in dev) for structlog and the standard logging module. Safe to call again."""
    s = s or get_settings()
    as_json = (s.log_format or ("console" if s.env == "dev" else "json")) == "json"
    shared = [structlog.contextvars.merge_contextvars, structlog.stdlib.add_log_level, structlog.stdlib.add_logger_name,
              _add_request, structlog.processors.TimeStamper(fmt="iso", utc=True)]
    if as_json:
        render = [structlog.processors.dict_tracebacks, structlog.processors.EventRenamer("message"),
                  structlog.processors.JSONRenderer(serializer=_dumps)]
    else:
        render = [structlog.dev.ConsoleRenderer(colors=False)]
    structlog.configure(processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
                        logger_factory=structlog.stdlib.LoggerFactory(), wrapper_class=structlog.stdlib.BoundLogger,
                        cache_logger_on_first_use=True)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *render]))
    root = logging.getLogger()
    # replaces any earlier handler (ours or basicConfig's); pytest's log capture stays
    root.handlers = [h for h in root.handlers if type(h).__module__.startswith("_pytest")] + [handler]
    root.setLevel(s.log_level.upper())
    for name in ("uvicorn", "uvicorn.error", "gunicorn.error", "arq"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # the "request" line below replaces it
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "botocore", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware:
    """Holds the request's login for log lines and writes the "request" access line (pure ASGI)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ctx: dict = {}
        token = _request.set(ctx)
        start = time.perf_counter()
        status = [500]

        async def send_status(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            if route not in QUIET_ROUTES:
                access_log.info("request", method=scope.get("method"), route=route, status=status[0],
                                duration_ms=round((time.perf_counter() - start) * 1000, 1))
            _request.reset(token)


def bind_user(user: dict):
    """The logged-in user of this request: into its log lines and its Sentry events (ids only)."""
    ctx = _request.get()
    if ctx is not None:
        ctx.update(business_id=user.get("business_id"), user_id=user.get("id"))
    sentry_sdk.set_user({"id": user.get("id")})
    sentry_sdk.set_tag("business_id", user.get("business_id"))


def request_context() -> dict:
    """request_id, business_id and user_id of the current request (empty outside one); e.g. for audit.py."""
    return {**({"request_id": rid} if (rid := correlation_id.get()) else {}), **(_request.get() or {})}


# --- Sentry ------------------------------------------------------------------------------------------------
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
BILL_LINK = re.compile(r"/i/[A-Za-z0-9_\-]+")
SAFE_HEADERS = {"user-agent", "content-type", "content-length", "x-request-id", "x-app-version", "accept-language"}
DENY = [*DEFAULT_DENYLIST, *DEFAULT_PII_DENYLIST, "phone", "customer_phone", "pin", "new_password",
        "current_password", "id_token", "push_token", "gstin", "address", "upi_id", "upi_ids", "idempotency_key"]


def _mask(text):
    return BILL_LINK.sub("/i/[token]", PHONE.sub("[phone]", text)) if isinstance(text, str) else text


def scrub_event(event: dict, _hint=None) -> dict:
    """Last pass over every event and transaction before it leaves (after Sentry's own EventScrubber)."""
    req = event.get("request") or {}
    for k in ("data", "query_string", "cookies", "env"):
        req.pop(k, None)
    if "url" in req:
        req["url"] = _mask(req["url"])
    if isinstance(req.get("headers"), dict):
        req["headers"] = {k: v for k, v in req["headers"].items() if k.lower() in SAFE_HEADERS}
    if "transaction" in event:
        event["transaction"] = _mask(event["transaction"])
    if isinstance(event.get("logentry"), dict):
        event["logentry"] = {k: _mask(v) for k, v in event["logentry"].items()}
    if "message" in event:
        event["message"] = _mask(event["message"])
    for exc in (event.get("exception") or {}).get("values") or []:
        exc["value"] = _mask(exc.get("value"))
    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        crumb["message"] = _mask(crumb.get("message"))
        crumb.pop("data", None)
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {"id": user["id"]} if user.get("id") else {}
    return event


def init_sentry(s: Settings | None = None, **options) -> bool:
    """Start Sentry if SENTRY_DSN is set. `options` go to sentry_sdk.init (tests pass a transport)."""
    s = s or get_settings()
    if not s.sentry_dsn:
        return False
    sentry_sdk.init(dsn=s.sentry_dsn, environment=s.env, release=s.git_sha or None, send_default_pii=False,
                    include_local_variables=False, max_request_body_size="never",
                    traces_sample_rate=s.sentry_traces_sample_rate,
                    event_scrubber=EventScrubber(denylist=DENY, recursive=True),
                    before_send=scrub_event, before_send_transaction=scrub_event, **options)
    return True


# --- metrics -----------------------------------------------------------------------------------------------
def _local(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def metrics_allowed(request: Request):
    """Dependency of /metrics: a scraper on the VM (not through the proxy: no X-Forwarded-For), or METRICS_TOKEN."""
    token = get_settings().metrics_token
    sent = request.headers.get("authorization", "")
    if token and hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        return
    if "x-forwarded-for" not in request.headers and _local(request.client.host if request.client else None):
        return
    raise HTTPException(404)  # as if there were no such page


def setup_metrics(app):
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(excluded_handlers=[*QUIET_ROUTES], should_group_status_codes=False,
                   should_instrument_requests_inprogress=True, inprogress_labels=True).instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False, dependencies=[Depends(metrics_allowed)])
