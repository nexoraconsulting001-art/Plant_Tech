"""Overload protection: when a worker is saturated, answer at once with 503 instead of queueing for seconds.

Without it a saturated worker kept taking requests: they queued in the event loop and for a MongoDB connection,
waited up to the 10 s operation limit, came back as 500s with a traceback each, and bill transactions sat open
until MongoDB aborted them. Now:

- InFlightLimit (pure ASGI): at most MAX_IN_FLIGHT requests are inside this worker process at once (default
  2 x MONGO_MAX_POOL_SIZE, see settings.py). One more gets 503 `busy` with Retry-After at once, before any route
  code or database call runs, so the app may send it again whatever it was. /livez, /readyz, /health and
  /metrics are never refused. uvicorn's own `limit_concurrency` is not used: it counts open connections too
  (uvicorn h11_impl/httptools_impl: `len(self.connections) >= limit or len(self.tasks) >= limit`), and Caddy
  keeps idle keep-alive connections open, so it would refuse work on an idle worker; its 503 is also plain text,
  without Retry-After or the app's error shape.
- db_error_handler: MongoDB too slow or out of reach (no free connection in the pool, an operation over its
  time limit, no primary, a network timeout: PyMongo's `error.timeout`, or any ConnectionFailure) becomes 503
  `db_busy` with Retry-After and one warning line, never a 500 with a traceback. Any other database error is
  raised as before (500, Sentry). The request may have written part of its work before the error (outside a
  transaction), so the app sends again only requests that are safe to repeat (sync-policy.ts).

Both answers use the usual error body (errors.AppError): `detail`, `key`, `code`, `params`.
"""
import logging
import time

from pymongo.errors import ConnectionFailure, PyMongoError

from errors import AppError, error_response
from observability import QUIET_ROUTES

log = logging.getLogger(__name__)

RETRY_AFTER = 2  # seconds the app waits before sending again (it adds its own random part)
WARN_EVERY = 10  # seconds between two "refusing requests" lines of one worker


def busy(code: str) -> AppError:
    """The 503 the app retries after Retry-After (the same text and code "busy" as a full login queue, auth.py)."""
    err = AppError(503, "The server is busy. Please try again in a few seconds.", code)
    err.headers = {"Retry-After": str(RETRY_AFTER)}
    return err


class InFlightLimit:
    """At most `limit` requests of this process inside the app at once; one more gets 503 `busy` (pure ASGI).
    One event loop per process, so a plain counter is enough."""

    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit
        self.in_flight = 0
        self.refused = 0
        self._warned_at = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in QUIET_ROUTES:
            return await self.app(scope, receive, send)
        if self.in_flight >= self.limit:
            self.refused += 1
            now = time.monotonic()
            if now - self._warned_at >= WARN_EVERY:
                self._warned_at = now
                log.warning("busy: %d requests in flight (MAX_IN_FLIGHT %d); refusing with 503, %d so far",
                            self.in_flight, self.limit, self.refused)
            return await error_response(busy("busy"))(scope, receive, send)
        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1


def is_db_unavailable(exc: BaseException) -> bool:
    """MongoDB is too slow or out of reach: any PyMongo timeout (pool wait, operation limit, server selection,
    network, write concern) or a lost connection. Not a refused write such as a duplicate key."""
    return isinstance(exc, ConnectionFailure) or bool(getattr(exc, "timeout", False))


async def db_error_handler(request, exc: PyMongoError):
    if not is_db_unavailable(exc):
        raise exc  # a real error: the usual 500 with its traceback (and Sentry)
    route = getattr(request.scope.get("route"), "path", None) or "unmatched"
    log.warning("database busy: %s on %s %s: %s", type(exc).__name__, request.method, route, str(exc)[:200])
    return error_response(busy("db_busy"))
