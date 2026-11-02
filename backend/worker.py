"""The background worker (arq): runs queued jobs (jobs.py) and the scheduled ones. One container, one replica.

    arq worker.WorkerSettings            (from backend/; compose service "worker")
    arq --check worker.WorkerSettings    (its health check)

Scheduled: every minute, the daily sales summary check (notify.send_due_summaries) when PUSH_ENABLED is on.
It used to run inside the web process; here it runs exactly once however many web workers there are.
WP-5 replaces it with a batched job and adds push delivery jobs; register new job modules in JOB_MODULES.
"""
import importlib
import logging

from arq import cron
from arq.connections import RedisSettings

import db as db_module
import jobs
import notify
import observability
import push
from settings import check

JOB_MODULES: list[str] = []  # modules with @jobs.job functions, imported so the worker knows them
log = logging.getLogger("worker")

settings = check()
for _module in JOB_MODULES:
    importlib.import_module(_module)


async def daily_summaries(ctx):
    """Hook for the daily summary (WP-5 makes it a batched, claimed job)."""
    if push.enabled():
        sent = await notify.send_due_summaries(ctx["db"])
        if sent:
            log.info("daily summaries sent to %d shop(s)", sent)


async def startup(ctx):
    observability.setup_logging()
    observability.init_sentry()
    db_module.set_appname(f"{settings.mongo_appname}-worker")
    ctx["db"] = db_module.get_db()
    log.info("worker started: %d job(s), push %s", len(jobs.REGISTRY), "on" if push.enabled() else "off")


async def shutdown(ctx):
    await db_module.close()


class WorkerSettings:
    functions = list(jobs.REGISTRY.values())
    cron_jobs = [cron(daily_summaries, second=0, run_at_startup=False, unique=True, timeout=300)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379")
    max_jobs = 20
    job_timeout = 300
    keep_result = 3600
    health_check_interval = 30
