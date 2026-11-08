# Hara Hisaab: where we are (session notes, updated 8 Nov 2026)

Hand-over notes so the next session can pick up without the old chat. No secrets here: keys and
passwords live only in the VM's `deploy/.env` and in EAS, and the owner enters them. The standing rules and
branch map are also in `CLAUDE.md` at the repo root.

## Start here (8 Nov 2026)
- **Branch:** work on `prod-ready`. On 8 Nov the code review's fixes went in (776f980, see "Code review fixes"
  below), the owner connected GitHub, and `prod-ready` was pushed and fast-forwarded into `staging` (the owner
  asked for the QA update: "get the app back to QA updated with all fixes").
- **QA deploy, 8 Nov:** the first run (c45e477) stopped at its first compose call, before anything changed: Docker
  Compose refuses `pids_limit` next to `deploy.resources.limits` (fixed in 07c84aa: limits in
  `deploy.resources.limits.pids`). A rehearsal with real Docker in the cloud session then deployed the old QA
  commit (0f47049) with data, and updated it the way the VM does (old deploy.sh syncs and runs the new one); it
  found that `python rollups.py rebuild --touched-since <day>` always failed (argparse ran the empty `--since`
  default through the date check; fixed in d655670). The rehearsal then passed: old 1.1.0 login, old bill and
  stock intact, new bills continue the number series, retry key replays, Home totals right, MongoDB a replica set.
  Those two fixes were pushed to `prod-ready` and `staging` for the second QA deploy.
- **Second QA deploy (3187206):** stopped at its first step, before any change: the running web container's image
  was gone from Docker's image list ("No such image"). Reproduced with real Docker: with the containerd image store
  a prune removes an untagged image while its container keeps running, and `docker commit` then fails too.
  deploy.sh now keeps a rollback image when it can (tag, else commit); when it cannot, staging deploys without an
  automatic rollback and says so in the log ("WARNING: no rollback image"), production stops before any change
  (7159a08, 7613128; tests in test_ops.py). The third QA deploy carries these.
- **Third QA deploy (a09eb5c):** built the image on the VM, then skipped the settings/migrations step (`compose
  config | grep -q` under pipefail: SIGPIPE) and failed the swap (web `cpus: "2"` on the 1-CPU VM); it switched back
  to the old version by itself. Fixed: the service list is read whole; web CPUs are `WEB_CPUS` (default 1). The
  shared VM has **1 CPU**: no compose limit may exceed it. The fourth QA deploy carries these.
- **QA is live on `prod-ready` since 8 Nov 05:10 UTC (6040de2):** fourth deploy green. Settings checked, 22
  migrations applied, web and worker started, ready 3 times in a row, rollups rebuilt (14 shop-days, 7 shops),
  public /readyz 200 (mongo ok, storage ok). From outside: 65 routes, POST /api/purchases takes Idempotency-Key,
  /api/config min_app_version 1.1.0 (the QA phones keep working). The demo refresh cron works again (it reads
  compose.yaml, which is valid now).
- **Trivy (8 Nov):** CI's image scan failed since 4 Nov on Debian packages of the base image with fixes out (perl,
  openssl, apt, pcre2; every Python package was clean). The Dockerfile now installs Debian's security updates
  after FROM (checked locally: all six packages at the fixed versions). On `prod-ready` only; QA gets it with the
  next `staging` push.
- **Deploy rehearsal, for next time:** a bare clone of the repo as `origin`, its `staging` branch plus a
  smoke-only commit that sets `PIP_CERT` to the sandbox proxy's CA (the image build cannot reach PyPI otherwise;
  the VM needs nothing), a clone of it as `$HOME/Plant_Tech-staging` with a `deploy/.env` (PROJECT_NAME, a free
  HOST_PORT, JWT_SECRET, DB_NAME for the old version), then `HOME=<that home> bash .../deploy/deploy.sh staging`.
  The fake-docker tests (`tests/test_ops.py`) do not catch compose-file or CLI problems; this does.
- **Deployed:** QA runs `staging` as last pushed (before the testers' round); production runs the old `main`
  (19ca6f3). Checked for the QA deploy: the VM's old deploy.sh syncs and runs the new one; MongoDB keeps its
  volume and becomes a one-member replica set; the demo shop keeps its daily refresh (migration 1002); the
  settings check needs `JWT_SECRET` of 32+ characters in the VM's `deploy/.env` (else the deploy stops with
  nothing swapped).
