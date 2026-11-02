"""The background worker (arq): runs queued jobs (jobs.py) and the scheduled ones. One container, one replica.

    arq worker.WorkerSettings            (from backend/; compose service "worker")
    arq --check worker.WorkerSettings    (its health check)

Queued jobs: push delivery and Expo receipt checks (push.py), catalogue name look-ups (routers/library.py), photo
thumbnails and public copies (routers/files.py), shop exports (routers/exports.py). Register new job modules in
JOB_MODULES (tests/test_worker_wiring.py checks that every module with an @jobs.job is listed).

Scheduled, once a minute (`tick`), here only, never in the web processes:
- The daily sales summary (notify.send_due_summaries) when PUSH_ENABLED is on. arq's `unique` cron makes it one
  run per minute however many workers there are, and notify's claim stops a second send anyway.
- Then a heartbeat: a GET to HEARTBEAT_URL (a Healthchecks.io, Better Stack or UptimeRobot heartbeat URL), when
  set. Only a tick that worked pings, so the monitor raises an alert when the worker is down, stuck, or cannot
  reach the database.

Scheduled once a day (India time, WorkerSettings.timezone), here only:
- 00:30 the audit log's previous day copied to object storage (audit.archive_audit_day, WP-10)
- 03:00 retention (retention.apply_retention, D9 defaults), 03:15 expired shop export files (purge_exports)
- 03:30 shops whose deletion grace period is over are archived and erased (routers/account.erase_due, WP-8)
"""
import importlib
import logging
import os

from arq import cron
from arq.connections import RedisSettings

import audit
import db as db_module
import jobs
import notify
import observability
import push
import retention
from common import IST
from routers import account, exports
from settings import check

JOB_MODULES: list[str] = ["push", "notify", "routers.library", "routers.files", "routers.exports", "retention",
                          "audit"]  # modules with @jobs.job functions, imported so the worker knows them
ERASE_TIMEOUT = account.ERASE_TIMEOUT + 300  # seconds: one big shop's archive copy may take most of an hour
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


async def erase_due_shops(ctx):
    """Daily: shops whose deletion grace period is over (routers/account.py). Safe to run twice."""
    erased = await account.erase_due(ctx["db"])
    if erased:
        log.info("erased %d shop(s) after their grace period", len(erased))
    return erased


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
    timezone = IST  # the daily crons below are India times
    cron_jobs = [
        cron(tick, name="tick", second=0, run_at_startup=False, unique=True, timeout=TICK_TIMEOUT),
        cron(audit.archive_audit_day, hour=0, minute=30, unique=True, timeout=1800),
        cron(retention.apply_retention, hour=3, minute=0, unique=True, timeout=1800),
        cron(exports.purge_exports, hour=3, minute=15, unique=True, timeout=1800),
        cron(erase_due_shops, hour=3, minute=30, unique=True, timeout=ERASE_TIMEOUT),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379")
    max_jobs = 20
    max_tries = push.MAX_TRIES  # a job whose worker died is run again; push.py backs off between tries
    job_timeout = 300
    keep_result = 3600  # a finished job's id is kept this long, so the same push queued again is skipped
    health_check_interval = 30
