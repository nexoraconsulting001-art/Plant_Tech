# Hara Hisaab: production readiness and scale plan (target: 1000 nurseries × 200 bills/day)

Written 2 Nov 2026 from a read-only audit of `staging` at 9ccb713 (seven auditors plus a cross-check). Built on branch `prod-ready`; see the status section at the end.

---

## 0. Spot-check of the auditors' main P0/P1 claims

| # | Claim | Evidence in the code | Verdict |
|---|---|---|---|
| 1 | A sale is several separate writes with no transaction. Stock is taken first, then the bill is saved, and a hand-written undo cleans up on failure. | `backend/routers/sales.py:279-293` (`move.take` → `_save` → `except: move.undo()`), `:178-228` (one `find_one_and_update` per product, then `undo`), `:250-276` | **Confirmed** |
| 2 | Mongo is a single standalone node with a 0.25 GB cache, no replica set and no auth. | `deploy/compose.yaml:18-23` | **Confirmed** |
| 3 | The invoices collection has only single-field indexes. Phone and google_sub on users, phone on customers, and ai_usage are not unique. | `backend/server.py:33,37,38,42` | **Confirmed** |
| 4 | The Orders tab defaults to "all dates", and its summary loads every matching bill, capped at 200,000. | `frontend/src/components/orders-view.tsx:36` (`"all"`), `backend/routers/sales.py:467` (`to_list(200000)`) | **Confirmed** |
| 5 | Dashboard and reports load full bill documents into Python, capped at 5000 or 50000. | `sales.py:549-550` (`to_list(5000)`), `:607-608` (`to_list(50000)`) | **Confirmed** |
| 6 | Library search, and matching a new product to the library, read the whole global library collection in Python on every request. | `backend/routers/library.py:79`, `:93`. Product search loads up to 5000 products and scores them in Python: `backend/routers/products.py:148-150`, `:170-171` | **Confirmed** |
| 7 | The API runs as one uvicorn process from a shell-form CMD. bcrypt runs synchronously inside async routes. | `backend/Dockerfile:14`, `backend/requirements.txt:1-2` (fastapi 0.110.1, uvicorn 0.25.0 without `[standard]`), `backend/auth.py:24-34` | **Confirmed** |
| 8 | Photo decoding runs synchronously inside the async Claude call. The Claude client and the Mongo client both use library defaults. | `backend/ai_client.py:165-176` (`_image_block` → `normalize_photo`), `:31-35` (`AsyncAnthropic()`), `backend/db.py:12` (`AsyncIOMotorClient(url)` with no options) | **Confirmed.** The SDK default is a 10-minute timeout with 2 retries. |
| 9 | Photo storage defaults to local disk. The summary loop and fire-and-forget tasks run inside the web process. | `backend/storage_client.py:5` (`"local"`), `backend/server.py:78`, `backend/push.py:61`, `backend/routers/library.py:105` | **Confirmed** |
| 10 | Bill creation has no idempotency key, and after a bill every cached query is invalidated. | `frontend/src/order.ts:33-59`, `:61` (`qc.invalidateQueries()` with no filter) | **Confirmed** |
| 11 | "Bill without GST" sets GST to 0 on each line but leaves the price as typed, so the customer pays the same total and the bill shows no tax. | `sales.py:163`, `:238` | **Confirmed.** This is a compliance P0 and needs an owner decision. |
| 12 | Product search on the scan screen has no debounce. The customer search fires with an empty query when the screen opens. A failed cart load shows an empty cart. | `frontend/app/order/scan.tsx:129-134`, `frontend/app/order/customer.tsx:41`, `frontend/src/cart-context.tsx:131-133` | **Confirmed** |

**Corrections and dropped points:**
- The ops auditor cited `files.py:175-183` for the photo proxy. The proxy is at **`backend/routers/files.py:47-55`**.
- The DB auditor counted `settle_stock` as one write per bill line. It writes only when rounding changes the stock (`products.py:66-67`), so it is almost never a write. The corrected write count is in Section 1.
- The DB auditor said the summary loop does per-shop work only from 21:00 to midnight. In fact `send_due_summaries` runs every 60 s all day for every shop with a registered owner phone (`notify.py:145-182`). Before 21:00 the check stops at the time comparison (`notify.py:153`), but it has already read the shop document.
- **Dropped:** the claim that Oracle reclaims idle Always Free instances. A busy production VM is not idle, so it does not apply.
- **Needs checking:** the Play Store "API 35" target-SDK claim. The required level rises every August, so check the current requirement in Play Console.
- **Unverified:** the Starlette CVE numbers. Treat them as unconfirmed until `pip-audit` reports them.
- **Unverified:** the 512-token prompt-cache minimum. The minimum depends on the model (512–4096 tokens). Confirm caching works with `count_tokens` and by checking `cache_read_input_tokens`.
- **Confirmed:** Claude prices (Sonnet 5.5 at $2 / $10 per million tokens; Haiku 5.5 at $0.10 / $0.50 per million for prompts up to 100k tokens) and the fact that Haiku 5.5 has no server-side refusal fallback. Both checked against the Claude API reference dated 2026-10-06.

---

## 1. Capacity model

### 1.1 Bills

| Metric | Value |
|---|---|
| Bills per day | 1000 shops × 200 = **200,000** (6.0M a month, about 73M a year) |
| Daily average over 9am–9pm | 200,000 / 43,200 s = **4.6 bills/s** |
| Busiest 2 hours (25% of the day) | 50,000 / 7,200 s = **6.9 bills/s** sustained |
| Design peak (×2 for bursts within a minute) | **~14 bills/s**; capacity target with headroom: **20 bills/s** |

### 1.2 API requests per bill, counted from the current app code

