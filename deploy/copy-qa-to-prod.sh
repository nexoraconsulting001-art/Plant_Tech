#!/usr/bin/env bash
# Copy QA (staging) into production, exactly: the database (shops, logins, bills, stock, the demo shop) and the
# photos. Run by the owner on the VM, once, on the evening the pilot shops move to the production app:
#     ~/Plant_Tech/deploy/copy-qa-to-prod.sh
# It asks you to type COPY before it changes anything (--yes skips the question).
#
# What it does, in order (docs/RUNBOOK.md, "Copy QA into production"):
#  1. checks: QA and production run the same code version, both are up, the settings make sense, the photo
#     folders differ;
#  2. pauses QA's and production's app servers (web and worker), so nobody writes while it copies (the QA app
#     shows "server busy" meanwhile);
#  3. backs up both databases first, to ~/.nurserypos/copies/<time>/ (QA's, and production's as it was);
#  4. replaces production's database with QA's;
#  5. copies QA's photos into production's own photo folder and points the copied records at them;
#  6. removes the phones registered for notifications (they are QA-app phones; the production app registers its
#     own when it opens);
#  7. compares the number of records in every collection on both sides;
#  8. starts production again and checks it answers; QA is started again in every case, also after a failure.
# Nothing is deleted for good: production's old data is in the backup of step 3.
#
# Settings: QA_DIR and PROD_DIR (defaults ~/Plant_Tech-staging and ~/Plant_Tech), each with deploy/.env.
set -euo pipefail

QA_DIR=${QA_DIR:-$HOME/Plant_Tech-staging}
PROD_DIR=${PROD_DIR:-$HOME/Plant_Tech}
YES=0
[ "${1:-}" = "--yes" ] && YES=1

say() { echo "==> $*"; }
die() { echo "STOPPED: $*" >&2; exit 1; }

env_get() {  # env_get <dir> <NAME>: the value in <dir>/deploy/.env, quotes removed
  local v
  v=$(grep -E "^$2=" "$1/deploy/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  printf '%s' "$v"
}
qa() { (cd "$QA_DIR/deploy" && docker compose "$@"); }
prod() { (cd "$PROD_DIR/deploy" && docker compose "$@"); }
counts() {  # counts <qa|prod> <db>: "collection count" lines, sorted
  "$1" exec -T mongo mongosh --quiet --eval "
    const d = db.getSiblingDB('$2');
    d.getCollectionNames().filter(c => !c.startsWith('system.')).sort()
      .forEach(c => print(c + ' ' + d.getCollection(c).countDocuments({})));"
}
ready() {  # ready <port>: /readyz answers 200 within 90 s
  for _ in $(seq 1 45); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$1/readyz" || true)" = 200 ] && return 0
    sleep 2
  done
  return 1
}

# ---- 1. checks --------------------------------------------------------------------------------------
for d in "$QA_DIR" "$PROD_DIR"; do
  [ -f "$d/deploy/.env" ] || die "$d/deploy/.env not found (QA_DIR / PROD_DIR)"
done
[ "$(cd "$QA_DIR" && pwd -P)" != "$(cd "$PROD_DIR" && pwd -P)" ] || die "QA_DIR and PROD_DIR are the same folder"
[ "$(env_get "$QA_DIR" ENV)" = staging ] || die "QA's deploy/.env must say ENV=staging (got '$(env_get "$QA_DIR" ENV)')"
[ "$(env_get "$PROD_DIR" ENV)" = production ] || die "production's deploy/.env must say ENV=production (got '$(env_get "$PROD_DIR" ENV)')"
for d in "$QA_DIR" "$PROD_DIR"; do
  url=$(env_get "$d" MONGO_URL)
  [ -z "$url" ] || [ "$url" = "mongodb://mongo:27017/?replicaSet=rs0" ] \
    || die "$d/deploy/.env sets MONGO_URL to another database; this script copies between the VM's own databases only"
