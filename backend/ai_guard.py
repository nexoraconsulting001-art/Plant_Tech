"""How many Claude calls run at once, and a circuit breaker for Claude outages (used by ai_client._ask).

    async with ai_guard.slot():      # waits up to AI_QUEUE_WAIT_SECONDS for a free slot, else ai_guard.Busy
        ...call Claude...

Slots: at most AI_MAX_CONCURRENCY calls per process (an asyncio semaphore) and, with REDIS_URL set, at most
AI_GLOBAL_CONCURRENCY for every web worker and VM together (a sorted set in Redis: one member per call in flight,
added by a small Lua script only while there is room; a member left by a crashed process expires after
SLOT_TTL). If Redis cannot be reached the call goes ahead on the process limit alone and the failure is
logged (and Redis is left alone for REDIS_RETRY_AFTER seconds), the same rule as ratelimit.py: an outage of
Redis never stops scans.

Breaker (per process): after AI_BREAKER_FAILURES calls in a row (default 10; each call is already 3 tries)
that failed because Claude was down or refused our key, calls fail at once for AI_BREAKER_RESET_SECONDS
(default 30); then one trial call is let through, and its result closes or reopens the breaker. A busy answer
(429) or a bad photo does not count.

The breaker and the semaphore belong to the running event loop: one per worker process in production, a fresh
pair for each test (each TestClient runs its own loop).
"""
import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from settings import get_settings

log = logging.getLogger(__name__)

SLOT_KEY = "hh-ai:slots"
SLOT_TTL = 400  # seconds; longer than the slowest call (a bill: 120 s a try, 3 tries)

# KEYS[1] the slots set; ARGV: now, ttl, limit, token. 1 = slot taken, 0 = all slots busy.
_ACQUIRE = """
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[2]))
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Busy(Exception):
    """No free slot within the wait; the caller says "busy, try again"."""


class Open(Exception):
    """The breaker is open: Claude failed several times in a row just now."""


class CircuitBreaker:
    def __init__(self, failures: int, reset_after: float):
        self.threshold, self.reset_after = max(1, failures), reset_after
        self.failures, self.opened_at, self.probing = 0, None, False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.probing or time.monotonic() - self.opened_at >= self.reset_after else "open"

    def check(self):
        """Raises Open unless a call may go ahead (closed, or the one trial call after the pause)."""
        if self.opened_at is None:
            return
        if not self.probing and time.monotonic() - self.opened_at >= self.reset_after:
            self.probing = True
            return
        raise Open()

    def success(self):
        self.failures, self.opened_at, self.probing = 0, None, False

    def failure(self):
        self.failures += 1
        self.probing = False
        if self.failures >= self.threshold:
            if self.opened_at is None:
                log.error("Claude failed %s times in a row; pausing calls for %ss", self.failures, self.reset_after)
            self.opened_at = time.monotonic()

    def release(self):
        """The trial call ended without a verdict (e.g. cancelled): let the next call try."""
        self.probing = False


_local: dict = {"loop": None, "sem": None, "breaker": None}
_redis: dict = {"loop": None, "client": None, "acquire": None, "skip_until": 0.0}
REDIS_RETRY_AFTER = 30  # seconds the shared limit is skipped after Redis failed, so calls don't each wait for it


def reset():
    """Forget the breaker, the semaphore and the Redis client; the next call makes new ones (tests)."""
    _local.update(loop=None, sem=None, breaker=None)
    _redis.update(loop=None, client=None, acquire=None, skip_until=0.0)


def _here() -> dict:
    loop = asyncio.get_running_loop()
    if _local["loop"] is not loop:
        _local.update(loop=loop, sem=asyncio.Semaphore(max(1, _int_env("AI_MAX_CONCURRENCY", 8))),
                      breaker=CircuitBreaker(_int_env("AI_BREAKER_FAILURES", 10),
                                             _int_env("AI_BREAKER_RESET_SECONDS", 30)))
    return _local


def breaker() -> CircuitBreaker:
    """This process's breaker (call it inside the event loop)."""
    return _here()["breaker"]


def _redis_script():
    url = get_settings().redis_url
    if not url:
        return None, None
    loop = asyncio.get_running_loop()
    if _redis["loop"] is not loop:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        _redis.update(loop=loop, client=client, acquire=client.register_script(_ACQUIRE))
    return _redis["client"], _redis["acquire"]


async def _global_acquire(deadline: float) -> str | None:
    """A slot in the shared limit (its token), None when there is no Redis or it failed; Busy after deadline."""
    client, acquire = _redis_script()
    if client is None or time.monotonic() < _redis["skip_until"]:
        return None
    token = uuid.uuid4().hex
    limit = max(1, _int_env("AI_GLOBAL_CONCURRENCY", 32))
    while True:
        try:
            if await acquire(keys=[SLOT_KEY], args=[time.time(), SLOT_TTL, limit, token]):
                return token
        except Exception:
            log.exception("AI slot check in Redis failed; using the process limit alone for %ss", REDIS_RETRY_AFTER)
            _redis["skip_until"] = time.monotonic() + REDIS_RETRY_AFTER
            return None
        if time.monotonic() >= deadline:
            raise Busy()
        await asyncio.sleep(0.2)


async def _global_release(token: str):
    try:
        await _redis["client"].zrem(SLOT_KEY, token)
    except Exception:
        log.warning("could not free an AI slot in Redis (it expires by itself)", exc_info=True)


@asynccontextmanager
async def slot():
    """One Claude call's place in the per-process and (with Redis) shared limits."""
    wait = max(0.0, float(os.environ.get("AI_QUEUE_WAIT_SECONDS", "10")))
    deadline = time.monotonic() + wait
    sem = _here()["sem"]
    try:
        async with asyncio.timeout(wait):
            await sem.acquire()
    except TimeoutError:
        raise Busy() from None
    try:
        token = await _global_acquire(deadline)
        try:
            yield
        finally:
            if token:
                await _global_release(token)
    finally:
        sem.release()
