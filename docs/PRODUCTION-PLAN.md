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
| D1 | **Domain and Cloudflare account** | Buy a domain (for example harahisaab.in) and use Cloudflare Free with `api.`, `img.` and `bill.` hostnames. Do it **before the next production build**, so no IP address is ever baked into a released binary (`frontend/eas.json:17,29,40,50`). **Owner, 8 Nov: keep the sslip.io addresses for now** (users never see the address); a production build then needs `ALLOW_IP_BACKEND_URL=1` in the EAS production environment. |
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
| D15 | **Production data wipe and migration to multi-shop** | Schedule it before the first paying shop (QA-CHECKLIST R.2). Make sure admin access no longer depends on a phone number (WP-8) before the wipe. **Owner, 8 Nov: approved, "no issues"**; the owner runs the wipe at the production release (Claude never deletes data). |
| D16 | **Pricing compared with cost** | At full scale the optimised all-in cost is about $2.3k–4.2k a month, about ₹2–4 per shop per day. Set tier prices and AI allowances so the Tree tier covers its AI usage. As-is, AI alone (~$7.7k) would exceed the revenue projected in TIERS-PLAN (~₹4.2 lakh, about $4.7k). |
| D17 | **iOS timing** | Apple Developer Program ($99 a year), after Android is stable. The WP-13 iOS settings are needed before the first TestFlight build. |

---

**Key files:**
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/backend/routers/sales.py`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/backend/server.py`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/deploy/compose.yaml`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/frontend/src/cart-context.tsx`
- `C:/Users/ShashankJindal/OneDrive - Cubastion Consulting Pvt Ltd/Personal Use/Lets Go/Planteck/Nzone/frontend/src/order.ts`

---

## Status (3 Nov 2026)

All fourteen Wave 1 packages are merged into `prod-ready` (in order WP-1 to WP-14, each with `--no-ff`), with integration commits between them for the seams they could not change themselves. Nothing is deployed. The backend suite passes on a local MongoDB replica set (387 tests after the review fixes: 385 pass, 2 skipped because they need `RUN_BENCH=1` or a real Redis). The app checks pass: `tsc`, eslint with 0 errors, `i18n:check` with 0 missing and 0 unused, and the two Node unit-test files. Running `python -m migrations up` twice on a fresh database changes nothing. Running every migration again on a seeded shop also changes nothing.

Done = built and its acceptance checks pass here. Partly = built, but some acceptance checks need staging, a phone, a real Redis, or an owner's input.

