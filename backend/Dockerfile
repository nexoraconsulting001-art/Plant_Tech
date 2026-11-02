# API, worker and migrations image (one image; compose picks the command).
# Base image pinned by tag (multi-arch: the VM is arm64). Before production pin the digest as well (supply chain):
#   docker buildx imagetools inspect python:3.12.14-slim-bookworm   -> FROM python:3.12.14-slim-bookworm@sha256:<digest>
#   (Docker Hub listed sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e on 2 Nov 2026; check it)
FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# every package pinned with hashes (requirements.lock, made from requirements.txt)
COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock

RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/uploads /tmp/prometheus && chown -R app:app /data /tmp/prometheus
COPY --chown=app:app . .

# GIT_SHA: the release shown in Sentry and logs (docker build --build-arg GIT_SHA=$(git rev-parse HEAD))
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA} PORT=8001 PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
USER app

EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8001\")}/livez', timeout=2)"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "server:app"]
