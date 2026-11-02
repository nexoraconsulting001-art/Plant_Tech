#!/usr/bin/env bash
# Restore drill: prove a backup made by backup.sh can be read back, and time it. Run it monthly, and after any
# change to backup.sh, on a machine that is NOT the VM (a laptop or a scratch VM with docker, age and rclone):
#
#   deploy/restore-drill.sh --identity ~/keys/hara-hisaab-backup.agekey --remote r2-backup:hara-hisaab-backups/prod
#   deploy/restore-drill.sh --identity <key> --file nurserypos-20261102T020000Z.archive.gz.age
#
# It fetches the newest backup (or the given file), checks its sha256, decrypts it into a throwaway mongo:7.0
# container, replays the oplog, prints the row counts of the main collections and how old the newest bill is, then
# removes the container. Nothing is written anywhere else. Exit 0 = the backup restores and has data.
# Add the "drill:" line it prints to the drill log in docs/RUNBOOK.md.
set -euo pipefail

IDENTITY="" REMOTE="" FILE="" KEEP=0 IMAGE=${DRILL_MONGO_IMAGE:-mongo:7.0}
while [ $# -gt 0 ]; do
  case "$1" in
    --identity) IDENTITY=$2; shift 2 ;;
    --remote)   REMOTE=$2; shift 2 ;;
    --file)     FILE=$2; shift 2 ;;
    --keep)     KEEP=1; shift ;;   # leave the container running to look around (docker exec -it <name> mongosh)
    *) echo "usage: restore-drill.sh --identity <age key file> (--remote <rclone remote> | --file <backup.age>) [--keep]" >&2
       exit 2 ;;
  esac
done
[ -n "$IDENTITY" ] && [ -r "$IDENTITY" ] || { echo "need --identity <age private key file>" >&2; exit 2; }
[ -n "$REMOTE" ] || [ -n "$FILE" ] || { echo "need --remote or --file" >&2; exit 2; }
for tool in docker age sha256sum; do command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 2; }; done

WORK=$(mktemp -d)
NAME="hh-restore-drill-$$"
cleanup() {
  rm -rf "$WORK"
  if [ "$KEEP" = 0 ]; then docker rm -f "$NAME" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
t0=$(date +%s)

# 1. Fetch
if [ -n "$REMOTE" ]; then
  command -v rclone >/dev/null || { echo "rclone is not installed" >&2; exit 2; }
  if [ -z "$FILE" ]; then
    FILE=$(rclone lsf --files-only --include '*.archive.gz.age' "$REMOTE" | sort | tail -n 1)
    [ -n "$FILE" ] || { echo "no backups in $REMOTE" >&2; exit 1; }
  fi
  rclone copyto "$REMOTE/$FILE" "$WORK/$FILE"
  rclone copyto "$REMOTE/$FILE.sha256" "$WORK/$FILE.sha256"
  SRC="$WORK/$FILE"
else
  SRC=$FILE
fi
if [ -f "$SRC.sha256" ]; then
  (cd "$(dirname "$SRC")" && sha256sum -c "$(basename "$SRC").sha256" >/dev/null) || { echo "checksum mismatch" >&2; exit 1; }
else
  echo "warning: no $SRC.sha256 next to the backup, checksum not checked" >&2
fi
t1=$(date +%s)

# 2. A throwaway MongoDB
docker run -d --name "$NAME" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  docker exec "$NAME" mongosh --quiet --eval 'db.runCommand({ping: 1}).ok' >/dev/null 2>&1 && break
  sleep 1
done

# 3. Decrypt straight into mongorestore (the plain dump never touches the disk)
age -d -i "$IDENTITY" "$SRC" | docker exec -i "$NAME" mongorestore --archive --gzip --oplogReplay --quiet
t2=$(date +%s)

# 4. Is the data there?
REPORT=$(docker exec "$NAME" mongosh --quiet --eval '
  const skip = ["admin", "config", "local"];
  let users = 0, newest = "";
  for (const d of db.adminCommand({listDatabases: 1}).databases) {
    if (skip.includes(d.name)) continue;
    const x = db.getSiblingDB(d.name);
    const n = (c) => x.getCollectionNames().includes(c) ? x[c].estimatedDocumentCount() : 0;
    const last = n("invoices") ? (x.invoices.find({}, {created_at: 1}).sort({created_at: -1}).limit(1).toArray()[0] || {}).created_at : "";
    print(`${d.name}: businesses=${n("businesses")} users=${n("users")} products=${n("products")} customers=${n("customers")} invoices=${n("invoices")} stock_history=${n("stock_history")} newest_bill=${last || "-"}`);
    users += n("users");
    if (last && String(last) > newest) newest = String(last);
  }
  print(`TOTAL users=${users} newest_bill=${newest || "-"}`);
')
echo "$REPORT"
t3=$(date +%s)

SIZE=$(stat -c %s "$SRC" 2>/dev/null || wc -c < "$SRC")
echo "drill: $(date -u +%F) $(basename "$SRC") ${SIZE} bytes; fetch $((t1 - t0)) s, restore $((t2 - t1)) s," \
  "checks $((t3 - t2)) s, total $((t3 - t0)) s"
if [ "$KEEP" = 1 ]; then echo "container $NAME kept: docker exec -it $NAME mongosh; docker rm -f $NAME when done"; fi
echo "$REPORT" | grep -q "^TOTAL users=[1-9]" || { echo "restored, but there are no users: wrong backup?" >&2; exit 1; }