| WP | Status | What remains |
|---|---|---|
| WP-0 Platform foundation | Done (merged before Wave 1) | Run the 1.1.0 regression on staging. Check that a test error reaches Sentry with the release SHA once a DSN is set. |
| WP-1 Sale integrity | Partly | k6 at 20 bills/s was never run. Locally, 20 bills at once across 10 shops reached p95 817 ms; run k6 on staging-sized hardware. The Orders bill search (`_filters` in `sales.py`) still uses an unanchored `$regex` within one shop's bills, so `test_no_unanchored_regex` still exempts `routers/sales.py`. Wave 2 items are still open: D6, walk-in bills (D10), and credit notes (`gst.split` in `price_lines` is done, see the review fixes below). |
| WP-2 Reports and rollups | Done | `REPORTS_FROM_ROLLUPS` is off by default in the code. `deploy/.env.example` now sets it to 1, because `sales.py` applies the rollups inside each bill's transaction. Turn it on only after migration 0202 has run and the first deploy's rollups rebuild has worked (`deploy.sh` step 6; RUNBOOK "Daily rollups"). |
| WP-3 Catalogue | Done | The suggest benchmark was measured locally only (p95 35 ms). |
| WP-4 Customers and suppliers | Done | Searching by the middle of a name or number no longer matches; this is intended. The `sales.py` regex exemption is listed under WP-1. |
| WP-5 Worker and notifications | Partly | Never run against a real Redis or arq worker. Prove on staging that the summary goes out exactly once, and that pushes and retries survive a restart. |
| WP-6 AI cost and safety | Partly | The Haiku accuracy evaluation needs an eval set (200 plant photos, 50 bills) and decision D5. Prompt caching cannot take effect yet, because the shared system prompt (~350 tokens) is below the 512-token minimum. Set `AI_MONTHLY_BUDGET_USD` per decision D4. |
| WP-7 Files, storage and CDN | Partly | No device check that 1.1.0 photos load through the redirect. Private scans are refused across shops only when the request carries a login. A public bucket and `PUBLIC_IMAGE_BASE_URL` need D1 (domain) and R2 set-up. |
| WP-8 Auth and account security | Partly | The 100 logins/s figure is in-process only; it needs k6 with 4 workers. OTP is not enforced until D8 is decided and 1.1.0 is retired. Google confirmation on Delete account works once Google sign-in is set up in the app (`src/google.ts`). |
| WP-9 GST and the tax invoice | Partly | The invoice template, HSN defaults and bill-number format need a CA's sign-off (D7). Bills now store HSN and the exempt flag per line (integration). Bills now save the `gst.split` amounts, and composition shops' bills carry no tax (review fixes). The "Bill without GST" decision (D6) and buyer GSTIN / place of supply on bills (hook step 4) are Wave 2. |
| WP-10 Compliance data | Partly | GSTR-1 is checked only against a subset of an unofficial schema; GSTN publishes none. Have a CA test an upload in the offline tool. Retention only counts customers due for anonymising; it does not anonymise them yet (D9). |
| WP-11 CI/CD, deploy, backups | Partly | The CI workflow, Trivy, gitleaks, k6, Caddy, age, rclone and Docker were never run here. Run the restore drill once and record its timing in `docs/RUNBOOK.md`. Make expo-doctor blocking once the Expo patches are current. A deploy with no gap needs a second API instance. |
| WP-12 App reliability | Partly | Code done. Still to check on a phone: 16 calls or fewer per bill, and that a cart survives the app being killed. |
| WP-13 App platform | Partly | A Gradle build must confirm the merged manifest has no READ_MEDIA permissions. Staged OTA by fingerprint can only be checked on EAS. The delete flow was tested against a local mock only; test it against staging. A production build is blocked until the domain (D1) exists, unless `ALLOW_IP_BACKEND_URL=1` is set. |
| WP-14 App lists and images | Partly | Smooth scrolling with 20k customers on a low-end Android phone is not yet checked; it was checked in the browser only. Plant and product photos now go up at 1024 px (integration). Plant identification accuracy at 1024 px with Sonnet is not yet measured; to revert, pass no `longEdge` in the four callers. |

