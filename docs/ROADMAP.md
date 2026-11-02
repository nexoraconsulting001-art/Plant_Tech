# Hara Hisaab: roadmap (2 Nov 2026)

The order the owner set: **fix open issues first, then put an iOS version live, then scale.** Production
(`main`) is on hold until the owner asks for it. Plans here keep production and scale in view so we don't
have to redo work later. Pricing and plan limits are in `TIERS-PLAN.md`.

## Where things stand
- **Repo:** `nexoraconsulting001-art/Plant_Tech`. It moved from `Shashank-Codes-bit/Plant_Tech`; GitHub redirects the old URL.
- **In sync on 2 Nov:** local `staging` = GitHub `staging` = 3d0f57f.
  - The QA server runs that backend. The last deploy was b4a395d, and no backend file changed after it.
  - The QA APK 1.1.0 was built from d37d61f. 3d0f57f only changed notes.
- **Production:** `main` = 19ca6f3, the old single-shop version. Don't touch it until the owner says so.

## Stage 1: open issues (now)
Carried over from `SESSION-NOTES.md`:
1. Fix what the owner finds in the phone checks on QA APK 1.1.0 (QA sections O, I.11–I.13, M.6–M.11, S, T).
2. **Login wait after a page reload:** render the root `<Stack/>` only once `useAuth().ready` (`app/_layout.tsx`). Waiting for the owner's OK.
3. Fill in the Hindi and Hinglish catalogue names on staging (`enrich_library.py --limit 50`).
4. Welcome page and brand polish: the owner picks option A, B or C on the design canvas first.
5. Better plant identification: the research is done, nothing is built yet.

Every fix goes on `staging`, the owner tests it in the browser, and only then do we build or send an OTA update.

## Stage 2: iOS live
The app is Expo, so most of it already runs on iPhone. What's missing:

| Need | Notes |
|---|---|
| Apple Developer account (₹8,700/yr approx.) | The owner enrols, an individual or the company (company needs a D-U-N-S number). Choose before the first upload, because the seller name shows on the App Store. |
| `eas.json` iOS settings | No iOS settings yet. QA: a `staging` build with `ios.simulator: false`, sent through **TestFlight** (iOS has no install-from-link APK). Production: the `production` profile plus `submit.production.ios` (ascAppId). |
| Bundle IDs | Already set: `com.planteck.nurserypos` and `.qa` (`app.config.js`). Register both in App Store Connect. |
| Real domain + HTTPS | `*.sslip.io` works but looks temporary to App Review and testers. Get a domain (e.g. `api.harahisaab.in`) before the iOS release. Do the same for Android to keep the backend URL the same everywhere. |
| Privacy | A privacy policy URL, App Privacy answers (phone numbers, photos sent to Claude) and account deletion inside the app (Apple guideline 5.1.1(v)). **Account deletion isn't built yet.** |
| Sign-in | Google sign-in is off (`src/google.ts`). If it is turned on for iOS, Apple requires **Sign in with Apple** too (guideline 4.8). Phone and password alone are fine. |
| Camera, photos, crop | Permission texts are already in `app.json`. Test the crop screen and multi-page bill capture on a real iPhone (EXIF rotation differs). |
| UPI and WhatsApp | `upi://` and WhatsApp links need `LSApplicationQueriesSchemes` on iOS. Check them on a phone. |
| Hindi fonts | Check the Devanagari line height on iOS at the largest text size (QA section T.7). |
| Store listing | Screenshots for 6.7" and 6.5" iPhones (and iPad, because `supportsTablet: true`; turn it off if we won't support iPad), description in English and Hindi, review notes with a demo login. |

Order: account → iOS profiles in `eas.json` → TestFlight QA build (after the owner's browser go) → phone checks → domain + privacy + account deletion → App Store review.

## Stage 3: production and scale
**Release to production (when the owner asks):** QA-CHECKLIST section R.
- Back up prod Mongo.
- The old single-shop data must move to the multi-shop schema. Write and test a migration script on a copy first.
- Merge `staging` into `main`. That deploys automatically.
- Then build `production` for Android, and iOS when it's ready.

**Limits of today's setup and what to change, in order:**
1. **One shared Always Free VM (6 GB, Arm).** Prod, staging and the other apps all share it. Fine for tens of shops. For paying shops, move prod to its own VM or a small paid instance, and keep staging on the shared one.
2. **Mongo in a container with a 0.25 GB cache, backups on the same disk** (`deploy/backup.sh`, 14 days kept). If the VM is lost, so are the backups. Copy backups to R2 now (cheap). Later, move to MongoDB Atlas (managed, point-in-time restore) when the data or the risk justifies it.
3. **Uploads:** R2 is used when `R2_BUCKET` is set. Keep it set in prod so photos never live on the VM disk.
4. **One uvicorn worker.** Add `--workers 2–4`, or gunicorn, when CPU is busy. Check first that nothing keeps per-process state (the AI daily limit is counted in Mongo, which is safe).
5. **AI cost is the main cost per shop** (see `TIERS-PLAN.md`): Phase 0 there measures and cuts it (Haiku for plant photos, caching) before plans and payments.
6. **Watching it:** no error tracking or uptime alerts today. Add Sentry (app + API) and an uptime check on `/api/` before the first paying shop.
7. **Releases:** keep `staging` → owner check → `main`. Use OTA updates for JS-only fixes (runtime = app version) and store builds for native changes.

## Decisions waiting on the owner
- Apple Developer: individual or company account.
- Domain name for the API and the privacy policy page.
- Whether iPad support is in scope.
- When to release staging to production.