| Step | Calls | Source |
|---|---|---|
| Customer search on open (fires with an empty `q`) | 1 | `app/order/customer.tsx:41` |
| Customer search while typing (300 ms debounce) | ~2 | `customer.tsx:36-41` |
| New customer `POST /customers` (assume 40% of bills) | 0.4 | `customer.tsx:73` |
| Load the saved cart `GET /orders/cart` | 1 | `src/cart-context.tsx:124` (`load`, via `moveTo`) |
| Delete the previous customer's cart (sometimes) | ~0.2 | `cart-context.tsx:148` |
| Product search, one request per keystroke from 2 characters, about 3.5 lines per bill | **~10** (range 6–16) | `app/order/scan.tsx:129-134` |
| Photo identify `/ai/identify-plant` | ~0.08 | `scan.tsx:59` |
| Cart autosave `PUT /orders/cart` (600 ms debounce, one per burst of changes) | **~5** (range 3–8) | `cart-context.tsx:68, 92-95, 111-121` |
| Pay screen stock check `GET /products?ids=` (staleTime 0) | 1 | `app/order/pay.tsx:49` |
| `POST /invoices` (a second call if stock is short and the owner confirms) | 1–2 | `src/order.ts:33-59` |
| `invalidateQueries()` refetches every mounted query: dashboard, orders list and summary, products list, customers list | **~4** (3–6) | `order.ts:61` |
| Home refreshes on focus after `dismissTo` | 1 | `hooks/useRefreshOnFocus.ts` |
| Bill screen `GET /invoices/{id}`, plus share `GET /invoices/{id}/share` | ~1.8 | `app/bill/[id].tsx:38, 61` |
| Customer opens `/i/{token}` (public link) | ~0.5 | `sales.py:534` |
| **Total today** | **~29 per bill** (about 18 for a quick bill, about 40 for a slow one) | |
| After the app work in WP-12 (debounce, no empty search, targeted invalidation, bill screen filled from the POST response, 1.5 s autosave) | **~14–16 per bill** | |

Image requests are not included above. Every thumbnail is a separate `GET /api/files/...` that goes through FastAPI to R2 (`files.py:47-55`). On a cold cache that is about 5–20 per bill. expo-image's disk cache reduces this over time.

### 1.3 Requests per second

| | Today (~29 per bill) | After app fixes (~15 per bill) |
|---|---|---|
| Daily average (4.6 bills/s) | ~135 rps | ~70 rps |
| Busiest 2 hours (6.9 bills/s) | ~200 rps | ~105 rps |
| Design peak (14 bills/s) | **~410 rps** (plus images) | **~210 rps** |
| Background load | Summary loop: about 1–2 queries per shop per minute, which is 17–33 queries/s per process at 1000 shops (`notify.py:145-182`). This multiplies by the number of workers. | Moves to one worker process (WP-5) |

### 1.4 Database operations

**Writes per bill, counted from the code:**
- 3.5 lines × (one product `find_one_and_update` + one `stock_history.insert_one`) = 7
- counter `$inc` = 1
- invoice insert = 1
- customer `$inc` = 1
- new customer = 0.4
- cart delete = 1
- cart upserts = 5

That is **about 16 writes per bill**: **about 110 writes/s** in the busiest 2 hours, **about 230/s** at design peak, and 3.3M writes a day. Rollups (WP-2) add 1–2 `$inc` per bill.

**Reads:**
- Every request reads the user (`auth.py:92`), and most requests make 2–5 queries. That is **about 1,000–2,000 reads/s at design peak today**.
- Several of those reads are unbounded scans: the library, 5000 products, and the summary that can load 200k bills.
- After WP-1 to WP-4, reads drop to about 300–600/s, and all of them use indexes.

### 1.5 Data growth

| Collection | Per month | Notes |
|---|---|---|
| invoices | 6M documents × ~1.7–2 KB = **10–12 GB logical**, about 4–5 GB on disk | Must be kept 72 months under GST law (CGST s.36), so about 8 years |
| stock_history | 21M rows × ~350 B = **~7 GB logical**, about 2.5–3 GB on disk | Nothing expires today (`server.py:39`) |
| Indexes (after the compound indexes) | ~4–5 GB | |
| customers | ~1.8M new × 0.4 KB = ~0.7 GB | |
| **Total** | **~20 GB logical, about 12–14 GB on disk per month (about 150 GB a year)** | About 1.2 TB over the 8-year retention period, so closed years must be archived |
| Hot working set | ~5–8 GB (last 2–4 weeks of bills plus index pages) | This is what sizes the Atlas tier |

### 1.6 Photos (R2)

- Assumption: about 5 new product photos and about 2 supplier-bill pages per shop per day, at about 300 KB each. Plant-identify photos are not stored unless the user creates a product from them (`scan.tsx:59` against `:366`).
- That is about 7k objects and 1.5–2 GB a day, or **30–60 GB a month**, about 500 GB after one year.
- Storage costs about $0.015/GB-month, so roughly **$1 a month in month one and about $8 a month by month 12**. Egress is free, and writes stay within the operation allowance.
- At thousands of shops scrolling product lists, the cost is not storage but API capacity, because every image goes through Python today. Fix: WP-7 (serve images from the CDN, add thumbnails).

### 1.7 AI calls

**Volume (assumptions, not measured):**

| Item | Per day | Per month |
|---|---|---|
| Plant photos: 15 per shop per day | 15,000 | 450k |
| Bill pages: 2 per shop per day | 2,000 | 60k |
| Library name look-ups (`library.py:104-107`, not counted against any quota): about 3–5 new names per shop per day | 3–5k | |
| **Ceiling at today's cap** (`ai.py:36`, 300 per shop per day) | **300k** | |

At peak (25% of scans in 2 hours) that is about 0.6 calls/s, about 5–10 in flight, and about 150k input tokens a minute. Compare this with the Anthropic organisation's ITPM limit before launch.

**Claude cost at Sonnet 5.5 $2/$10 and Haiku 5.5 $0.10/$0.50 per million tokens:**

