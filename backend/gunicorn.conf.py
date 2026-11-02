"""gunicorn settings for the API container: `gunicorn -c gunicorn.conf.py server:app` (the Dockerfile's CMD).

- Workers: WEB_CONCURRENCY, else 2 per CPU capped at 8. Each worker is one Uvicorn event loop with its own
  MongoDB pool (MONGO_MAX_POOL_SIZE), so workers x pool size must stay under the database's connection limit.
  On the shared VM compose sets WEB_CONCURRENCY=2.
- Timeouts: a worker silent for 150 s is restarted (a supplier bill read by Claude can take two minutes);
  on SIGTERM workers get 30 s to finish what they are doing (compose stop_grace_period is 30 s too).
- Each worker is replaced after about 2000 requests (with jitter, so not all at once) to cap memory growth.
- Forwarded headers are trusted from any peer: the port is published on 127.0.0.1 only, so only the VM's
  Caddy can reach it, and inside Docker its requests come from the bridge gateway, not 127.0.0.1.
- Settings are checked before any worker starts: bad production settings stop gunicorn with exit code 2.
- Prometheus: with PROMETHEUS_MULTIPROC_DIR set, the folder is emptied at start and a dead worker's numbers
  are dropped, as prometheus_client's multiprocess mode requires.
"""
import multiprocessing
import os
import shutil
from pathlib import Path

bind = f"0.0.0.0:{os.environ.get('PORT', '8001')}"
worker_class = "uvicorn_worker.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY") or min(2 * multiprocessing.cpu_count(), 8))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "150"))
graceful_timeout = 30
keepalive = 5  # behind Caddy, which reuses connections
max_requests = 2000
max_requests_jitter = 200
forwarded_allow_ips = "*"
proxy_allow_ips = "*"
accesslog = None  # the app writes one JSON "request" line per request (observability.py)
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
worker_tmp_dir = "/dev/shm" if Path("/dev/shm").is_dir() else None  # heartbeat files off the container's disk


def on_starting(server):
    from settings import check

    check()  # exits non-zero on bad settings, before any worker is forked
    folder = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)


def child_exit(server, worker):
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
