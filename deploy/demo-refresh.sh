#!/usr/bin/env bash
# Keeps a demo shop looking fresh: once a day every date in the shop moves forward, so its newest day of
# orders is today again. Runs only on a shop marked demo (backend/demo_data.py seed marks it; demo_data.py refuses
# any other shop). On production (owner, 11 Nov 2026) the mark can only be on a shop that was new and empty when
# it was seeded, so real shops' bills (tax records) are never moved.
# Cron on the VM (UTC), daily at 5:30 am India time:
#   0 0 * * * bash /home/ubuntu/Plant_Tech-staging/deploy/demo-refresh.sh staging 9654346434 >> /home/ubuntu/nurserypos-demo.log 2>&1
#   2 0 * * * bash /home/ubuntu/Plant_Tech/deploy/demo-refresh.sh prod 9654346434 >> /home/ubuntu/nurserypos-demo-prod.log 2>&1
# First time for a shop: docker compose exec web python demo_data.py seed --phone <owner phone>
set -euo pipefail

case "${1:-}" in
  staging) DIR="$HOME/Plant_Tech-staging" ;;
  prod) DIR="$HOME/Plant_Tech" ;;
  *) echo "usage: demo-refresh.sh staging|prod <owner phone>" >&2; exit 2 ;;
esac
PHONE="${2:?owner phone}"

cd "$DIR/deploy"
docker compose exec -T web python demo_data.py refresh --phone "$PHONE" </dev/null