| Setup | Per plant photo | Per bill page | Per month |
|---|---|---|---|
| Today: Sonnet, 1568 px square photo (~3.1k image tokens), up to 120 catalogue names in every prompt, no cache | ~$0.0128 | ~$0.031 | **~$7.7k** (≈ ₹6.8 lakh) |
| Optimised: Haiku at 1024 px for plants with about 10% retried on Sonnet; bills on Sonnet at lower effort or Haiku first; no catalogue; cached system prompt | ~$0.001 blended | $0.015–0.03 | **~$1.4k–2.4k** |
| Every shop at the 300-per-day cap | | | **~$115k** (no ceiling exists today) |

---

## 2. Target architecture

```
                 Phones (Expo app)          Customers (WhatsApp bill links)
                        |                              |
              api.<domain>  img.<domain>       bill.<domain>
                        \         |                    /
                 Cloudflare (proxied DNS, TLS Full-strict, WAF,
                 rate-limit rules on /api/auth/* and /i/*,
                 Load Balancing with /readyz health checks)
                   |                     |
         R2 custom domain (CDN,          |
         immutable cache, thumbnails)    |
                              +----------+----------+
                              |   API VM 1..N        |   (stateless; Caddy -> gunicorn +
                              |   gunicorn + Uvicorn |    UvicornWorker, uvloop/httptools,
                              |   workers = vCPU     |    workers = vCPU, compose limits)
                              +----------+----------+
                                         |  enqueue / rate limits / caches
                              +----------v----------+
                              | Redis (managed)      |----> Worker container (replicas: 1)
                              | arq queue, limits    |      arq worker + arq cron:
                              +----------------------+      summaries, push + receipts,
                                         |                  library look-ups, exports,
                              +----------v----------+       retention, R2 -> B2 copy
                              | MongoDB Atlas        |
                              | 3-node replica set,  |  Continuous backup (PITR),
                              | Mumbai; Search later;|  Online Archive (phase 3),
                              | private endpoint or  |  alerts, Performance Advisor
                              | IP list; SCRAM user  |
                              +----------------------+
  Side services: Anthropic (separate prod and staging workspaces with spend limits), Expo push,
  Sentry (API + app), Better Stack / UptimeRobot (uptime + cron heartbeats),
  Grafana Cloud (metrics + logs), GitHub Actions -> GHCR (arm64 image by SHA) -> migrate -> rolling deploy.
```

**Why each component:**
- **Atlas replica set.** It is the only managed MongoDB, it supports transactions (needed for WP-1), and it gives point-in-time restore, encryption at rest, and hosting in India (DPDP).
- **Stateless API behind Cloudflare Load Balancing.** It removes the single point of failure. Cloudflare also gives free WAF, rate limiting and DDoS protection, and fronts R2 with no extra vendor.
- **arq on Redis for the worker.** arq is an asyncio-native job queue that fits FastAPI and Motor/PyMongo async. Celery or Dramatiq are equally standard alternatives.
- **Single-replica scheduler.** Exactly one process runs scheduled jobs, so they are never duplicated across workers.
- **Sentry, uptime checks, Grafana.** These are industry-default tools for error tracking, uptime and metrics, and all have free tiers.

**Sizing at 1000 shops:**
- **API:** 3 Ampere VMs with 4 OCPU / 24 GB each (any 2 can carry the peak; the third covers deploys and failures), 4 gunicorn workers per VM. Mongo `maxPoolSize` 25 per worker gives about 300 connections, well under the Atlas limits (M10: 1500, M30: 3000). The k6 test in WP-11 must show at least 3× the 14 bills/s design peak.
- **Atlas:** M30 (8 GB RAM) up to about 400–600 shops, M40 (16 GB) at 1000 shops. Move up when Atlas shows cache-dirty above 5% or reads served from disk climbing, not by guesswork.
- **Worker:** 1 VM or container with 1–2 OCPU.

### Phases and approximate monthly cost (USD, list prices; check each provider's pricing page before budgeting)

| | Phase 1: pilot to 100 shops (≤20k bills/day) | Phase 2: 100–400 shops | Phase 3: 1000 shops |
|---|---|---|---|
| Compute | 1 **dedicated** prod VM (Oracle pay-as-you-go A1, 4 OCPU / 24 GB, Mumbai or Hyderabad) ≈ $55. API, worker and Redis as compose services on it. Staging stays on the shared Always Free VM. | 2 API VMs + 1 small worker VM ≈ $135 | 3 API VMs + worker ≈ $200 |
| Load balancing / edge | Cloudflare Free (single origin) + domain ≈ $1 | + Cloudflare Load Balancing ≈ $5–10 | ≈ $10–20 |
| Database | Atlas M10 Mumbai ≈ $60–90 incl. backup; move to M20 (≈ $150–190) when metrics say so | Atlas M30 ≈ $450–600 incl. backup and storage | Atlas M30→M40 ≈ $600–1,300 (+ Atlas Search nodes $100–300 if adopted; Online Archive after year 1) |
| Redis | container on the VM (AOF on) $0 | managed (Redis Cloud / Upstash / ElastiCache) $10–30 | $30–60 |
| R2 | < $5 | $5–10 | $5–15 |
| Observability | Sentry Developer (free), UptimeRobot / Better Stack free, Grafana Cloud free | Sentry Team $26–80, logs $0–50 | $80–200 |
| **Infra subtotal** | **~$120–300** | **~$650–950** | **~$0.9k–1.8k** |
| Claude (optimised) | ~$150–250 | ~$0.6k–1k | ~$1.4k–2.4k (as-is: ~$7.7k) |

**What can stay as it is for the first 50–100 shops:**
- One API VM, as long as it is dedicated and runs several gunicorn workers.
- Redis and the worker as containers on that VM.
- Caddy as the origin server.
- Anchored-prefix indexed search instead of Atlas Search.
- Sonnet for reading supplier bills.
- One R2 bucket per environment.
- Keeping the monolith.
- No Online Archive.

