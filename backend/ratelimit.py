"""Rate limits (the `limits` library, sliding-window counters).

    @router.post("/login", dependencies=[Depends(ratelimit.hit(ratelimit.by_ip, "10/minute", scope="login"))])
    await ratelimit.consume(f"ai:{user['id']}", "30/hour")       # inside a route, for any key

Rates are strings like "10/minute" or "5 per 10 seconds" (the limits notation). Over the limit: 429 with
Retry-After and the usual error body (`code` "rate_limited"). With REDIS_URL set the counts are shared by every
web worker and VM; without it (a laptop, tests) each process counts on its own, in memory.
If Redis is down the request is let through and logged, so an outage never locks shops out of billing.
"""
import logging
import time
from typing import Callable

from fastapi import Request
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage, RedisStorage
from limits.aio.strategies import SlidingWindowCounterRateLimiter

from errors import AppError
from settings import get_settings

log = logging.getLogger(__name__)

_limiter: SlidingWindowCounterRateLimiter | None = None
_rates: dict[str, RateLimitItem] = {}


def limiter() -> SlidingWindowCounterRateLimiter:
    global _limiter
    if _limiter is None:
        url = get_settings().redis_url
        storage = (RedisStorage(url.replace("redis://", "async+redis://", 1).replace("rediss://", "async+rediss://", 1),
                                implementation="redispy", key_prefix="hh-limits")
                   if url else MemoryStorage())
        _limiter = SlidingWindowCounterRateLimiter(storage)
    return _limiter


def reset():
    """Forget every count and the storage (tests)."""
    global _limiter
    _limiter = None


def _rate(rate: str) -> RateLimitItem:
    if rate not in _rates:
        _rates[rate] = parse(rate)
    return _rates[rate]


async def consume(key: str, rate: str):
    """Count one hit on `key`; raises the 429 AppError when `key` is over `rate`."""
    item = _rate(rate)
    try:
        if await limiter().hit(item, key):
            return
        reset_at, _ = await limiter().get_window_stats(item, key)
    except Exception:
        log.exception("rate limit check failed for %s; letting the request through", key.split(":")[0])
        return
    seconds = max(1, int(reset_at - time.time()) + 1)
    err = AppError(429, "Too many attempts. Please try again in {{seconds}} seconds.", code="rate_limited", seconds=seconds)
    err.headers = {"Retry-After": str(seconds)}
    raise err


def by_ip(request: Request) -> str:
    """The caller's address (the real one: gunicorn trusts the proxy's X-Forwarded-For)."""
    return request.client.host if request.client else "unknown"


def hit(key: str | Callable[[Request], str], rate: str, scope: str = ""):
    """A FastAPI dependency counting one hit per request. `key` is a fixed string or a function of the request
    (e.g. by_ip); `scope` keeps different limits on the same key apart (default: the route's path)."""
    async def dependency(request: Request):
        k = key(request) if callable(key) else key
        name = scope or getattr(request.scope.get("route"), "path", request.url.path)
        await consume(f"{name}:{k}", rate)
    return dependency
