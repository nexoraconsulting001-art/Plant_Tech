"""Background jobs: work that should not hold up a request (name look-ups, push sends, exports).

    @jobs.job
    async def look_up_names(ctx, entry_id: str): ...     # ctx["db"] is the database
    await jobs.enqueue("look_up_names", entry_id)

With REDIS_URL set the job goes on the arq queue and the worker container runs it (worker.py), surviving a
restart of the web process. Without Redis (a laptop, tests) it runs as an asyncio task in this process, which
is what the code did before the worker existed. A job must be safe to run twice: arq retries after a crash.

A module that defines jobs must be imported by worker.py (JOB_MODULES) so the worker knows them.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from settings import get_settings

log = logging.getLogger(__name__)

REGISTRY: dict[str, Callable[..., Awaitable]] = {}
_pool = None
_running: set[asyncio.Task] = set()  # in-process jobs, kept alive until they finish


def job(fn):
    """Register `fn(ctx, *args, **kwargs)` as a job under its function name."""
    if REGISTRY.get(fn.__name__, fn) is not fn:
        raise RuntimeError(f"two jobs named {fn.__name__}")
    REGISTRY[fn.__name__] = fn
    return fn


async def pool():
    global _pool
    if _pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def enqueue(name: str, *args, _job_id: str | None = None, _defer_by: float | None = None, **kwargs) -> str | None:
    """Run job `name` in the background. `_job_id` makes it unique (arq skips a second one while the first is
    queued or kept). Returns the arq job id, or None when run in-process or skipped as a duplicate."""
    if name not in REGISTRY:
        raise KeyError(f"unknown job {name}")
    if get_settings().redis_url:
        queued = await (await pool()).enqueue_job(name, *args, _job_id=_job_id, _defer_by=_defer_by, **kwargs)
        return queued.job_id if queued else None
    task = asyncio.get_running_loop().create_task(_run_here(name, _defer_by, args, kwargs))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return None


async def _run_here(name: str, defer_by: float | None, args, kwargs):
    from db import get_db

    if defer_by:
        await asyncio.sleep(defer_by)
    try:
        await REGISTRY[name]({"db": get_db(), "job_try": 1, "in_process": True}, *args, **kwargs)
    except Exception:
        log.exception("job %s failed", name)


async def drain(timeout: float = 10):
    """Wait for in-process jobs (tests; shutdown)."""
    if _running:
        await asyncio.wait(list(_running), timeout=timeout)


async def close():
    global _pool
    await drain()
    if _pool is not None:
        await _pool.aclose()
        _pool = None
