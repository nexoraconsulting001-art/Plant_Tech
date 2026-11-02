# Hara Hisaab: where we are (session notes, 18 Oct 2026, end of day)

Hand-over notes so the next session can pick up without the old chat. No secrets here: keys and
passwords live only in the VM's `deploy/.env` and in EAS, and the owner enters them.

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
- **Backend tests:** pytest with mongomock (67 passing).
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
