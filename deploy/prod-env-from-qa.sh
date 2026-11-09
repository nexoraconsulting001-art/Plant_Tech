#!/usr/bin/env bash
# Make production's settings file (~/Plant_Tech/deploy/.env) from QA's, for production's first deploy of this
# version. Run by the owner on the VM, once, before the release (docs/PROD-RELEASE.md, step A1). Before the
# release neither checkout on the VM has this file yet, so it is taken from GitHub without deploying anything:
#     cd ~/Plant_Tech-staging && git fetch -q origin prod-ready && git show origin/prod-ready:deploy/prod-env-from-qa.sh > /tmp/prod-env-from-qa.sh && bash /tmp/prod-env-from-qa.sh
#
# QA's settings are the starting point (the Claude key, the photo storage keys, the limits), and these are made
# production's own: ENV=production, a new JWT_SECRET (logins of the two never mix), production's own photo
# folder (STORAGE_PREFIX), production's address, no CORS (the phone app needs none), production's compose name
# and port as they are today, and ALLOW_NO_ROLLBACK=1 for the first deploy (remove it after the release). Lines
# that only make sense for QA (heartbeats, the backup folder) are left out; set production's own later
# (docs/RUNBOOK.md, Backups). The old file is kept as .env.before-<time>. No value is printed.
# --keep-own-keys: production's own Claude and R2 keys already in its file (ANTHROPIC_API_KEY, R2_*) are kept
#   instead of QA's (the owner gave production its own key and bucket, 9 Nov).
# --again: make it again over a file that already says ENV=production.
set -euo pipefail

QA_ENV=${QA_ENV:-$HOME/Plant_Tech-staging/deploy/.env}
PROD_ENV=${PROD_ENV:-$HOME/Plant_Tech/deploy/.env}
PROD_URL=${PROD_URL:-https://nurserypos.140-238-251-141.sslip.io}

die() { echo "STOPPED: $*" >&2; exit 1; }
get() { grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }

[ -f "$QA_ENV" ] || die "QA's settings file $QA_ENV not found"
# No ENV line means staging (compose.yaml: ENV defaults to staging); anything else is not QA's file.
QA_IS=$(get "$QA_ENV" ENV); QA_IS=${QA_IS:-$(get "$QA_ENV" APP_ENV)}; QA_IS=${QA_IS:-staging}
[ "$QA_IS" = staging ] || die "$QA_ENV says ENV=$QA_IS, not staging: is this QA's settings file?"
[ -d "$(dirname "$PROD_ENV")" ] || die "production's folder $(dirname "$PROD_ENV") not found"
AGAIN=0; KEEP_OWN=0
for a in "$@"; do
  case "$a" in --again) AGAIN=1 ;; --keep-own-keys) KEEP_OWN=1 ;; *) die "unknown option $a (--again, --keep-own-keys)" ;; esac
done
OWN_KEYS="ANTHROPIC_API_KEY R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET R2_PUBLIC_BUCKET"
if [ -f "$PROD_ENV" ] && [ "$(get "$PROD_ENV" ENV)" = production ] && [ "$AGAIN" != 1 ]; then
  die "$PROD_ENV already says ENV=production (made before?). To make it again from QA's: add --again"
fi
command -v openssl >/dev/null || die "openssl is needed (sudo apt install openssl)"

# Production's compose name and port stay as they are now (else a second production would start beside it).
PROJECT=$( [ -f "$PROD_ENV" ] && get "$PROD_ENV" PROJECT_NAME || true ); PROJECT=${PROJECT:-nurserypos}
PORT=$( [ -f "$PROD_ENV" ] && get "$PROD_ENV" HOST_PORT || true ); PORT=${PORT:-8120}
[ "$PROJECT" != "$(get "$QA_ENV" PROJECT_NAME)" ] || die "production and QA would have the same PROJECT_NAME ($PROJECT)"
[ "$PORT" != "$(get "$QA_ENV" HOST_PORT)" ] || die "production and QA would have the same HOST_PORT ($PORT)"
QA_PREFIX=$(get "$QA_ENV" STORAGE_PREFIX); QA_PREFIX=${QA_PREFIX:-nurserypos}
PREFIX=nurserypos; [ "$QA_PREFIX" != nurserypos ] || PREFIX=nurserypos-prod

NEW="$PROD_ENV.new"
umask 077
{
  echo "# Production settings, made from QA's by prod-env-from-qa.sh on $(date -u +%F). Secrets: keep this file private."
  grep -vE '^(ENV|APP_ENV|PROJECT_NAME|HOST_PORT|STORAGE_PREFIX|PUBLIC_BASE_URL|CORS_ORIGINS|JWT_SECRET|ALLOW_NO_ROLLBACK|HEARTBEAT_URL|BACKUP_[A-Z_]*|MONGO_URL)=' "$QA_ENV" \
    | grep -vE '^# Production settings, made from'
  echo
  echo "# production's own"
  echo "ENV=production"
  echo "PROJECT_NAME=$PROJECT"
  echo "HOST_PORT=$PORT"
  echo "STORAGE_PREFIX=$PREFIX"
  echo "PUBLIC_BASE_URL=$PROD_URL"
  echo "CORS_ORIGINS="
  echo "JWT_SECRET=$(openssl rand -hex 32)"
  echo "# first deploy of this version only (production has no users yet); remove this line after the release"
  echo "ALLOW_NO_ROLLBACK=1"
} > "$NEW"
KEPT=""
if [ "$KEEP_OWN" = 1 ] && [ -f "$PROD_ENV" ]; then
  echo "# production's own keys, kept from its previous file" >> "$NEW"
  for k in $OWN_KEYS; do
    line=$(grep -E "^$k=" "$PROD_ENV" | tail -n 1 || true)
    [ -n "${line#*=}" ] || continue
    grep -vE "^$k=" "$NEW" > "$NEW.tmp" || true
    mv "$NEW.tmp" "$NEW"
    printf '%s\n' "$line" >> "$NEW"
    KEPT="$KEPT $k"
  done
fi

if [ -f "$PROD_ENV" ]; then
  cp -p "$PROD_ENV" "$PROD_ENV.before-$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$NEW" "$PROD_ENV"
chmod 600 "$PROD_ENV"
echo "==> wrote $PROD_ENV from QA's settings (the old one is kept as .env.before-<time>)"
echo "    production's own: ENV, PROJECT_NAME=$PROJECT, HOST_PORT=$PORT, STORAGE_PREFIX=$PREFIX, PUBLIC_BASE_URL,"
echo "    CORS_ORIGINS (none), a new JWT_SECRET, ALLOW_NO_ROLLBACK=1 (remove after the release)"
echo "    left out (QA's own): HEARTBEAT_URL, BACKUP_*, MONGO_URL"
[ "$KEEP_OWN" != 1 ] || echo "    kept from production's previous file:${KEPT:- nothing (no own keys found)}"
