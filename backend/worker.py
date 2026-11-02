"""The background worker (arq): runs queued jobs (jobs.py) and the scheduled ones. One container, one replica.

    arq worker.WorkerSettings            (from backend/; compose service "worker")
    arq --check worker.WorkerSettings    (its health check)

Queued jobs: push delivery and Expo receipt checks (push.py). Register new job modules in JOB_MODULES.

Scheduled, once a minute (`tick`), here only, never in the web processes:
- The daily sales summary (notify.send_due_summaries) when PUSH_ENABLED is on. arq's `unique` cron makes it one
  run per minute however many workers there are, and notify's claim stops a second send anyway.
- Then a heartbeat: a GET to HEARTBEAT_URL (a Healthchecks.io, Better Stack or UptimeRobot heartbeat URL), when
  set. Only a tick that worked pings, so the monitor raises an alert when the worker is down, stuck, or cannot
  reach the database.
"""
import importlib
import logging
import os

from arq import cron
from arq.connections import RedisSettings

import db as db_module
import jobs
import notify
import observability
import push
from settings import check

JOB_MODULES: list[str] = ["push", "notify"]  # modules with @jobs.job functions, imported so the worker knows them
TICK_TIMEOUT = 300  # seconds; notify.STALE_CLAIM is longer, so a claim older than that has no live holder
log = logging.getLogger("worker")

settings = check()
for _module in JOB_MODULES:
    importlib.import_module(_module)


async def heartbeat() -> bool:
    """Tell the uptime monitor the worker is alive (HEARTBEAT_URL). A failed ping is logged, never raised."""
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        return False
    try:
        r = await push.http().get(url, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("heartbeat ping failed: %r", e)
        return False


async def tick(ctx):
    """Every minute: the daily summaries that are due, then the heartbeat."""
    if push.enabled():
        try:
            sent = await notify.send_due_summaries(ctx["db"])
        except Exception:
            log.exception("daily summary check failed")
            return  # no heartbeat: the monitor alerts if this keeps happening
        if sent:
            log.info("daily summaries sent to %d shop(s)", sent)
    await heartbeat()


async def startup(ctx):
    observability.setup_logging()
    observability.init_sentry()
    db_module.set_appname(f"{settings.mongo_appname}-worker")
    ctx["db"] = db_module.get_db()
    log.info("worker started: %d job(s), push %s, heartbeat %s", len(jobs.REGISTRY),
             "on" if push.enabled() else "off", "on" if os.environ.get("HEARTBEAT_URL", "").strip() else "off")


async def shutdown(ctx):
    await push.close()
    await jobs.close()
    await db_module.close()


class WorkerSettings:
    functions = list(jobs.REGISTRY.values())
    cron_jobs = [cron(tick, name="tick", second=0, run_at_startup=False, unique=True, timeout=TICK_TIMEOUT)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379")
    max_jobs = 20
    max_tries = push.MAX_TRIES  # a job whose worker died is run again; push.py backs off between tries
    job_timeout = 300
    keep_result = 3600  # a finished job's id is kept this long, so the same push queued again is skipped
    health_check_interval = 30
