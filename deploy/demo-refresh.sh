#!/usr/bin/env bash
# Keeps a demo shop looking fresh: once a day every date in the shop moves forward, so its newest day of
# orders is today again. Runs only on a shop marked demo (backend/demo_data.py seed marks it), and never on
# production: bills there are tax records, so moving their dates is refused here and again in demo_data.py
# (ENV=production).
# Cron on the VM (UTC), daily at 5:30 am India time:
#   0 0 * * * bash /home/ubuntu/Plant_Tech-staging/deploy/demo-refresh.sh staging 9654346434 >> /home/ubuntu/nurserypos-demo.log 2>&1
# First time for a shop: docker compose exec web python demo_data.py seed --phone <owner phone>
set -euo pipefail

case "${1:-}" in
  staging) DIR="$HOME/Plant_Tech-staging" ;;
  prod) echo "demo-refresh: refused, demo data is never refreshed on production" >&2; exit 3 ;;
  *) echo "usage: demo-refresh.sh staging <owner phone>" >&2; exit 2 ;;
esac
PHONE="${2:?owner phone}"

cd "$DIR/deploy"
docker compose exec -T web python demo_data.py refresh --phone "$PHONE" </dev/null
