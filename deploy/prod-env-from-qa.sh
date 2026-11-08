#!/usr/bin/env bash
# Make production's settings file (~/Plant_Tech/deploy/.env) from QA's, for production's first deploy of this
# version. Run by the owner on the VM, once, before the release:
#     bash ~/Plant_Tech-staging/deploy/prod-env-from-qa.sh
# (from QA's checkout: production's checkout still has the old version until the release.)
#
# QA's settings are the starting point (the Claude key, the photo storage keys, the limits), and these are made
# production's own: ENV=production, a new JWT_SECRET (logins of the two never mix), production's own photo
# folder (STORAGE_PREFIX), production's address, no CORS (the phone app needs none), production's compose name
# and port as they are today, and ALLOW_NO_ROLLBACK=1 for the first deploy (remove it after the release). Lines
# that only make sense for QA (heartbeats, the backup folder) are left out; set production's own later
# (docs/RUNBOOK.md, Backups). The old file is kept as .env.before-<time>. No value is printed.
set -euo pipefail

QA_ENV=${QA_ENV:-$HOME/Plant_Tech-staging/deploy/.env}
PROD_ENV=${PROD_ENV:-$HOME/Plant_Tech/deploy/.env}
PROD_URL=${PROD_URL:-https://nurserypos.140-238-251-141.sslip.io}

die() { echo "STOPPED: $*" >&2; exit 1; }
get() { grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }

[ -f "$QA_ENV" ] || die "QA's settings file $QA_ENV not found"
[ "$(get "$QA_ENV" ENV)" = staging ] || die "$QA_ENV does not say ENV=staging"
[ -d "$(dirname "$PROD_ENV")" ] || die "production's folder $(dirname "$PROD_ENV") not found"
if [ -f "$PROD_ENV" ] && [ "$(get "$PROD_ENV" ENV)" = production ] && [ "${1:-}" != --again ]; then
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
  grep -vE '^(ENV|PROJECT_NAME|HOST_PORT|STORAGE_PREFIX|PUBLIC_BASE_URL|CORS_ORIGINS|JWT_SECRET|ALLOW_NO_ROLLBACK|HEARTBEAT_URL|BACKUP_[A-Z_]*|MONGO_URL)=' "$QA_ENV" \
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

if [ -f "$PROD_ENV" ]; then
  cp -p "$PROD_ENV" "$PROD_ENV.before-$(date -u +%Y%m%dT%H%M%SZ)"
fi
mv "$NEW" "$PROD_ENV"
chmod 600 "$PROD_ENV"
echo "==> wrote $PROD_ENV from QA's settings (the old one is kept as .env.before-<time>)"
echo "    production's own: ENV, PROJECT_NAME=$PROJECT, HOST_PORT=$PORT, STORAGE_PREFIX=$PREFIX, PUBLIC_BASE_URL,"
echo "    CORS_ORIGINS (none), a new JWT_SECRET, ALLOW_NO_ROLLBACK=1 (remove after the release)"
echo "    left out (QA's own): HEARTBEAT_URL, BACKUP_*, MONGO_URL"
