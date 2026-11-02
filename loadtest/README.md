# Load tests: the billing path at 1x, 3x and 5x the design peak

The design peak is **14 bills/s** (1000 shops x 200 bills a day, busiest two hours, x2 for bursts; see
`docs/PRODUCTION-PLAN.md` section 1). Each scenario iteration is one whole bill, made the way the app makes it:

| Step | Requests |
|---|---|
| Customer screen opens (recent customers), then a name is typed | `GET /api/customers/find` x 2-4 |
| The customer's saved order | `GET /api/orders/cart` |
| 2-5 products, each found by typing (one request per kept keystroke) | `GET /api/products?search=` x 4-15 |
| The order saved after each product | `PUT /api/orders/cart` x 2-5 |
| Pay screen: fresh stock | `GET /api/products?ids=` |
| The bill, with an `Idempotency-Key` header (retried once with the same key on a network error or 5xx) | `POST /api/invoices` |
| Bill screen and Home refresh | `GET /api/invoices/{id}`, `GET /api/dashboard/today` |
| Half the time: the WhatsApp link and the customer opening it | `GET /api/invoices/{id}/share`, `GET /i/{token}` |

That is about 18 requests per bill (the plan's 14-16 after the app fixes, plus the search typing), so 14 bills/s
is about 250 requests/s. Images are not included (they move to the CDN in WP-7).

| Run | Bills/s | Requests/s | Pass |
|---|---|---|---|
| `MULT=1` | 14 | ~250 | p95 `POST /api/invoices` < 500 ms, errors < 1% |
| `MULT=3` | 42 | ~750 | the same: this is the capacity the plan asks for (section 2) |
| `MULT=5` | 70 | ~1250 | headroom: find where it breaks, and what breaks first |

## 1. Test data
Never against production: the test creates real bills. Use a staging-like server with its **own** database.
```bash
cd backend
# JWT_SECRET = the target server's: shops.json then holds a login token per shop (no burst of bcrypt logins)
JWT_SECRET=<target's secret> python ../loadtest/seed.py \
    --mongo-url "<target's MONGO_URL>" --db hh_loadtest --shops 1000 --products 300 --customers 2000 --drop
```
- 1000 shops with 300 products and 2000 customers each: a few minutes and roughly 1 GB (50 shops took 5 s locally).
- The database name must contain `loadtest`. The server under test needs `MONGO_DB=hh_loadtest`.
- It writes `loadtest/shops.json` (logins: never commit it; `.gitignore` keeps it out).
- Seed a fresh database: the script runs the migrations afterwards, so indexes and backfills cover the seeded rows.

## 2. k6 (preferred)
[Install k6](https://grafana.com/docs/k6/latest/set-up/install-k6/), then from the repo root:
```bash
k6 run -e BASE_URL=https://<staging api> -e MULT=1 --summary-export loadtest/results/k6-1x.json loadtest/k6/bill.js
k6 run -e BASE_URL=https://<staging api> -e MULT=3 -e DURATION=15m --summary-export loadtest/results/k6-3x.json loadtest/k6/bill.js
k6 run -e BASE_URL=https://<staging api> -e MULT=5 --summary-export loadtest/results/k6-5x.json loadtest/k6/bill.js
```
Options: `DURATION` (hold, default `10m`), `RAMP` (default `1m`), `THINK` (pause multiplier, default 1; 0 =
back-to-back), `PEAK` (bills/s at MULT=1), `SHOPS_FILE`. k6 exits non-zero when a threshold fails (see the top of
`k6/bill.js`). Run k6 from a machine close to the server (same region), not from a home connection: at 5x it needs
about 1000 virtual users.

## 3. Locust (same scenario, Python)
`locust` is in `backend/requirements-dev.txt`.
```bash
MULT=3 locust -f loadtest/locustfile.py --host https://<staging api> --headless --processes -1 \
    --csv loadtest/results/locust-3x --html loadtest/results/locust-3x.html
```
Each user makes one bill every `PACE` seconds (default 20), so the shape starts `14 x MULT x PACE` users. On Windows
`--processes` is not available and gevent needs a short install path; run it from Linux or WSL for the big runs.

## 4. What to watch while it runs
- The API: `docker compose stats`, `/metrics` (request rate, p95 per route), the JSON request log.
- MongoDB (Atlas): operation execution time, tickets available, cache dirty %, "scanned / returned" (anything but
  an index scan on the hot queries is a bug; WP-1 to WP-4 add the indexes).
- Redis and the worker: queue length should stay near zero.
Record each run (date, commit, server size, results) in `docs/RUNBOOK.md` under "Capacity".

## Local smoke run (2 Nov 2026, laptop, one uvicorn process, before WP-1 to WP-4)
60 shops, `PEAK=2 PACE=6 DURATION=45 THINK=0.2`, locust: 1.84 bills/s, ~34 requests/s, no errors,
`POST /api/invoices` p50 160 ms / p95 520 ms. This only proves the scripts and the seeded data work end to end;
it says nothing about server capacity (one process on Windows, product search still scores in Python).