**What cannot stay, even for the first paying shop:**
- Production on the shared Always Free VM.
- Standalone Mongo with backups on the same disk.
- The sslip.io hostname baked into builds and bill links.
- One uvicorn process.
- No transactions on bills.
- No Sentry and no uptime monitoring.
- No ceiling on AI spend.
- The "Bill without GST" toggle.

---

## 3. Code work list

Rules for these packages:
- Each package **owns** the files listed for it. No two packages in the same wave edit the same file.
- New indexes go in each package's own numbered migration file under `backend/migrations/`. The runner discovers them by filename, so there is no shared registry file.
- **Compatibility with 1.1.0:** QA APK 1.1.0 (versionCode 2) gets no more over-the-air updates (`docs/PUSH-SETUP.md:14-19`). The server must therefore keep every endpoint and response shape that 1.1.0 uses, for as long as that build is in testers' hands. Packages marked **[COMPAT 1.1.0]** must pass a 1.1.0 regression run (the QA-CHECKLIST billing, orders, photos and login sections) on staging before merging.

### Wave 0: foundation and seams (one developer, merged first, about 1–1.5 weeks)

**WP-0 Platform foundation [COMPAT 1.1.0]**

Owns:
- `backend/requirements.txt`, `requirements-dev.txt`, a lockfile (`uv lock` or `pip-compile --generate-hashes`)
- `backend/Dockerfile`, new `backend/gunicorn.conf.py`
- new `backend/settings.py` (pydantic-settings)
- `backend/db.py`, `backend/server.py`
- new `backend/observability.py`, `backend/ratelimit.py`, `backend/audit.py`, `backend/rollups.py` (stub), `backend/jobs.py` (enqueue helper), `backend/worker.py` (stub WorkerSettings)
- new `backend/migrations/` (runner plus `0001_baseline.py`)
- `backend/tests/conftest.py`
- `deploy/compose.yaml`
- `frontend/package.json` and `yarn.lock` (dependency additions only)

Work:
- **Dependencies:** upgrade FastAPI, Starlette and uvicorn to current releases with uvicorn `[standard]`; add gunicorn. Replace Motor with `pymongo.AsyncMongoClient`, configured explicitly: `maxPoolSize=25`, `serverSelectionTimeoutMS=5000`, `timeoutMS=10000`, `retryWrites`, `w=majority`, `appname`. Add sentry-sdk[fastapi], structlog, asgi-correlation-id, prometheus-fastapi-instrumentator, limits + redis, arq, cachetools and orjson. Move pytest to the dev requirements.
- **Container:** exec-form CMD running gunicorn with UvicornWorker, `--graceful-timeout 30`, `--max-requests`. Run as a non-root `USER`. Pin the base image by digest.
- **Settings:** a validated `Settings` class. In production the app refuses to start without `STORAGE_BACKEND=r2` and all R2 variables, and without a `JWT_SECRET` of at least 32 characters.
- **Health:** `/livez`, and `/readyz` (Mongo ping plus storage check). `/health` stays as an alias for CI and 1.1.0.
- **Start-up is read-only:** the drafts drop, `ensure_indexes`, `seed_library` and `suppliers.backfill` move from `server.py:66-70` into `migrations/0001`, run by `python -m migrations up` under a lock. The summary task is removed from the lifespan (`server.py:78`); the worker takes it over in WP-5.
- **Edge of the app:** `/docs` off in production, CORS restricted. Add `GET /api/config` returning `min_app_version` and feature flags.
- **Tests:** CI and local tests run on a real `mongod --replSet rs0` (GitHub `services:` container, or testcontainers locally).
- **Compose:** a `worker` service, a `redis` service (phase 1), healthchecks, `depends_on: condition: service_healthy`, `deploy.resources.limits`, `pids_limit`, `init: true`, `stop_grace_period: 30s`, and `logging: {driver: local, max-size: 20m, max-file: 5}`.
- **Frontend dependencies:** add `@sentry/react-native`, `@react-native-community/netinfo`, `@shopify/flash-list`, `expo-crypto` and `expo-build-properties`.
- **Seam refactors (pure moves, no change in behaviour):**
  - `settle_stock` and `record_stock` move from `products.py` to new `backend/stock.py`.
  - `_search` and the suggest internals move from `products.py` to new `backend/catalog_search.py`.
  - `/invoices/summary`, `/dashboard/today` and `/reports/*` move from `sales.py` to new `backend/routers/reports.py`. **Include the reports router before `sales.router`** so that `/invoices/summary` is not captured by `/invoices/{invoice_id}` (`sales.py:460` against `:487`).

Acceptance:
- The whole existing test suite passes on the real replica set.
- Start-up performs zero writes.
- Running `migrations up` twice is a no-op.
- A thrown test error reaches Sentry tagged with release = git SHA.
- Logs are JSON with `request_id`, `business_id` and `user_id`.
- Misconfigured production settings make the process exit non-zero.
- The 1.1.0 regression run passes on staging.

Tests:
- Settings validation.
- Migration idempotency.
- `/readyz` returns 503 when Mongo is down.
- A route-order test for `/invoices/summary`.

### Wave 1: run in parallel after WP-0

**WP-1 Sale integrity [COMPAT 1.1.0]** (L)

Owns: `backend/routers/sales.py`, `backend/stock.py`, `backend/routers/purchases.py`, `backend/routers/carts.py`, `migrations/01xx_*`, and new `tests/test_sale_txn.py` and `tests/test_idempotency.py` (existing sales tests are updated here only).