- **Built:** the QA app is 1.1.0 (versionCode 2). The code is 1.2.0 (versionCode 3, new native modules), so the
  next QA step is a 1.2.0 build, not an OTA, and only after the owner's browser go.
- **Owner answers (8 Nov):**
  1. Fix the review findings and update QA: yes (done, waiting for the push).
  2. D1 domain: keep the sslip.io addresses for now (users never see the address). A production build then needs
     `ALLOW_IP_BACKEND_URL=1` in the EAS "production" environment (app.config.js refuses IP-like addresses
     without it). When a domain comes: `APP_BACKEND_URL` per GitHub Environment (eas-update.yml) and
     `EXPO_PUBLIC_BACKEND_URL` in EAS.
  3. D15 production data wipe: approved, "no issues". The owner runs it at the production release
     (QA-CHECKLIST R.2); Claude never deletes data.
  4. A high-level design (HLD) explainer was made for the owner (an artifact, not in the repo).
- **Still open:** bill correction owner-only (as built)?; Google Play developer account (personal or company; a
  personal one made after Nov 2023 needs a closed test with 12+ testers for 14 days before production, check
  Google's current rule); the other decisions D2-D14, D16, D17 in `docs/PRODUCTION-PLAN.md`.
- **Next:** the owner's browser test on QA (the owner's own browser app, localhost:8083,
  pointed at staging; Claude never types logins there), then the owner's go for the 1.2.0 QA build. Then the two
  overload changes (bills past the in-flight limit by a margin; a slow query answered apart from a pool wait),
  the k6 capacity test plan, and iOS preparation.
- **Cloud sessions:** no SSH to the VM. Set up as on 8 Nov: start `dockerd`, run `mongo:7.0` as replica set rs0
  (CLAUDE.md), a Python **3.12** venv with `pip install --require-hashes --no-deps -r requirements-dev.lock` (3.13
  is not what CI and the image use), and `npx yarn@1.22.22 install --frozen-lockfile` in `frontend/`. pytest's
  `-q` is already in pytest.ini: run `pytest -o addopts="" -q` to see the summary line.

## The project in one paragraph
**Hara Hisaab (हरा हिसाब)**, formerly NurseryPOS, is a multi-tenant point-of-sale app for plant nurseries.
- **App:** Expo SDK 57 (`frontend/`), version **1.1.0** (versionCode 2) since this session.
- **Backend:** FastAPI and MongoDB (`backend/`) on the shared Oracle VM behind Caddy.
- **Repo:** `nexoraconsulting001-art/Plant_Tech` (moved from `Shashank-Codes-bit/Plant_Tech`; git remote `plant_tech`; `origin` is the old udyanojha/Nzone).
- **Branches:** `staging` deploys to https://nurserypos-staging.140-238-251-141.sslip.io (port 8130, `~/Plant_Tech-staging`). Prod uses port 8120.
- **Deploy:** pushing `staging` runs the GitHub Actions deploy. It is live when the new endpoints appear in `/openapi.json`.
  - From Claude's sandbox the VM is only reachable through PowerShell (`Invoke-WebRequest`, TLS 1.2); Bash `curl`/`ssh` time out.

## Standing rules from the owner
- **No app builds or over-the-air updates** (QA, prod or dev) until the owner has tested in the browser and said go.
- **Shared VM:** never touch other sites, never `docker compose down -v`, never start another Caddy, keep ports localhost-only, and check `ss -ltn` first.
- **No permanent deletes of data by Claude.** The owner runs `~/nurserypos-wipe.sh` themselves.
- **Test logins only on localhost** against the local API, never typed into the browser against staging.
- **Demo customers' phone numbers are dummies.** Don't WhatsApp them.
- **Design:** must look classy, not cheap. No "…" cut-off text, no emoji, quiet branding.
- **Plan first** for big changes, then build.

