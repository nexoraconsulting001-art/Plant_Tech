"""The worker knows every background job and runs the daily ones (integration of WP-3, 5, 7, 8 and 10).

A module that defines an @jobs.job must be in worker.JOB_MODULES, or a worker started with REDIS_URL would not know
the job and a queued one would never run (without Redis, jobs run in the web process and this would go unnoticed).
"""
import re
from pathlib import Path

import worker

BACKEND = Path(__file__).resolve().parent.parent


def test_every_job_module_is_registered():
    defining = set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("tests", "migrations") or rel.name == "jobs.py":
            continue
        if re.search(r"^@jobs\.job\s*$", path.read_text(encoding="utf-8"), re.M):
            defining.add(".".join(rel.with_suffix("").parts))
    assert defining and defining <= set(worker.JOB_MODULES), sorted(defining - set(worker.JOB_MODULES))


def test_daily_jobs_are_scheduled():
    names = {c.name: c for c in worker.WorkerSettings.cron_jobs}
    assert {"tick", "cron:archive_audit_day", "cron:apply_retention", "cron:purge_exports",
            "cron:erase_due_shops"} <= set(names)
    assert all(c.unique for c in names.values())  # one run however many workers there are
    assert names["cron:erase_due_shops"].timeout_s > 3600
    jobs = {f.__name__ for f in worker.WorkerSettings.functions}
    assert {"look_up_names", "prepare_upload", "build_shop_export", "deliver_push", "check_push_receipts"} <= jobs