Work:
- **Transactions:** `create_invoice`, `cancel_invoice`, `correct_invoice` and `create_purchase` each run inside `session.with_transaction(...)` with snapshot read concern, majority write concern and primary reads. `StockMove.undo` and `_unmark_cancelled` are deleted. stock_history rows are written with one `insert_many` inside the transaction. `rollups.apply(...)` is called inside the transaction. `notify.stock_changed` is called after commit.
- **Idempotency:** an optional `Idempotency-Key` header on `POST /invoices` and `/correct`, stored with a unique index on `{business_id, idempotency_key}`. A repeated key returns the original invoice.
- **Bill numbers (owner to confirm the format):** the counter is incremented inside the transaction, keyed per financial year, at most 16 characters, with a unique index on `{business_id, invoice_number}`. **The same migration backfills or dedupes existing data first.**
- **Customers:** customers are created at sale time with an upsert using `$setOnInsert`, backed by a unique index on `{business_id, phone}` (dedupe script first).
- **Input limits:** `InvoiceIn.items` capped at `max_length=200`; `allow_inf_nan=False` and `le=` bounds on price and quantity (`sales.py:46-56`). `business_id` added to every filter (`sales.py:205,217,273,333…`).
- **GST toggle:** `gst_removed` is still accepted but **ignored**, pending decision D6.
- **Carts:** `updated_at` becomes a BSON Date with a 7-day TTL index.
- **Public bill page `/i/{token}`:** token expiry and a revoke/rotate action; responses carry `Referrer-Policy: no-referrer` and `Cache-Control: private, no-store`.
- **Indexes:** invoices `{business_id, created_at:-1}`, `{business_id, customer_id, created_at:-1}`, `{business_id, created_by.id, date_key, created_at:-1}`; `public_token` unique; stock_history `{business_id, product_id, created_at:-1}`.

Acceptance:
- **Fault injection:** a forced exception between every pair of writes leaves the database unchanged.
- 50 concurrent sells of a product with 10 left produce exactly 10 bills and stock 0.
- Two identical POSTs with the same key produce one invoice.
- After induced failures the number series has no gaps.
- `explain()` on every hot query shows IXSCAN with no in-memory SORT.
- k6 at 20 bills/s: p95 `POST /invoices` under 300 ms.
- 1.1.0 bills and corrections still work without the header.

**WP-2 Reports and rollups [COMPAT 1.1.0]** (M)

Owns: `backend/rollups.py` (the implementation), `backend/routers/reports.py`, `migrations/02xx_rollups_backfill.py`, new `tests/test_reports.py`.

Work:
- Rollup collections `daily_sales` (`_id = business_id:date_key`; orders, sales, by method, by user, by hour) and `daily_item_sales`, kept in step by `$inc` through `rollups.apply`. Backfill migration for existing bills.
- `/invoices/summary` reads the rollups. This includes the "all dates" view 1.1.0 sends, which now costs O(days), not O(bills).
- Dashboard and reports use `$match`/`$group`/`$facet` aggregations. "Recent bills" is a `find().limit(20)` with a projection.
- Low-stock counts use `count_documents` with `$expr` on `{business_id, deleted_at, stock}`.
- No `to_list` over 1000 anywhere in the module. Optional: report reads sent to secondaries.

Acceptance:
- Golden tests: totals equal the old Python implementation on fixtures, including cancelled and corrected bills.
- A shop with 500k bills gets its "all" summary in under 100 ms.
- Response shapes are unchanged.

**WP-3 Catalogue: search, library, products [COMPAT 1.1.0]** (M)

Owns: `backend/routers/products.py`, `backend/catalog_search.py`, `backend/search.py`, `backend/routers/library.py`, `migrations/03xx_*`, new `tests/test_search.py`.

Work:
- A precomputed, indexed `search_keys` array (name_key, transliterated and phonetic forms, botanical name, SKU, aliases) on products and library entries, filled on write. Index `{business_id:1, search_keys:1}`. Queries use an anchored prefix with `.limit(30)` and a projection.
- A cached in-memory library index with a version stamp. `entry_for_new_product` matches exactly on keys (removes the scans at `library.py:79` and `:93`). `library.name_key` becomes unique.
- New shop-created library entries are visible only to the shop that made them until an admin approves them. Names are sanitised (no URLs or phone numbers, length cap).
- Name look-ups go through `jobs.enqueue`.
- SKUs come from the counters collection, with a unique index on `{business_id, sku}`.
- `GET /products` accepts optional `limit`, `cursor`, `search`, `low` and `category`. Without them it returns the old array.
- Index `{business_id, deleted_at, name}`.

Acceptance:
- The existing Hindi and Hinglish ranking cases still pass.
- Suggest p95 under 50 ms with 200k library entries and 5k products.
- Documents examined per search are at most a few times the limit.

**WP-4 Customers and suppliers [COMPAT 1.1.0]** (M)

Owns: `backend/routers/customers.py`, `backend/routers/suppliers.py`, `migrations/04xx_*`, `tests/test_customers.py`, and new `tests/test_suppliers_stats.py`.

Work:
- Normalised `name_key` and `phone10` fields, searched with anchored prefixes on `{business_id, name_key}`, `{business_id, phone10}` and `{business_id, last_purchase_at:-1}`.
- `/customers` gets optional cursor pagination; without it, the old response is returned. Creating an existing phone returns the existing customer instead of a duplicate.
- An owner-only "erase customer" action: anonymise the customer and blank phone and address on past B2C bills, written to the audit log.
- Supplier `_stats` becomes an aggregation backed by purchases `{business_id, supplier_id, created_at:-1}`.

Acceptance:
- No unanchored `$regex` remains (enforced by a test that greps for it).
- Customer search examines at most 2× the number of results.
- Erasing a customer leaves invoice totals unchanged.

**WP-5 Worker, scheduler and notifications [COMPAT 1.1.0]** (M)

Owns: `backend/worker.py`, `backend/notify.py`, `backend/push.py`, `backend/routers/notifications.py`, `tests/test_notifications.py`.

Work:
- An arq cron job runs the daily summary in the worker only.
- One query selects the shops that are due. The send is claimed before the summary is computed. Totals come from `rollups.read_day`. Shops are processed in batches through a semaphore, and default send times are spread by a hash of the shop id.
- Push sends become durable arq jobs with backoff, a check of Expo push receipts, removal of `DeviceNotRegistered` tokens, and one shared `httpx.AsyncClient`.
- Removing a push token requires auth, with a narrow exception kept for 1.1.0.
- A heartbeat ping to the uptime monitor.

