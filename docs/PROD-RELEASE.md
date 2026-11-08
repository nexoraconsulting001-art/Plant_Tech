# Production release and the pilots' move (planned 8 Nov 2026)

The owner's decision (8 Nov): production gets this version, QA's data is copied into it exactly (the pilot shops
and the demo shop), the pilot shops move to the production app, and app builds come from `main` from then on.
Production keeps the sslip.io address for now (D1). Production has no users, so nothing there is lost: its old
data is backed up by the copy command before it is replaced.

Everything below was rehearsed with real Docker in the cloud session (old production version upgraded on
production settings, QA copied in, logins and photos checked on both sides). Times are rough.

## A. Before the release (owner, once, about 30 minutes)

1. **Production settings on the VM.** Log in to the VM (as for the Claude key) and run:
   ```bash
   cd ~/Plant_Tech-staging && git fetch -q origin prod-ready && git show origin/prod-ready:deploy/prod-env-from-qa.sh > /tmp/prod-env-from-qa.sh && bash /tmp/prod-env-from-qa.sh
   ```
   It writes production's `~/Plant_Tech/deploy/.env` from QA's: the same Claude and photo-storage keys and
   limits, and production's own: `ENV=production`, a new login secret, its own photo folder (`nurserypos`, QA's is
   `nurserypos-staging`), its address, its name and port as today, and `ALLOW_NO_ROLLBACK=1` for the first deploy.
   The old file is kept as `.env.before-<time>`. It prints no keys. Optional: a separate Claude key for
   production (edit the `ANTHROPIC_API_KEY=` line with `nano ~/Plant_Tech/deploy/.env`).
2. **GitHub**, repository Settings > Environments:
   - `production`: Required reviewers = you (a production deploy and a production app build wait for your
     approval). Environment secrets: add `EXPO_TOKEN` (the same token as for staging is fine).
   - `staging`: add `EXPO_TOKEN` too, for QA app builds.
3. **Expo** (expo.dev > project nurserypos > Environment variables > Add variables), environment **production**:
   - `EXPO_PUBLIC_BACKEND_URL` = `https://nurserypos.140-238-251-141.sslip.io` (Plain text)
   - `ALLOW_IP_BACKEND_URL` = `1` (Plain text). The app refuses an sslip.io address for production unless this
     is set, so that nobody ships one by mistake; here it is the decision (D1). Moving to a domain later is an
     app update (both addresses must work during the move).
   - `GOOGLE_SERVICES_JSON`: Edit, tick **production** as well.
4. Tell Claude: **release**.

## B. Release (Claude and owner, about 45 minutes; not 18:00-21:30)

1. Claude pushes the same version to `main` (production) and `staging` (QA).
2. QA updates by itself (about 10 minutes). Production waits for you: GitHub > Actions > "Backend deploy" (the run
   for `main`) > Review deployments > production > Approve. GitHub first builds the production image (up to about
   30 minutes), then the VM deploys.
3. Claude checks production from outside (`/readyz`). If the deploy stops, nothing was changed: production keeps
   running the old version, and Claude reads the log and says what to do.
4. After a good deploy, remove the one-time switch on the VM:
   ```bash
   sed -i '/^ALLOW_NO_ROLLBACK=/d' ~/Plant_Tech/deploy/.env
   ```
From now on `main` has the "App build" button (GitHub > Actions > App build).

## C. The production app (after your go for the build)

GitHub > Actions > App build > Run workflow: Branch **main**, profile **prod-apk** (an APK to share; the Play
Store needs `production` later), a short note > Run > approve (production environment). The run waits for Expo
and puts the download link in its summary. Install it on your phone; log in after the move (step D).
A QA app build: the same with Branch **staging** and profile **staging**.

## D. The move evening (after the shops close)

1. Tell the pilots: "Tonight after closing, please don't bill on the QA app. Tomorrow morning install the new
   Hara Hisaab app (link) and log in with the same number and password." The QA app pauses during the copy
   anyway, so a forgotten bill cannot slip through.
2. On the VM:
   ```bash
   ~/Plant_Tech/deploy/copy-qa-to-prod.sh
   ```
   It shows what it will copy and asks you to type `COPY`. About 5 minutes. It backs up both databases first,
   replaces production's with QA's, copies the photos into production's folder, removes QA-app phones from
   notifications, compares every collection's record count, and starts both again. The last line says `done`.
3. Tell Claude; Claude checks production from outside. Log in on the production app: your shop, today's bills,
   photos, the demo shop.
4. Send the pilots the app link. They can delete the QA app (it keeps QA's copy, which no longer changes in
   production).
5. Keep the backup folder the script names (in `~/.nurserypos/copies/`) for a week, then delete it: it holds the
   shops' data.

If anything stops: QA starts again by itself, nothing is lost, and the script prints how to put production back.
Tell Claude what it printed.

## E. After the move

- **Backups for production** this week: docs/RUNBOOK.md, section 3 (nightly, encrypted, off the VM).
- **Notifications**: docs/PUSH-SETUP.md (FCM V1 key in Expo credentials, `PUSH_ENABLED=1` and
  `EXPO_ACCESS_TOKEN` in production's `deploy/.env`, then `~/Plant_Tech/deploy/deploy.sh prod`).
- **Play Store**: the Google Play account (personal or company), the store listing, and for a new personal
  account the closed test Google asks for first (12 testers for 14 days, as of 2026; a company account skips it).
- **A domain** (D1) before the Play Store release.
- QA keeps a copy of the pilot shops (real customers' numbers): no destructive tests on them, never message their
  customers from QA; the owner can remove them from QA later.
