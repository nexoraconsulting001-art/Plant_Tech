#!/usr/bin/env bash
# Deploy Hara Hisaab (NurseryPOS) on the VM:
#   deploy.sh staging | deploy.sh prod         deploy the newest commit of the branch
#   deploy.sh staging rollback [<sha>]         put back the image that ran before the last deploy (or the one of <sha>)
# Also run by the GitHub Actions deploy key through ~/bin/nurserypos-ci-deploy (deploy/ci-deploy).
#
# A deploy, step by step (the old version keeps serving until step 4):
#   1. sync the checkout to its branch, then re-run the freshly checked-out copy of this script
#   2. image: build it here (default) or pull ghcr.io/...:<sha> made by GitHub Actions (IMAGE_SOURCE=ghcr in .env)
#   3. check the settings and run the migrations (python -m migrations up) in one-off containers
#   4. swap web and worker to the new image and wait until /readyz answers 200 several times in a row
#   5. if any step fails: put the previous image back (and wait until it is ready again), then exit 1
#   6. after a version without daily rollups served bills (the old one during this deploy, or a rollback), rebuild
#      the rollups of the days it may have changed (python rollups.py rebuild --touched-since <day>)
# Migrations must keep working with the previous version (add fields and indexes; remove them a release later),
# because a rollback runs the old code on the migrated database. See docs/RUNBOOK.md.
#
# Images are tagged <project>-api:<sha>; the newest few stay for manual rollbacks. State (the last good commit,
# a deploy log) lives in ~/.nurserypos/<target>/, outside the checkout.
set -euo pipefail

TARGET=${1:-}
ACTION=${2:-deploy}
usage() { echo "usage: deploy.sh staging|prod [rollback [<sha>]]" >&2; exit 2; }
case "$TARGET" in
  staging) DIR="${NURSERYPOS_DIR:-$HOME/Plant_Tech-staging}"; BRANCH=staging ;;
  prod)    DIR="${NURSERYPOS_DIR:-$HOME/Plant_Tech}";         BRANCH=main ;;
  *) usage ;;
esac
case "$ACTION" in deploy|rollback) ;; *) usage ;; esac

# Sync first, then re-run the freshly checked-out copy of this script (a rollback leaves the checkout alone).
if [ "$ACTION" = deploy ] && [ "${NURSERYPOS_SYNCED:-}" != 1 ]; then
  git -C "$DIR" fetch --quiet origin "$BRANCH"
  git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
  NURSERYPOS_SYNCED=1 exec "$DIR/deploy/deploy.sh" "$@"
fi

cd "$DIR/deploy"
env_get() { grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }
PORT=$(env_get HOST_PORT); PORT=${PORT:-8120}
PROJECT=$(env_get PROJECT_NAME); PROJECT=${PROJECT:-nurserypos}
IMAGE_SOURCE=$(env_get IMAGE_SOURCE); IMAGE_SOURCE=${IMAGE_SOURCE:-build}
GHCR_IMAGE=$(env_get GHCR_IMAGE)          # e.g. ghcr.io/<owner>/hara-hisaab-api
READY_TIMEOUT=${READY_TIMEOUT:-90}        # seconds the new version gets to answer /readyz
READY_STREAK=${READY_STREAK:-3}           # answers in a row: a container that crashes after one is not ready
KEEP_IMAGES=${KEEP_IMAGES:-4}             # <project>-api:<sha> images kept for manual rollbacks
IMAGE="$PROJECT-api"                      # compose.yaml: image ${PROJECT_NAME}-api:latest
STATE="${NURSERYPOS_STATE:-$HOME/.nurserypos}/$TARGET"
mkdir -p "$STATE"
SHA=$(git rev-parse HEAD)
export GIT_SHA="$SHA"                     # the release in Sentry and the logs (compose build arg)

log() { echo "==> $*"; echo "$(date -u +%FT%TZ) $*" >> "$STATE/deploy.log"; }

# One deploy per target at a time: a manual run and a CI run must not overlap.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$STATE/lock"
  flock -w 600 9 || { echo "another deploy of $TARGET is still running" >&2; exit 1; }
fi