Acceptance:
- With 3 API replicas and 1 worker, each shop gets exactly one summary.
- A restart in the middle of a send does not lose the push.
- At 21:00, 1000 shops take under 5 minutes of work and add no measurable latency to billing.

**WP-6 AI cost and safety [COMPAT 1.1.0]** (M)

Owns: `backend/ai_client.py`, `backend/routers/ai.py`, `migrations/06xx_*`, new `tests/test_ai.py`.

Work:
- **Client:** `AsyncAnthropic(timeout=httpx.Timeout(60, connect=5), max_retries=2)`, with per-call timeouts of 30 s for plants and 120 s for bills.
- **Concurrency:** a semaphore per process plus a global limit in Redis, and a circuit breaker.
- **Errors:** auth, permission, billing and credit errors become a 503 that gives the scan back and raises a Sentry alert.
- **Usage log:** one `ai_calls` document per call (model from `resp.model`, tokens including cache tokens, cost, request_id, latency).
- **Models:** a model setting per route (`CLAUDE_MODEL_PLANT/INVOICE/NAMES`). The fallback beta header is sent only where the model supports it (not Haiku 5.5).
- **Images:** decoded in a thread, `MAX_IMAGE_PIXELS` about 40 MP with the bomb warning raised as an error, `formats=['JPEG','PNG','WEBP']`, `img.draft()` for JPEGs, and no second re-encode of pages already normalised at upload.
- **Prompts:** fixed instructions move into a cached system prompt; the catalogue is no longer sent (`ai.py:59` loads names only); `stop_reason == "max_tokens"` is handled.
- **Quotas:** stored per period and kind, with a unique index on `{business_id, period, kind}` and plan entitlements (pending decision D4). Per-user rate limits use `ratelimit.py`.
- The `normalize_photo` signature stays the same because `files.py` uses it.

Acceptance:
- On an eval set of 200 labelled plant photos and 50 bills, Haiku at 1024 px reaches at least 95% of Sonnet's accuracy before the switch.
- `cache_read_input_tokens` is above 0 in production logs.
- A 170 MP PNG is rejected without a memory spike.
- An Anthropic outage returns 503 within 5 s once the breaker is open.

**WP-7 Files, storage and CDN [COMPAT 1.1.0]** (M)

Owns: `backend/routers/files.py`, `backend/storage_client.py`, `migrations/07xx_*`, new `tests/test_files.py`.

Work:
- boto3 `Config(max_pool_connections=50, timeouts, retries standard)`.
- A 256 px WebP thumbnail made at upload.
- Public storage (approved product and library photos) is separated from private storage (supplier-bill scans).
- `GET /api/files/...` returns a **302** to `img.<domain>` for public files, or to a short-lived presigned URL after a business check for private files. This keeps 1.1.0 working.
- An optional presigned-PUT upload endpoint. `/upload-base64` is kept.
- An upload rate limit and storage quota per shop. `uploads.path` becomes unique.

Acceptance:
- 1.1.0 thumbnails and photos load through the redirect (checked on a device).
- The API does no R2 reads for public images.
- Private scans are refused across tenants.

**WP-8 Auth and account security [COMPAT 1.1.0, critical]** (M)

Owns: `backend/auth.py`, `backend/routers/auth.py`, `backend/routers/staff.py`, new `backend/routers/account.py`, `migrations/08xx_*`, `tests/test_auth.py`, `tests/test_roles_and_tenancy.py`.

Work:
- bcrypt and Google token checks run in a thread with their own capacity limiter. Google certificates are cached.
- Rate limits per IP and per phone (429 with Retry-After). Exponential backoff replaces the hard lockout. Unknown phones are checked against a dummy hash so timing does not reveal which numbers have accounts.
- New or changed PINs need at least 6 digits.
- An explicit `is_admin` flag on the user, set by a CLI command. `ADMIN_PHONES` is removed (`auth.py:68-70`).
- A TTL cache of users keyed by `token_version`.
- JWT `iss`/`aud` claims are added, but tokens without them are accepted for 30 days so that 1.1.0 logins keep working.
- Unique partial indexes on users `phone` and `google_sub`.
- OTP endpoints (provider per decision D8) behind a flag, **enforced only after 1.1.0 is retired**.
- `DELETE /api/account`: confirm, grace period, then erase. Records required by GST law go to a restricted archive.
- Audit log entries for logins, locks and staff changes.

Acceptance:
- 100 logins/s do not raise p95 on other routes by more than 20 ms.
- A brute-force script is throttled.
- 1.1.0 login, Google sign-in and staff flows pass.

**WP-9 GST and the tax invoice [COMPAT 1.1.0: response changes are additive only]** (L)

Owns: new `backend/gst.py`, `backend/invoice_html.py`, `backend/routers/business.py`, `migrations/09xx_*`, new `tests/test_gst.py`.

Work:
- A tax profile per shop: unregistered, regular or composition, with the state taken from the GSTIN.
- GSTIN check with the mod-36 checksum. A valid GSTIN is required when the shop is marked GST-registered.
- HSN/SAC codes and an exempt flag on categories, overridable per product, with suggested defaults (for example 0602 for live plants).
- `gst.split()` returns CGST+SGST or IGST per line and per rate, using `Decimal` with ROUND_HALF_UP.
- The bill shows the correct title (Tax Invoice / Bill of Supply / Invoice-cum-Bill of Supply), HSN, taxable value, rate and tax per line, reverse-charge line and signatory.
- The public page masks the phone and hides the address (unless a B2B bill or one of ₹50,000 or more needs them).
- `list_categories` becomes one aggregation (`business.py:183-187`). `logo_path` is checked against the shop's own uploads.
- A CA signs off the template.

Acceptance:
- Golden tests for intra-state, inter-state, exempt plus taxable mixed bills, and composition shops.
- Rounding reconciles to the paisa across lines, rate totals and HSN totals.

**WP-10 Compliance data: export, audit, retention** (M)