done
QA_SHA=$(git -C "$QA_DIR" rev-parse HEAD)
PROD_SHA=$(git -C "$PROD_DIR" rev-parse HEAD)
[ "$QA_SHA" = "$PROD_SHA" ] || die "QA runs ${QA_SHA:0:7} and production ${PROD_SHA:0:7}: deploy the same version to both first (push staging and main to the same commit)"
QA_DB=$(env_get "$QA_DIR" DB_NAME); QA_DB=${QA_DB:-$(env_get "$QA_DIR" MONGO_DB)}; QA_DB=${QA_DB:-nurserypos}
PROD_DB=$(env_get "$PROD_DIR" DB_NAME); PROD_DB=${PROD_DB:-$(env_get "$PROD_DIR" MONGO_DB)}; PROD_DB=${PROD_DB:-nurserypos}
QA_PORT=$(env_get "$QA_DIR" HOST_PORT); QA_PORT=${QA_PORT:-8120}
PROD_PORT=$(env_get "$PROD_DIR" HOST_PORT); PROD_PORT=${PROD_PORT:-8120}
QA_PREFIX=$(env_get "$QA_DIR" STORAGE_PREFIX); QA_PREFIX=${QA_PREFIX:-nurserypos}
PROD_PREFIX=$(env_get "$PROD_DIR" STORAGE_PREFIX); PROD_PREFIX=${PROD_PREFIX:-nurserypos}
[ "$(env_get "$QA_DIR" STORAGE_BACKEND)" = r2 ] || die "QA keeps its photos on local disk (STORAGE_BACKEND); this script copies R2 photos only"
[ "$(env_get "$PROD_DIR" STORAGE_BACKEND)" = r2 ] || die "production's STORAGE_BACKEND must be r2"
if [ "$QA_PREFIX" = "$PROD_PREFIX" ] && [ "$(env_get "$QA_DIR" R2_BUCKET)" = "$(env_get "$PROD_DIR" R2_BUCKET)" ]; then
  die "QA and production would share the photo folder $QA_PREFIX/: give production its own STORAGE_PREFIX (e.g. nurserypos) in its deploy/.env"
fi
pgrep -f demo-refresh.sh >/dev/null 2>&1 && die "the demo shop refresh is running right now; wait a few minutes"
for side in qa prod; do
  "$side" exec -T mongo mongosh --quiet --eval 'db.hello().isWritablePrimary || quit(1)' >/dev/null \
    || die "$side's database is not up (cd into its deploy folder and run: docker compose ps)"
done
ready "$QA_PORT" || die "QA does not answer on 127.0.0.1:$QA_PORT/readyz"
ready "$PROD_PORT" || die "production does not answer on 127.0.0.1:$PROD_PORT/readyz (deploy it first)"

echo
echo "  From QA:         $QA_DIR  (database $QA_DB, photos $QA_PREFIX/, version ${QA_SHA:0:7})"
echo "  Into production: $PROD_DIR  (database $PROD_DB, photos $PROD_PREFIX/)"
echo "  Production's database is REPLACED by QA's (a backup of both is made first)."
echo "  QA and production are paused for a few minutes; QA keeps its data and starts again afterwards."
echo
if [ "$YES" != 1 ]; then
  read -r -p "Type COPY to go ahead: " answer
  [ "$answer" = COPY ] || die "nothing was changed"
fi

# ---- 2. pause both ---------------------------------------------------------------------------------
QA_PAUSED=0
PROD_CHANGED=0
PROD_OK=0
finish() {
  if [ "$QA_PAUSED" = 1 ]; then
    say "starting QA again"
    qa start web worker >/dev/null 2>&1 || echo "WARNING: QA did not start again: cd $QA_DIR/deploy && docker compose start web worker" >&2
  fi
  if [ "$QA_PAUSED" = 1 ] && [ "$PROD_CHANGED" = 0 ]; then
    say "production was not changed; starting it again"
    prod start web worker >/dev/null 2>&1 || true
  elif [ "$PROD_OK" != 1 ]; then
    echo >&2
    echo "Production is left paused. Its data before this run is in $OUT/prod-before.archive.gz; to put it back:" >&2
    echo "  cd $PROD_DIR/deploy && docker compose exec -T mongo mongorestore --archive --gzip --drop < $OUT/prod-before.archive.gz && docker compose start web worker" >&2
  fi
}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/.nurserypos/copies/$STAMP"
mkdir -p "$OUT" && chmod 700 "$OUT"
trap finish EXIT