## How to test
- **Local (safe for logins):**
  - **API:** `backend/tests/devserver.py` (launch config `nurserypos-api-local`, port 8001): in-memory Mongo, a fake Claude, demo owner 9000000009 (password in the launch config env). It needs a Python venv with `backend/requirements.txt` + `requirements-dev.txt`; the launch config's `runtimeExecutable` points at that venv's python, which lives in a session scratchpad, so a new session recreates the venv and updates the path.
  - **Fake Claude answers:** edit `fake_ai.json` in the dev folder the API prints at start (keys `plant`, `invoice`, `names`), e.g. `{"plant": {"kind": "other", ...}}` to try the laptop alert.
  - **Web:** `nurserypos-web-local` (port 8082).
  - **Photos in the browser:** the built-in browser can't pick files; a small JS hook on `HTMLInputElement.prototype.dispatchEvent` that answers the picker's click with a canvas-made JPEG works (see this session's transcript if needed).
- **Owner's browser test app:** `nurserypos-web-staging` at http://localhost:8083, pointed at the staging API.
  - **One web server at a time:** stop 8082 before starting 8083 and vice versa. Two web servers with `--clear` collide (EBUSY).
  - **Stale route types:** after adding route files, restart the dev server if `tsc` complains about typed routes.
- **Phone:** the QA APK, "Hara Hisaab QA" (`com.planteck.nurserypos.qa`, EAS profile `staging`, channel staging).
  - **Current QA build: 1.1.0 (versionCode 2), EAS build `50a49b3b-4d1f-442d-8da0-b49ddf364d04`, built 18 Oct 2026 from d37d61f** after the owner's browser go. APK: https://expo.dev/artifacts/eas/-5E0RBUTgsR_KA61vnKF033F5yPUIQ4OT78eih41k_w.apk (shared as a link; Expo keeps artifacts only for a limited time, so rebuild if it expires).
  - It installs over the old QA app and keeps the login (same app ID and keystore).
  - **OTA updates:** runtime is `appVersion`, so updates to channel `staging` now reach only 1.1.0 phones. Still no OTA or build without the owner's go.
  - **Since the push notifications commit (Nov) the app is 1.2.0 (versionCode 3):** it adds native modules, so its updates must never reach 1.1.0 phones. The next QA step is a 1.2.0 build, not an OTA.
  - **How to build:** `npx eas-cli@latest build --profile staging --platform android --non-interactive --no-wait` from `frontend/` (logged in as the owner's Expo account; run it from PowerShell, the Bash sandbox blocks network).
- **Backend tests:** pytest against a real MongoDB replica set (`MONGO_URL=mongodb://127.0.0.1:27117/?replicaSet=rs0`; transactions need one), run from Git Bash so `tests/test_ops.py` runs too. On `prod-ready`: 440 tests, 438 pass, 2 skipped (`RUN_BENCH=1`, real Redis). `ruff check .` clean.
- **App checks:** `npx tsc --noEmit`, `npx eslint app src` (warnings only, no errors) and `npm run i18n:check`. The package manager is yarn 1 (`npx yarn@1.22.22 add …`; `npx expo install` fails without yarn on PATH).

## Done this session (owner's 12-point feedback + supplier master; one commit per phase on `staging`)
1. **Setup "Add my products" / "Add an employee"** waited for nothing: tapping before the shop was saved bounced back into setup. Now dimmed until saved; `resetTo(router, href, then)` + `takePendingOpen()` in the tabs layout. (64025c2)
2. **Professional copy** across app and server; server messages with numbers come as `AppError` (`backend/errors.py`: English `detail` + `key`/`params`/`code`) so the app can translate them; `confirm.tsx` gained `choose()` and `notice()`. (c0f1539)
3. **Photo guardrails:** the AI first classifies a photo (plant / nursery product / bill / other). Add items and Add product refuse non-nursery items ("Not a nursery item") and bills ("Supplier bill detected" → Open supplier bill); Supplier bill refuses non-bills. Photos are uploaded only after acceptance. Whole-word product matching (`backend/search.py`); unchecked shop photos stay private until the admin approves; scans are refunded when Claude wasn't reached. (c9f6a58)
4. **Profit margin on cost (%)** in setup step 3 and Shop details (default 60); used for new products from supplier bills and as a tap-to-use suggestion in Add product. **Bill without GST** toggle (owner, GST shop only): same total, no GST lines; saved with the customer's order. (ad69490)
5. **In-app crop screen** for every photo (`src/components/crop-host.tsx`, `src/image-tools.ts`): only Retake / Use photo; product photos square, logos and bills free; photos shrunk to ≤1568 px on the phone. App version 1.1.0. (79c00bc)
6. **Suppliers + multi-page bills:** `suppliers` collection (match by GSTIN → phone → name without "M/s"/"Pvt Ltd"; fill missing details only; never duplicated), More → Suppliers list and detail; bills of up to 5 pages read in one call; different suppliers / bills / non-bill pages refused. Old purchases are linked on server start. (63c3f7f)
7. **Spacing and overlap:** every padding/gap on the theme scale, one `SearchBox`, compact `FilterChip`, Sheet `navigationBarTranslucent`, keyboard-aware new-customer form, Welcome top inset. (512ea9e)
8. **Search in any language:** `गुलाब`, `gulab`, `rose` all find Rose (translit + sound-alike matching, library Hindi names and aliases); Products tab searches on the server; new shop-made catalogue entries get Hindi/Hinglish names looked up in the background (`AI_ENRICH_NAMES`, not counted as scans); `backend/enrich_library.py` for existing ones. (2ccaf50)
9. **Hindi:** the owner approved the glossary (everyday shop Hindi with ऑर्डर, बिल, स्टॉक, प्रोडक्ट…). All 626 texts are in `frontend/src/locales/hi.json`, including the server's messages. More → Language and a toggle on Welcome; the choice is kept per phone; English is the default. Customer bills and the WhatsApp message stay English (`i18n.getFixedT("en")`). Text containing Devanagari gets 1.5x line height (`hindiRoom` in `theme.ts`). Suggested category names are translated; names a shop typed pass through.
   - **Keep it complete:** `npm run i18n:check` (frontend) fails on any text without a Hindi translation or with different `{{placeholders}}`; `--missing` prints the gaps as JSON. Run it before every commit that adds text.
10. Point 8 (saved orders per customer) was already done in 86c0b9e; nothing to build.

**Checked in the local browser:** onboarding race (slow network), laptop / bill / not-a-bill alerts, similar products on a new item, Bill without GST through to the printed bill, profit field and suggestion, crop screen (square and free, drag, Use photo), two-page bill → new supplier → Suppliers detail, chips fitting at 360 px, Hindi/Hinglish search.

**Owner's browser test:** passed on 18 Oct ("looks good"); the 1.1.0 QA APK was built the same day.

**Roadmap (2 Nov):** open issues first, then iOS live, then production and scale. See `docs/ROADMAP.md`; pricing tiers in `docs/TIERS-PLAN.md`.

## Testers' round (2 Nov 2026, on `staging`, not pushed until the owner's browser go)
Owner's answers: both addresses on the bill; WhatsApp on/off; several UPI IDs with a default, changeable before the QR;
owners may sell below stock with a warning, employees may not; push notifications and the daily summary for the owner only;
bills can be corrected for 7 days; decimals by product unit, never for plants.
- 0da049e units per product (pcs/bag/packet/tray whole; kg/g/litre/ml/metre decimals) and a typeable quantity everywhere.
- d7f3e21 selling below stock: owner warned (Found, cart, Pay) and the stock goes negative (`allow_short`, owner only on the server); employees blocked; Home "Sold below stock" card.
- 3df3276 customer address (billing form, Edit customer, printed under "Bill to"), New order from a customer.
- 4f8e071 "Send bill on WhatsApp" switch (on: WhatsApp opens after billing); `upi_ids` + `default_upi`, Change on the QR screen, UPI ID saved on the bill; old clients still get `upi_id`.
- 562536b correct/cancel within 7 days, **owner only** (one constant in `sales.py`): cancel + new linked number, stock and totals restored, reports skip Cancelled. The Pay screen says what to give back or collect.
- 386fd6d Home low-stock card, More → Notifications (owner), push via Expo (`PUSH_ENABLED`, off by default), daily summary loop (default 21:00 IST). Setup steps in `docs/PUSH-SETUP.md` (Firebase, owner does it).
- 163318b review fixes; app is now 1.2.0 (versionCode 3).
- Checked in the local browser on 2 Nov: 40 pcs typed, below-stock warnings and "Bill anyway", two UPI IDs and the QR picker, WhatsApp auto-open, Correct bill (INV-0424 → INV-0425), give-back amount, Notifications screen.
- Not checked yet: a phone (push needs Firebase + a 1.2.0 build), the employee view in the browser, Hindi on the new screens.

## Code review fixes (8 Nov 2026, `prod-ready` 776f980)
A code review of everything production would get (`main...prod-ready`, 97 commits, ~31k lines of code) found 11
issues; 10 are fixed with tests, one is left by the owner's D1 answer.
- **Supplier bills never add stock twice:** POST /api/purchases takes an `Idempotency-Key` like bills (shared
  helpers in `backend/idempotency.py`, which bills use too); the same key answers with the saved supplier bill, a
  changed one is refused (409 `idempotency_mismatch`, `params.purchase_id`). The supplier-bill screen sends one key
  per bill and says when a try may or may not have been saved. 1.1.0 sends no key: unchanged.
- **Catalogue look-ups** for a supplier bill's new items run 8 at a time, once per name.
- **Job queue fails fast when Redis is down** (`backend/jobs.py`): one connection try with a 1 s limit, 3 s per
  enqueue, then 30 s of immediate `QueueDown`. Before, arq's pool tried 5 times a second apart, so a bill that
  crossed a low-stock line waited 5-10 s while Redis was down. A name look-up that cannot be queued no longer
  fails the product save.
- **Deploys find their image:** with `IMAGE_SOURCE=ghcr`, deploy.sh pulls `tree-<hash of backend/>` (then the
  commit tag); deploy.yml tags both. Before, a docs-only or app-only commit after a backend push made the
  production deploy fail. The swap now starts web and worker only (`--no-deps`), so migrate and data-owner no
  longer run twice per deploy.
- **Found by the QA deploy and its rehearsal:** compose.yaml's process limits (pids_limit) made Docker Compose refuse
  the project; `rollups.py rebuild --touched-since` always failed. Both fixed (07c84aa, d655670) with a test.
- **CSV exports** escape typed names a spreadsheet would run as a formula (`common.csv_text`; invoices.csv,
  GSTR-1 names and HSN descriptions).
- **Smaller:** PATCH /customers answers 409 (not 500) when another phone took the number a moment before; PUT
  /business answers 404 for an erased shop; erasing a shop also removes its `storage_usage` row and notification
  markers (`storage_client.delete_many`).
- **Left as it is (owner, D1):** eas-update.yml falls back to the sslip.io address for the production channel;
  revisit with the domain.
- **Checks on 8 Nov:** backend 450 passed, 2 skipped (12 new tests; ruff clean; shellcheck clean on deploy.sh);
  app tsc clean, eslint 0 errors, i18n 850 texts, Node tests 18/18. Not checked in a browser or on a phone yet.

## Production readiness and scale (Nov 2026, local branch `prod-ready`, not pushed)
Owner's ask: production-ready with standard tools, sized for 1000 nurseries x 200 bills a day (peak ~14 bills/s,
~250 requests/s). Branch `prod-ready` (from `staging`) holds the 14 work packages, the review fixes and the
load-test fixes; `docs/PRODUCTION-PLAN.md` has the full status, the owner decisions D1–D17 still open and the
cost by phase. Nothing is pushed, deployed or built.
- **Load test:** `loadtest/` (locust and k6, `seed.py` for 1000 shops). Laptop runs on 3 Nov after the fixes:
  1x had 0 errors and bills p95 520 ms; 3x shed load with fast 503 `busy` and recovered at once. A capacity number
  still needs k6 from a separate machine against staging-sized hardware.
- **App checks the way CI runs them:** `npx eslint app src`, and `tsc` with a stale `.expo/types/router.d.ts`
  moved aside; Node tests `node --test --experimental-transform-types tests/*.test.mjs scripts/test-paging.mjs`.

## Open items for next session
1. **Phone checks on the 1.1.0 QA APK** (the owner is installing it): QA sections O (crop: dragging, photos taken sideways come out upright, back button), I.11–I.13 (bottom buttons, sheets, keyboard), M.6–M.11 (bill pages taken one after another with the camera), S (suppliers) and T (Hindi; especially T.7, no clipped vowel marks at the largest font). Fix whatever the owner reports.
2. **Do not move to production yet** (owner, 18 Oct). `main` stays on 19ca6f3 (the old single-shop version); no merge of `staging` into `main` until the owner says so.
3. **Staging backfill of catalogue names:** `docker compose exec web python enrich_library.py --limit 50` in `~/Plant_Tech-staging` (one small AI call per shop-made entry). Not run yet.
4. **Login-wait fix (still waiting for the owner's OK).** After a page reload, a screen opened by direct link (e.g. `/more/shop`) can query before the saved login is read and spin or show "Please log in". Proposed fix: render the root `<Stack/>` only once `useAuth().ready` is true (`app/_layout.tsx`).
5. **Welcome page and brand polish:** owner picks A / B / C on the design canvas (https://claude.ai/artifact/JNyyF5Zo4hHZAezhMRRwED, version 12), then build (see previous notes in git history).
6. **Plant identification upgrade:** research done, no code (Opus with higher effort, top 3 answers, match against the shop's own photos, optional Pl@ntNet).
7. **Production release:** only when the owner asks (QA-CHECKLIST section R). Prod still runs the old single-shop version.

## Demo shop
- **Account:** owner 9654346434 on staging (also the admin), "Green House" shop. It has 25 plants, 15 products, 45 customers and ~400 orders.
- **Daily refresh:** a cron job runs at 00:00 UTC (5:30 am IST) with `deploy/demo-refresh.sh staging 9654346434`. It moves all dates forward so the newest day is always today, and refuses to run on non-demo shops.