Owns: new `backend/routers/exports.py`, the `backend/audit.py` implementation (WP-0 created the interface), `backend/demo_data.py`, `deploy/demo-refresh.sh`, `migrations/10xx_retention.py`, new `tests/test_exports.py`.

Work:
- GSTR-1 export (b2b, b2cs, b2cl, cdnr, cdnur, hsn, doc_issue) as JSON and CSV, plus a 3B summary.
- A full-shop zip export built as a background job, delivered through a presigned R2 URL.
- An append-only `audit_log` with an insert-and-find-only database role and a daily export to object-locked storage.
- TTL and retention migrations.
- Demo-shop refresh refused unless `business.demo` is true, and blocked in production.

Acceptance:
- The export validates against the GSTN offline-tool schema.
- The audit log cannot be updated by the app's database user.

**WP-11 CI/CD, deploy, backups, runbooks** (M)

Owns:
- `.github/workflows/*` (deploy.yml plus new ci.yml), `.github/dependabot.yml`
- `deploy/deploy.sh`, `deploy/ci-deploy`, `deploy/backup.sh`, `deploy/README.md`, `deploy/.env.example`
- new `deploy/Caddyfile.snippet`, new `loadtest/` (k6), new `docs/RUNBOOK.md`

Work:
- **PR checks:** ruff, pytest on a real replica set, pip-audit, Trivy, gitleaks or secret scanning; for the frontend `tsc --noEmit`, `expo lint`, `check-i18n`, `expo-doctor`. Actions pinned by commit SHA.
- **Build and deploy:** an arm64 image built and pushed to GHCR tagged by SHA. Deploy runs migrations, then a blue/green or rolling update with `/readyz`, and rolls back automatically. A `production` GitHub Environment with required reviewers, and one concurrency group per VM.
- **Caddy:** `encode zstd gzip`, `request_body` limits (12 MB on upload and AI routes, 256 KB elsewhere), security headers, active health checks, and log redaction of `/i/*` and query strings.
- **Backups until Atlas is live:** encrypted with age, copied off the VM with rclone, monitored by a heartbeat, plus a restore-drill script.
- **Mobile releases:** EAS Update published only from CI.
- **Load test:** a k6 bill scenario at 1×, 3× and 5× of 14 bills/s.
- **Docs:** the out-of-date X-App-Key text is removed from the README and `.env.example`.

Acceptance:
- A failing test or audit blocks the merge.
- A deliberately broken image rolls back on its own with no 5xx seen at Cloudflare.
- A restore drill is documented with its timing.

**WP-12 App reliability on the billing path** (M; ships to 1.2.x and later, not to 1.1.0)

Owns: `frontend/src/api.ts`, `src/cart-context.tsx`, `src/order.ts`, `src/query-client.ts`, `src/hooks/useRefreshOnFocus.ts`, `app/order/*.tsx`, `app/bill/[id].tsx`.

Work:
- **Network:** timeouts through `AbortSignal.timeout` (10 s normally, 45 s for `/ai`), with a typed timeout error.
- **Idempotency:** one `expo-crypto` UUID per bill attempt, kept across retries and the "bill anyway" resend.
- **Cart:** saved on the phone (AsyncStorage) and restored on start; failed saves retried with backoff; a "Not saved" indicator; flushed when the app goes to the background; `load()` tells "not found" apart from a network error (`cart-context.tsx:131-133`); debounce raised to 1.2–1.5 s.
- **Queries:** targeted invalidation plus `setQueryData` from the POST response; `onlineManager` (netinfo) and `focusManager` wired up, with an offline banner.
- **Search:** `scan.tsx` debounced at 250 ms with `keepPreviousData`, and the query signal passed through so stale searches are cancelled; no customer search with an empty query.

Acceptance:
- A Maestro flow with a network drop during Pay produces exactly one bill.
- Measured calls per bill are 16 or fewer.
- Killing the app in the background does not lose the cart.

**WP-13 App platform and store compliance** (M)

Owns: `frontend/app.json`, `frontend/eas.json`, `frontend/metro.config.js`, `app/_layout.tsx`, `src/components/error-boundary.tsx`, `app/(tabs)/more.tsx`, new `app/account/*.tsx`.

Work:
- **Sentry:** Expo plugin, source maps uploaded from EAS, PII scrubbing, route-level ErrorBoundary.
- **Domain:** `EXPO_PUBLIC_BACKEND_URL` set to `https://api.<domain>` through EAS environment variables, failing fast if missing.
- **Updates and versions:** `runtimeVersion: {policy: "fingerprint"}`; `appVersionSource: remote` with `autoIncrement`.
- **Android:** the READ_MEDIA permissions blocked; target and compile SDK set through expo-build-properties.
- **iOS:** `LSApplicationQueriesSchemes`, privacy manifest, `supportsTablet: false`.
- **Store requirements:** delete-account screen, privacy and terms links, min-version prompt from `/api/config`.
- LogBox: silence only named warnings instead of all of them.

Acceptance:
- The merged AndroidManifest has no READ_MEDIA permissions.
- A staged-rollout OTA reaches only binaries with a matching fingerprint.
- The delete flow works end to end against staging.

**WP-14 App lists and images** (S–M)

Owns: `app/(tabs)/products.tsx`, `app/(tabs)/customers.tsx`, library screens, `src/components/ui.tsx` (Thumb), `src/image-tools.ts`.

Work:
- FlashList with `useInfiniteQuery` against the WP-3 and WP-4 cursor APIs; server-side search and counts.
- Thumbnail URLs for list rows; `cachePolicy="memory-disk"` and `recyclingKey`.
- `image-tools.ts` takes the long edge as a parameter: 1024 px for plant photos, 1568 px for bills. (The call change in `scan.tsx` belongs to WP-12.)

Acceptance:
- A shop with 20k customers scrolls smoothly on a low-end Android phone.
- Every customer is reachable, with no silent cap at 5000.

### Wave 2: sequential integration after Wave 1 merges