Integration fixes made while merging, beyond the merges themselves:
- Supplier bills and demo data create products through `products.insert_product`. Bill customers carry WP-4's search fields.
- Job modules are registered in `worker.py`, and the daily crons are scheduled there: audit archive, retention, expired exports, and shop erasure (WP-8's `erase_due`). The exports router is mounted.
- Shop erase also removes the shop's public photos and its summary row.
- Tests and the dev server use 6-digit PINs, the admin flag and a GSTIN that passes the checksum. The flaky login-throttle test and the worker-cron test are fixed.
- `deploy/.env.example` lists every new server setting, and `frontend/.env.example` lists the app's Sentry and legal-link variables. ruff's per-file exceptions are removed.
- App:
  - 6-digit PIN screens.
  - GSTIN examples that pass the checksum.
  - Plant and product photos at 1024 px.
  - The Google `id_token` on Delete account.
  - `check-i18n.js` now also reads the GST warnings.
  - CI runs the app's unit tests.

Review fixes (3 Nov 2026), each with regression tests:
- Rollups (WP-2, WP-11): the old version keeps billing while migration 0202 runs, and a rollback runs a version without rollups. `deploy.sh` notes the first such day. After the next good swap it runs `python rollups.py rebuild --touched-since <day>` (that day minus `CORRECT_DAYS`, every shop). A failed rebuild keeps the new version, exits 1 and is retried by the next deploy. RUNBOOK has a "Daily rollups" section.
- Rollback (WP-1, WP-11): carts keep `updated_at` as the ISO string the previous version parses. The 7-day TTL is now on a new `saved_at` date (migrations 0104 and 1001 changed; nothing was deployed). A rollback to an image without `worker.py` starts `web` alone.
- Idempotency (WP-1, WP-12): the key is the first write of the bill's transaction. Duplicates sent at once now get the bill instead of `out_of_stock` or `bill_cancelled`. The key stores a fingerprint of the order. The same key with a changed order gets 409 `idempotency_mismatch`, naming the bill that was made, and the app opens that bill.
- GST (WP-1, WP-9, WP-10): `price_lines` saves `gst.split` per line (`net`, `tax`, `cgst`/`sgst`/`igst`) and `inter_state` on the bill. Print and GSTR-1 read the saved amounts (`gst.saved_split`), so print, app, exports and returns agree. A bill of Rs 100 at 18% now carries 15.26 (7.63 + 7.63) instead of 15.25. Composition shops' bills are saved at 0%, and the GSTR exports refuse them (`gst_composition`).
- Push (WP-5): any valid login removes the push token it sends, as the 1.1.0/1.2.0 apps expect. Removing another shop's phone is audit-logged and limited to 10 an hour per login.
- Login (WP-8): `auth.reserve_login` claims the phone atomically before bcrypt. Parallel guesses get one check; the rest get 429 `login_busy`. Older 4-digit PINs are not force-reset yet; that needs an owner decision.
- Admins (WP-8): `ADMIN_PHONES` only flags logins that exist when migration 0801 runs. `.env.example`, RUNBOOK ("App admins") and QA R.1 now say to run `manage_users admin grant` after the admin signs up on a new database.
- AI (WP-6, WP-12): each Claude call has a total limit, `ai_client.TOTAL_TIMEOUT` (plant 50 s, bill 150 s). When it is hit, the call ends as `ai_timeout` and the scan is given back. The app waits 75 s for `/ai/` and 180 s for `/ai/scan-invoice`.

Load-test fixes (3 Nov 2026), after a local locust run with 1000 seeded shops (`loadtest/README.md`; a laptop run, not a capacity number). Each item has regression tests:
- Overload (`backend/overload.py`): each API worker works on at most `MAX_IN_FLIGHT` requests (default twice `MONGO_MAX_POOL_SIZE`). One more gets 503 `busy` with `Retry-After` at once. Health, readiness and metrics are never refused. MongoDB timeouts and lost connections get 503 `db_busy` and one warning line instead of a 500 with a traceback. `MONGO_TIMEOUT_MS` is now 5 s (it includes the wait for a pooled connection), and a whole transaction, retries included, gets 10 s instead of PyMongo's 120 s. The app sends a 503 again after `Retry-After`, at most 3 tries in all, only when that is safe; a bill keeps its Idempotency-Key. A bill that got `db_busy` is treated like a bill with no answer. Settings are in `deploy/.env.example`, and RUNBOOK section 7 has a new "Overload protection" part.
- Migrations: 0401 walks the customers once (`migrations.walk`, in _id order); before, it read them again for every batch and took ~48 min on 2 million customers. The migration lock is now renewed every minute while a migration runs. The other migrations and `rollups.rebuild` were checked and already read each document once.
- Search: the check for products without search keys (and for customers without search fields) runs at most once per shop every 5 minutes per process, not on every keystroke; every writer sets them. Name forms are cached and the query's forms are worked out once per search: ranking 200 candidates went from 16 ms to 2 ms, with identical results. Library entries are read once per search.
- Responses: the busiest routes (`GET /api/products`, `/customers/find`, `GET`/`PUT /orders/cart`, `GET /invoices/{id}`, `/dashboard/today`) skip FastAPI's jsonable_encoder walk and still send the same bytes (`backend/fast_json.py`). A 30-item search answer went from 0.95 ms to 0.09 ms. `POST /api/invoices` is unchanged: one call per bill, ~0.1 ms of encoding.
- Logs: no traceback shows local variables. The dev console prints plain tracebacks. structlog's `dict_tracebacks` also showed them in the JSON lines (structlog 26.1 defaults to `show_locals=True`), so staging and production logs carried PINs and tokens from frame locals. Those lines are now the same, without `locals`.
- Review of these fixes (3 Nov 2026), each with regression tests:
  - Order saves (`cart-sync.ts`): the Retry-After line had taken over the `else` that drops a refused save, so a save that got no answer, a timeout or a 5xx without Retry-After was deleted from the outbox and never sent again. A failure that can be retried is kept again, and Retry-After only sets the wait. New Node test `tests/cart-sync.test.mjs` runs the real outbox and API client with a fake server (CI now runs the Node tests with `--experimental-transform-types`).
  - Transactions (`db.py`): the 10 s limit was put around `with_transaction`, so a try that ran out of time had no time left for its abortTransaction, which PyMongo skips without a word; MongoDB then kept the transaction and the shop's invoice counter for 60 s, and every bill of that shop met a write conflict. The limit is now on each try of the callback (what is left of the 10 s); the abort and the commit run under the usual per-operation limit.
  - Readiness (`/readyz`): a full connection pool made the ping time out, so it answered 503 and Caddy took the only upstream out (requests held for 30 s instead of fast 503s). A pool-wait timeout now answers 200 with `"mongo": "busy"`; no primary or no answer is still `"down"`.
  - Rollups rebuild: the CLI and migration 0202 put one deadline (1 hour) on the whole run, which a year of 1000 shops (about 365,000 shop-days at ~20 ms) passes. `rebuild` now limits each step only, runs 8 shops side by side, logs progress, and names the failed shops with the command that repeats them. A migration can set `TIMEOUT = None` when it limits its own steps (0202 does).
  - Smaller ones: migration 0401 makes a last pass, shop by shop on the name_key index, for customers saved behind the walk; a bill whose earlier try got `db_busy` and whose last try got `busy` is shown as "may or may not have been made" (`ApiError.maybeDone`); the AI routes are not counted by the in-flight limit (ai_guard limits them); the name-forms cache is 25,000 names (about 9 MB per worker, not 35 MB).
  - Not changed: a GET that gets `db_busy` because the query itself is slow (not overload) is still tried 3 times. Telling the two apart needs the server to answer a pool wait and an operation over its time limit differently; that is a separate change to the retry rules.
- Laptop re-run after the fixes (3 Nov 2026, same laptop and data, 4 API workers, `LOG_FORMAT=json`, locust on the same 8 logical cores):
  - 1x (the 1000-shop peak, ~14 bills/s, ~230 requests/s): 0 errors; `POST /api/invoices` p50 120 ms, p95 520 ms, p99 1.1 s (before: p95 840 ms); every other route p95 under 250 ms. The API used about 2 cores, MongoDB about 1.
  - 3x (well past what this laptop can serve): 44% of requests got the fast 503 `busy` from the in-flight limit (about 60 were `db_busy`), which the app retries after `Retry-After` and locust counts as failures. Bills that went through: p95 3.7 s (before: 34 s), about 11% more of them than before. The API went idle as soon as the load stopped and answered `/readyz` in ~20 ms; no death spiral.
  - The in-flight limit refuses bills and searches alike. Letting `POST /api/invoices` past the limit by a small margin would keep billing going while searches are shed; not done.
- Still to do: run the load test again on staging-sized hardware with k6 from a separate machine (WP-1), with the new 503 rate per route watched in `/metrics`.
- Checks after these fixes and their review: the backend suite on the local replica set has 440 tests (438 pass; 2 skipped, because they need `RUN_BENCH=1` or a real Redis), including `tests/test_ops.py` under Git Bash, and ruff is clean. The app checks pass: `tsc`, eslint with 0 errors, `i18n:check` (846 texts, 0 missing) and the Node unit tests (18).

## Status update (8 Nov 2026)

A code review of everything production would get (`main...prod-ready`) found 11 issues; 10 are fixed on
`prod-ready` (776f980) with regression tests, one is left by the owner's D1 answer. Details in
`docs/SESSION-NOTES.md`, "Code review fixes". What changes in the table above:
- WP-1 Sale integrity: supplier bills (POST /api/purchases) now take an Idempotency-Key too (`backend/idempotency.py`,
  shared with bills), so a retried supplier bill no longer adds its stock twice; its catalogue look-ups run in
  parallel.
- WP-5 Worker and notifications: queueing a job fails fast while Redis is down (one 1 s connection try, 3 s per
  enqueue, then 30 s of immediate failure), so bills that cross a low-stock line are not held up by it.
- WP-10 Compliance data: CSV exports escape typed text that a spreadsheet would run as a formula.
- WP-11 CI/CD: GHCR images are also tagged by the tree hash of `backend/`, and deploy.sh pulls by it, so a
  docs-only commit no longer breaks a production deploy; the swap starts web and worker only.
- Owner decisions: D1 (sslip.io for now) and D15 (production wipe approved) are answered; the rest stay open.
- QA deploy (8 Nov, the owner asked for it): `prod-ready` pushed into `staging`. The first run stopped before
  changing anything (compose.yaml: `pids_limit` next to `deploy.resources.limits`, refused by Docker Compose). A
  rehearsal with real Docker (old QA version and data, then the update as the VM runs it) then found that the
  rollups rebuild command always failed (argparse default). Both fixed with tests and pushed for the second run.
  Two more stops followed, each before any harm (the running image no longer listed; a skipped migrations step
  and web `cpus: "2"` on the 1-CPU VM); each fixed with tests. The fourth run went live at 05:10 UTC: QA runs
  `prod-ready` (6040de2), 22 migrations applied, rollups rebuilt, /readyz 200.
- WP-11 note: the fake-docker deploy tests cannot catch compose-file or command-line problems; rehearse a deploy
  with real Docker before a release (SESSION-NOTES, "Deploy rehearsal").
- CI on `prod-ready` also fails the Trivy image scan (HIGH/CRITICAL Debian packages in the base image, mostly Perl,
  with fixes available). It does not block deploys (they run the backend tests only); fix proposed to the owner.
- Checks: backend 450 passed, 2 skipped (12 new), ruff clean; app tsc, eslint 0 errors, i18n 850 texts, Node 18/18.
- Still open from 3 Nov: bills past the in-flight limit by a small margin, a pool wait answered apart from a slow
  query, and the k6 capacity test on staging-sized hardware.

## Status update (8 Nov 2026, afternoon)
- Plant detection made consistent (owner's ask): Claude now chooses from the shop's own product list sent with the
  photo, its choice is used only when it is really one of the products, Claude's names also find products by their
  Hindi, Hinglish and catalogue names (whole names only), and the same photo gets the same answer for 24 hours
  without using a scan (`ai_photo_answers`, migration 0602). The app shows the other close products in stock, and
  Add product asks before making a second copy of one. Details: SESSION-NOTES, "Plant detection".
- WP-11 CI/CD: an "App build" button in GitHub Actions (`.github/workflows/eas-build.yml`) runs the full CI, then
  the EAS build of the chosen profile and puts the download link in the run summary. Needs the owner's EXPO_TOKEN
  in the GitHub Environment; still only after the owner's go for that build.
- Web version: a reloaded screen waited for nothing and showed "Please log in"; requests now wait for the saved
  login. Phones were not affected.
- Full local browser test passed (owner, employee, Hindi, scan flows): SESSION-NOTES, "Browser test before the QA
  build".
- Checks: backend 456 passed, 2 skipped, ruff clean; app tsc, eslint 0 errors, i18n 854 texts, Node 19/19.
- Owner steps before notifications work on the QA app: google-services.json (received, all three apps) into EAS,
  FCM V1 key in Expo credentials, PUSH_ENABLED=1 on staging.
