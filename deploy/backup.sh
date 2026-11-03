#!/usr/bin/env bash
# Daily MongoDB backup of this compose project, until the database moves to Atlas (which has its own backups).
#   crontab -e:  0 2 * * * /home/ubuntu/Plant_Tech/deploy/backup.sh >> /home/ubuntu/Plant_Tech/deploy/backup.log 2>&1
#
#   1. mongodump --oplog (one consistent point in time) straight into age: the plain dump never touches the disk
#   2. kept in deploy/backups/ for BACKUP_KEEP_DAYS (default 14)
#   3. copied off the VM with rclone (Cloudflare R2 or Backblaze B2) and checked there
#   4. a heartbeat ping on success (and on failure, if a fail URL is set), so a missed backup raises an alert
#
# Settings, in deploy/.env (docs/RUNBOOK.md, Backups, has the one-time setup):
#   BACKUP_AGE_RECIPIENTS_FILE  file of age public keys (age1...), one per line. The private key is NOT on the VM.
#   BACKUP_RCLONE_REMOTE        e.g. r2-backup:hara-hisaab-backups/prod (a remote in ~/.config/rclone/rclone.conf)
#   BACKUP_HEARTBEAT_URL        pinged on success (Better Stack / Healthchecks.io / UptimeRobot heartbeat URL)
#   BACKUP_HEARTBEAT_FAIL_URL   pinged on failure (optional)
#   BACKUP_KEEP_DAYS            local copies, default 14. Remote copies: a lifecycle rule on the bucket (35 days).
# Without BACKUP_AGE_RECIPIENTS_FILE it still makes the plain local dump it always made (and says so), but never
# uploads a plain dump.
set -euo pipefail
cd "$(dirname "$0")"

env_get() { grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }
RECIPIENTS=$(env_get BACKUP_AGE_RECIPIENTS_FILE)
REMOTE=$(env_get BACKUP_RCLONE_REMOTE)
HEARTBEAT=$(env_get BACKUP_HEARTBEAT_URL)
HEARTBEAT_FAIL=$(env_get BACKUP_HEARTBEAT_FAIL_URL)
KEEP_DAYS=$(env_get BACKUP_KEEP_DAYS); KEEP_DAYS=${KEEP_DAYS:-14}
PROJECT=$(env_get PROJECT_NAME); PROJECT=${PROJECT:-nurserypos}

ping_url() { [ -z "$1" ] || curl -fsS -m 10 --retry 3 -o /dev/null "$1" || echo "heartbeat ping failed" >&2; }
fail() {
  echo "$(date -u +%FT%TZ) backup FAILED: $1" >&2
  ping_url "$HEARTBEAT_FAIL"
  exit 1
}
trap 'fail "line $LINENO"' ERR

mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASE="backups/$PROJECT-$STAMP.archive.gz"
started=$(date +%s)

if [ -z "$RECIPIENTS" ]; then
  echo "WARNING: BACKUP_AGE_RECIPIENTS_FILE is not set: plain local dump only, nothing leaves the VM" >&2
  docker compose exec -T mongo mongodump --archive --gzip --oplog --quiet > "$BASE.part"
  mv "$BASE.part" "$BASE"
  find backups -name "$PROJECT-*.archive.gz" -mtime +"$KEEP_DAYS" -delete
  fail "backups are not encrypted or off the VM yet"  # alerts until it is set up
fi

command -v age >/dev/null || fail "age is not installed (sudo apt install age)"
[ -s "$RECIPIENTS" ] || fail "no age public keys in $RECIPIENTS"
FILE="$BASE.age"
docker compose exec -T mongo mongodump --archive --gzip --oplog --quiet | age -R "$RECIPIENTS" -o "$FILE.part"
[ "$(stat -c %s "$FILE.part")" -gt 1024 ] || fail "the dump is suspiciously small"
mv "$FILE.part" "$FILE"
sha256sum "$FILE" | sed 's#backups/##' > "$FILE.sha256"
dumped=$(date +%s)

if [ -n "$REMOTE" ]; then
  command -v rclone >/dev/null || fail "rclone is not installed (https://rclone.org/install/)"
  # copyto compares size and checksum after the upload; check re-reads both files from the remote
  rclone copyto --quiet "$FILE" "$REMOTE/$(basename "$FILE")"
  rclone copyto --quiet "$FILE.sha256" "$REMOTE/$(basename "$FILE").sha256"
  rclone check --quiet --one-way "$(dirname "$FILE")" "$REMOTE" --include "$(basename "$FILE")*"
else
  fail "BACKUP_RCLONE_REMOTE is not set: the encrypted backup is only on this VM"
fi

find backups \( -name "$PROJECT-*.archive.gz.age" -o -name "$PROJECT-*.archive.gz.age.sha256" \
  -o -name "$PROJECT-*.archive.gz" \) -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) backup ok: $(basename "$FILE") $(stat -c %s "$FILE") bytes," \
  "dump $((dumped - started)) s, upload $(( $(date +%s) - dumped )) s"
ping_url "$HEARTBEAT"