- **Use `gst.split` in the bill (owner of WP-1).** Call it from `sales.py` `price_lines`. Remove `gst_removed` per decision D6.
- **Walk-in bills (owner of WP-1).** Optional walk-in customer with no phone, per decision D10.
- **Credit notes instead of 7-day correction (owner of WP-1).** Credit-note series, partial returns, CDNR/CDNUR reporting. Cancel or correct allowed only while the bill's tax period is open.
- **Register export jobs (owner of WP-5).** Register the WP-10 export jobs in `worker.py`.
- **After 1.1.0 is retired:** enforce OTP; refresh tokens with rotation; stop accepting JWTs without `aud`.
- **P2 migrations (dedicated, sequential, because they touch many files):**
  - Money as integer paise or Decimal128.
  - Timestamps as BSON Date, plus keyset pagination on `(created_at, id)`.
  - stock_history as a time-series collection with retention.
  - Atlas Search in place of the prefix search (phase 2 or 3).
  - Online Archive for closed financial years.

---

## 4. Owner decisions

| # | Decision | Recommendation |
|---|---|---|
| D1 | **Domain and Cloudflare account** | Buy a domain (for example harahisaab.in) and use Cloudflare Free with `api.`, `img.` and `bill.` hostnames. Do it **before the next production build**, so no IP address is ever baked into a released binary (`frontend/eas.json:17,29,40,50`). |
| D2 | **Database: MongoDB Atlas or self-hosted** | Atlas in Mumbai (AWS ap-south-1). Start on M10 (about $60–90 a month) with Continuous Cloud Backup, then move to M30 at about 100–150 shops. Self-hosting needs 3 VMs plus Percona Backup for MongoDB and on-call expertise. |
| D3 | **Paid compute vendor** | Oracle pay-as-you-go Ampere A1 in Mumbai or Hyderabad (about $55 a month for 4 OCPU / 24 GB) for a **dedicated** production VM. Keep staging on the shared Always Free VM. Move to AWS (Graviton next to Atlas) only if cross-cloud latency or egress shows up in measurements. |
| D4 | **AI budget and plan allowances** | Separate Anthropic workspaces for production and staging, each with a hard spend limit (pilot about $300 a month; full scale about $3k a month), usage alerts and auto-reload. Allowances per plan in TIERS-PLAN: trial about 20 scans a day, paid tiers monthly. Ask Anthropic for a higher usage tier before onboarding beyond 200 shops. |
| D5 | **Haiku for plant photos** | Approve once the eval set shows at least 95% of Sonnet's accuracy. This saves about $5k a month at full scale. |
| D6 | **"Bill without GST" feature** | **Remove it.** It collects the full price while showing no tax (`sales.py:163,238`), which creates penalty risk for the shop under CGST s.122/132 and reputational risk for you. Exempt goods (live plants, HSN 0602) are handled with a 0% or exempt tax class instead. Ask a CA to review the existing `gst_removed` bills. |
| D7 | **CA and legal review** | Engage a practising CA to sign off the tax-invoice template, HSN defaults, the bill-number format (per financial year, at most 16 characters, for example `2627/INV/000123`) and the credit-note policy. Engage an Indian technology lawyer for the privacy notice (English and Hindi), terms of service with a data processing agreement (the shop is Data Fiduciary, you are Data Processor), the grievance officer and the sub-processor list (Anthropic, Cloudflare, Expo/FCM/APNs, Google, Oracle, MongoDB). |
| D8 | **Phone OTP provider** | MSG91 OTP: native to India's DLT rules, with sender ID and templates registered in advance (registration takes days to weeks). Alternatives are Twilio Verify or Firebase Authentication phone sign-in. Check current SMS prices before choosing. |
| D9 | **Data retention schedule** | Invoices, credit notes, purchases, stock records and the audit log for 8 years, archived after the annual return (GSTR-9) is filed. Customer contact details until erased, or anonymised after 3 years without a purchase. Logs 180 days (CERT-In). Carts 7 days. Orphaned uploads 30 days. stock_history kept hot for 24 months, then archived. |
| D10 | **Customer phone optional** | Yes: allow a "Walk-in" bill with no phone for B2C sales under ₹50,000, and ask for the phone only for WhatsApp delivery or credit, with a one-line notice. |
| D11 | **Offline billing** | Defer it. Ship idempotent retries plus the offline banner now (WP-12). Full offline billing (bills queued on the phone, numbers per device) is a large product change. |
| D12 | **Employee price and discount rights** | Add a "change price or discount" permission, off by default for employees, with a maximum discount %. Price overrides show in the employees report and the audit log. |
| D13 | **Service targets and on-call** | Aim for 99.9% monthly uptime and p95 bill save under 500 ms. Name who gets paged (SMS through Better Stack or UptimeRobot) and publish a status page. Decide the support hours shops will be told. |
| D14 | **Account security** | Turn on 2FA for GitHub, Expo, Cloudflare, Anthropic, Oracle, Atlas, Google Play and Apple. Protect `main` and `staging` branches. You are the required reviewer for production deploys. |
| D15 | **Production data wipe and migration to multi-shop** | Schedule it before the first paying shop (QA-CHECKLIST R.2). Make sure admin access no longer depends on a phone number (WP-8) before the wipe. |
| D16 | **Pricing compared with cost** | At full scale the optimised all-in cost is about $2.3k–4.2k a month, about ₹2–4 per shop per day. Set tier prices and AI allowances so the Tree tier covers its AI usage. As-is, AI alone (~$7.7k) would exceed the revenue projected in TIERS-PLAN (~₹4.2 lakh, about $4.7k). |
| D17 | **iOS timing** | Apple Developer Program ($99 a year), after Android is stable. The WP-13 iOS settings are needed before the first TestFlight build. |

---

**Key files:**
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/backend/routers/sales.py`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/backend/server.py`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/deploy/compose.yaml`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/frontend/src/cart-context.tsx`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/frontend/src/order.ts`