# Ready = /readyz answers 200. A version older than WP-0 has no /readyz (404); then /health counts.
ready_once() {
  local code
  code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/readyz" 2>/dev/null || true)
  [ "$code" = 200 ] && return 0
  [ "$code" = 404 ] && curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

wait_ready() {
  local streak=0 waited=0
  while [ "$waited" -lt "$READY_TIMEOUT" ]; do
    if ready_once; then
      streak=$((streak + 1))
      [ "$streak" -ge "$READY_STREAK" ] && return 0
    else
      streak=0
    fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

running_image() {  # image ID of the running web container (empty if none)
  local id
  id=$(docker compose ps -q web 2>/dev/null | head -n 1 || true)
  if [ -n "$id" ]; then docker inspect -f '{{.Image}}' "$id" 2>/dev/null || true; fi
}

# Whether an image has a file of the app (WORKDIR /app), e.g. worker.py or rollups.py: older versions lack them.
has_file() { docker run --rm --no-healthcheck --entrypoint test "$1" -f "$2" >/dev/null 2>&1; }

# Daily rollups (backend/rollups.py): a version without them saves bills that never reach daily_sales and
# daily_item_sales, so once such a version has served bills (the old one while this deploy migrates, or a
# rollback), the next good deploy rebuilds the days it may have changed. The marker holds the first such day
# (India date) and stays until a rebuild worked.
ROLLUPS_MARK="$STATE/rollups-stale-since"
mark_rollups_stale() {  # $1: an image that serves bills now
  if [ ! -s "$ROLLUPS_MARK" ] && ! has_file "$1" rollups.py; then
    TZ=IST-5:30 date +%F > "$ROLLUPS_MARK"  # India date (POSIX TZ: UTC+5:30, no tz database needed)
    log "$TARGET: $1 keeps no daily rollups; the next deploy rebuilds them from $(cat "$ROLLUPS_MARK")"
  fi
}
rebuild_rollups() {
  [ -s "$ROLLUPS_MARK" ] || return 0
  local since
  since=$(cat "$ROLLUPS_MARK")
  if docker compose run --rm --no-TTY --no-deps migrate python rollups.py rebuild --touched-since "$since"; then
    rm -f "$ROLLUPS_MARK"
    log "$TARGET: daily rollups rebuilt from $since"
    return 0
  fi
  log "$TARGET: REBUILDING THE DAILY ROLLUPS FAILED (from $since) - see docs/RUNBOOK.md, Daily rollups"
  return 1
}

# Back to an earlier image. Web and worker only (--no-deps): the old code must not start the migration runner.
# An image older than the worker (arq, worker.py) gets web alone: its worker would only crash-loop.
restore() {
  log "$TARGET: rolling back to $1"
  docker tag "$1" "$IMAGE:latest"
  if has_file "$1" worker.py; then
    docker compose up -d --no-build --no-deps web worker
  else
    docker compose rm --stop --force worker >/dev/null 2>&1 || true
    docker compose up -d --no-build --no-deps web
  fi
  mark_rollups_stale "$1"
  if wait_ready; then
    log "$TARGET: rolled back, serving the previous version again"
    return 0
  fi
  log "$TARGET: THE ROLLBACK DID NOT COME UP - see docs/RUNBOOK.md, Rollback"
  docker compose logs --tail 40 web >&2 || true
  return 1
}

prune_images() {
  # Keep :latest, :rollback and the newest KEEP_IMAGES commit tags; never touch other sites' images.
  { docker image ls "$IMAGE" --format '{{.Tag}}' | grep -E '^[0-9a-f]{40}$' | tail -n +"$((KEEP_IMAGES + 1))" |
    while read -r tag; do docker image rm "$IMAGE:$tag" >/dev/null 2>&1 || true; done; } || true
  docker image prune -f --filter "label=com.docker.compose.project=$PROJECT" >/dev/null 2>&1 || true
}

if [ "$ACTION" = rollback ]; then
  REF="$IMAGE:${3:-rollback}"
  docker image inspect "$REF" >/dev/null 2>&1 || { echo "no image $REF here (docker image ls $IMAGE)" >&2; exit 1; }
  restore "$REF"
  exit 0
fi

log "$TARGET: deploying $(git log --oneline -1)"

# What runs now is what a failed deploy goes back to.
PREVIOUS=$(running_image)
if [ -n "$PREVIOUS" ]; then
  docker tag "$PREVIOUS" "$IMAGE:rollback"
  mark_rollups_stale "$PREVIOUS"  # it keeps billing while the migrations run
fi
SWAPPED=0

failed() {
  log "$TARGET: deploy of $SHA FAILED: $1"
  if [ -n "$PREVIOUS" ]; then
    if [ "$SWAPPED" = 1 ]; then
      restore "$IMAGE:rollback" || true
    else
      # Nothing was swapped, the old containers still serve; :latest points at them again so that a restart or a
      # manual `docker compose up -d` does not start the failed image.
      docker tag "$IMAGE:rollback" "$IMAGE:latest"
      log "$TARGET: nothing was swapped, the previous version is still serving"
    fi
  fi
  exit 1
}

# 2. The image
if [ "$IMAGE_SOURCE" = ghcr ]; then
  [ -n "$GHCR_IMAGE" ] || failed "IMAGE_SOURCE=ghcr needs GHCR_IMAGE in deploy/.env"
  docker pull --quiet "$GHCR_IMAGE:$SHA" || failed "could not pull $GHCR_IMAGE:$SHA (did the image job finish?)"
  docker tag "$GHCR_IMAGE:$SHA" "$IMAGE:latest" || failed "docker tag"
else
  docker compose build web || failed "image build"
fi
docker tag "$IMAGE:latest" "$IMAGE:$SHA" || failed "docker tag"

# 3. Settings and migrations, while the old version keeps serving
if docker compose config --services | grep -qx migrate; then
  docker compose up -d --no-build mongo redis || failed "database containers"
  docker compose run --rm --no-TTY --no-deps migrate python -m settings || failed "settings check"
  docker compose run --rm --no-TTY migrate || failed "migrations"
fi

# 4. Swap and wait
SWAPPED=1
docker compose up -d --no-build --remove-orphans || failed "docker compose up"
NEW=$(docker image inspect -f '{{.Id}}' "$IMAGE:latest")
if wait_ready && [ "$(running_image)" = "$NEW" ]; then
  log "$TARGET: healthy on 127.0.0.1:$PORT with $SHA"
  echo "$SHA" > "$STATE/good"
  docker compose logs --tail 6 web || true
  prune_images
  # 6. The new version is serving and stays; a failed rebuild is repeated by the next deploy, or by hand.
  rebuild_rollups || { echo "the new version is serving; rebuild the daily rollups: docs/RUNBOOK.md" >&2; exit 1; }
  exit 0
fi

docker compose logs --tail 40 web >&2 || true
failed "not ready within $READY_TIMEOUT s"