say "pausing QA and production (web, worker)"
QA_PAUSED=1
qa stop web worker >/dev/null
prod stop web worker >/dev/null

# ---- 3. backups ------------------------------------------------------------------------------------
say "backing up QA's database"
qa exec -T mongo mongodump --archive --gzip --db "$QA_DB" --quiet > "$OUT/qa.archive.gz"
say "backing up production's database as it is now"
prod exec -T mongo mongodump --archive --gzip --quiet > "$OUT/prod-before.archive.gz"
[ "$(stat -c %s "$OUT/qa.archive.gz")" -gt 1024 ] || die "QA's backup is suspiciously small; nothing was changed in production"
chmod 600 "$OUT"/*.archive.gz
ls -l "$OUT"
counts qa "$QA_DB" > "$OUT/qa-counts.txt"

# ---- 4. production's database = QA's ------------------------------------------------------------------
say "replacing production's database with QA's"
PROD_CHANGED=1
prod exec -T mongo mongosh --quiet --eval "db.getSiblingDB('$PROD_DB').dropDatabase()" >/dev/null
prod exec -T mongo mongorestore --archive --gzip --nsInclude "$QA_DB.*" --nsFrom "$QA_DB.*" --nsTo "$PROD_DB.*" \
  --quiet < "$OUT/qa.archive.gz"

# ---- 5. photos -------------------------------------------------------------------------------------
# QA's photo settings go to the copy tool as SRC_<name>, passed by name (-e NAME) so no key is on a command line.
src_vars=()
for n in STORAGE_BACKEND STORAGE_PREFIX R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET R2_PUBLIC_BUCKET R2_ENDPOINT_URL; do
  export "SRC_$n=$(env_get "$QA_DIR" "$n")"
  src_vars+=(-e "SRC_$n")
done
export SRC_STORAGE_PREFIX="$QA_PREFIX"
say "copying photos from $QA_PREFIX/ to $PROD_PREFIX/"
prod run --rm --no-deps -T "${src_vars[@]}" migrate python -m env_copy photos
say "pointing the copied records at production's photos"
prod run --rm --no-deps -T "${src_vars[@]}" migrate python -m env_copy rewrite

# ---- 6. notifications -------------------------------------------------------------------------------
prod run --rm --no-deps -T migrate python -m env_copy clear-push

# ---- 7. compare ------------------------------------------------------------------------------------
say "comparing record counts"
prod run --rm --no-deps -T migrate python -m migrations up
counts prod "$PROD_DB" > "$OUT/prod-counts.txt"
if diff <(grep -v '^push_tokens ' "$OUT/qa-counts.txt") <(grep -v '^push_tokens ' "$OUT/prod-counts.txt"); then
  echo "  every collection has the same number of records on both sides ($(wc -l < "$OUT/qa-counts.txt") collections)"
else
  die "the counts differ (above: < QA, > production)"
fi

# ---- 8. production on ------------------------------------------------------------------------------
say "starting production"
prod start web worker >/dev/null
ready "$PROD_PORT" || die "production does not answer on 127.0.0.1:$PROD_PORT/readyz after the copy"
PROD_OK=1
say "done: production now has QA's data (backups and counts in $OUT)"
echo "  Next: log in on the production app with your usual number and password, and open today's bills."
echo "  Keep $OUT for a week (it holds the shops' data), then delete it: rm -r $OUT"
