# Hara Hisaab runbook

What to do, step by step, for deploys, rollbacks, backups and restores, incidents, secret rotation and scaling.
Server layout and first-time setup: [deploy/README.md](../deploy/README.md). The plan behind it:
[docs/PRODUCTION-PLAN.md](PRODUCTION-PLAN.md). Times are UTC on the VM and in logs; India is UTC+5:30.

| | Production | Staging (QA) |
|---|---|---|
| Branch / GitHub Environment | `main` / `production` (approval needed) | `staging` / `staging` |
| API | `PUBLIC_URL` variable, today https://nurserypos.140-238-251-141.sslip.io | https://nurserypos-staging.140-238-251-141.sslip.io |
| On the VM | `~/Plant_Tech`, port 8120, project `nurserypos` | `~/Plant_Tech-staging`, port 8130, project `nurserypos-staging` |
| Deploy log | `~/.nurserypos/prod/deploy.log` | `~/.nurserypos/staging/deploy.log` |

Service targets (decision D13, to confirm): 99.9% monthly uptime, p95 bill save under 500 ms.
On call: _name and phone to be filled in by the owner_ (paged by the uptime monitor).

---

## 1. Deploy

### Normal path
1. Pull request into `staging`; CI must be green ("CI result": lint, tests on a real replica set, pip-audit, Trivy,
   gitleaks, app checks). Merge.
2. The push to `staging` deploys staging by itself (`.github/workflows/deploy.yml`). Watch the run; it ends with the
   public `/health` and `/readyz` check.
3. QA on the "Hara Hisaab QA" app (`docs/QA-CHECKLIST.md`), including the 1.1.0 regression run for any
   [COMPAT 1.1.0] package.
4. Merge `staging` into `main`. The workflow builds `ghcr.io/<owner>/hara-hisaab-api:<sha>` (arm64), then waits in
   the `production` environment. The reviewer approves in the run's page; only then does the VM deploy.
5. After a production deploy: open Sentry (new errors for this release), the uptime monitor, and make one test bill
   in the owner's shop.

Avoid deploying production during the evening rush (18:00-21:30 India time) unless it is a fix for that rush.

### What the VM does (`deploy/deploy.sh <staging|prod>`)
sync the checkout -> build (or pull from GHCR when `.env` has `IMAGE_SOURCE=ghcr`) -> `python -m settings` ->
`python -m migrations up` (old version still serving) -> swap `web` and `worker` -> `/readyz` 200 three times in a
row within 90 s -> otherwise the previous image goes back by itself and the run fails. After a good swap, if a version
without daily rollups served bills meanwhile (see "Daily rollups" below), it rebuilds them for the days concerned.
During the swap the API is down for a few seconds; Caddy holds requests for up to 30 s (`lb_try_duration` in
`deploy/Caddyfile.snippet`) so the app sees a slow request, not an error. Zero-gap deploys need two API instances
behind the load balancer (scaling step 3).

### Rules for migrations (so a rollback is always possible)
- Expand, then contract: add fields, collections and indexes in release N; stop using old ones in N; remove them in
  N+1 or later. Never rename or delete in the same release that stops using it.
- A migration is idempotent and safe to run while the previous version is serving.
- A unique index on existing data: the migration first finds and fixes duplicates, then builds the index.

### By hand on the VM
```bash
~/Plant_Tech-staging/deploy/deploy.sh staging          # or ~/Plant_Tech/deploy/deploy.sh prod
tail -n 20 ~/.nurserypos/staging/deploy.log
```

### Daily rollups (`REPORTS_FROM_ROLLUPS`)
Home's today card, the Orders totals, the sales report and the 9 pm summary read `daily_sales` and
`daily_item_sales` when `REPORTS_FROM_ROLLUPS=1`. Every bill of this version updates them, but a version without
`backend/rollups.py` (the one before WP-2) does not. Such a version serves bills while the first deploy migrates, and
again after a rollback to it. `deploy.sh` notes the first such day in `~/.nurserypos/<target>/rollups-stale-since`.
After the next good swap it runs `python rollups.py rebuild --touched-since <that day>`. That rebuilds every shop's
days from that day minus 7 (bills can be corrected for 7 days) and then deletes the note. If the rebuild fails, the
deploy still keeps the new version but exits 1, and the next deploy tries again. To run it by hand:
```bash
cd ~/Plant_Tech/deploy
docker compose run --rm --no-deps migrate python rollups.py rebuild --touched-since "$(cat ~/.nurserypos/prod/rollups-stale-since)"
rm ~/.nurserypos/prod/rollups-stale-since
docker compose run --rm --no-deps migrate python rollups.py rebuild   # every day of every shop (slow; always safe)
```
Rebuilding is safe while the app is billing. Until the first rebuild after the first deploy of WP-2 has worked, keep
`REPORTS_FROM_ROLLUPS` off (the reports then read the bills).

### App admins
Admin rights (the shared picture library) are a flag on the login. Migration 0801 sets it once for the numbers in
`ADMIN_PHONES` that already have a login. On a new database, or for a new admin, the person signs up in the app
first, and then:
```bash
cd ~/Plant_Tech/deploy
docker compose run --rm --no-deps migrate python -m manage_users admin grant <phone>
docker compose run --rm --no-deps migrate python -m manage_users admin list
```

### One-time GitHub setup (owner)
- Settings > Environments > `production`: Required reviewers = the owner; Deployment branches = `main` only.
  Without this the production job runs with no approval.
- Settings > Branches: protect `main` and `staging`; require the "CI result" check and a pull request for `main`.
- Settings > Code security: Dependabot alerts and security updates on; secret scanning and push protection on.
- Variables (Settings > Secrets and variables > Actions > Variables), all optional until the domain exists:
  `PUBLIC_URL` (per environment), `DEPLOY_HOST`, `DEPLOY_KNOWN_HOSTS`, `STAGING_IMAGE_SOURCE=ghcr`,
  `APP_BACKEND_URL` (per environment, for app updates).
- Secrets: `NURSERYPOS_DEPLOY_KEY` (exists), `EXPO_TOKEN` (per environment, only for the "App update" workflow).

### App releases
- Binaries: `eas build` as in `docs/SESSION-NOTES.md`, only after the owner's go.
- Over-the-air updates: only from Actions > "App update" (`.github/workflows/eas-update.yml`), never from a laptop.
  An update reaches only phones whose app version matches `frontend/app.json` (the 1.1.0 QA build never gets 1.2.x
  code).

---

## 2. Rollback

**Automatic:** a deploy whose new version does not get ready puts the old image back and fails the run. Read why in
the run log and `docker compose logs --tail 100 web`.

**By hand, the image** (the code was ready but is wrong):
```bash
~/Plant_Tech/deploy/deploy.sh prod rollback              # the image that ran before the last deploy
docker image ls nurserypos-api                           # images kept: :latest, :rollback and the last four commits
~/Plant_Tech/deploy/deploy.sh prod rollback <commit sha> # any of those
```
The checkout stays on the new commit, so the next push deploys again: revert the bad commit on `main` (pull
request) as the follow-up. A rollback to an image from before the worker (no `worker.py`) starts `web` alone. A
rollback to an image from before the daily rollups notes the day, and the next deploy rebuilds them (section 1,
Daily rollups). Over SSH with the deploy key: `ssh ... ubuntu@<vm> "prod rollback"` (once the new
`ci-deploy` is installed).

**The data** (a migration or a bug damaged data): rolling the image back does not undo data changes. Decide with the
owner between fixing forward (a script or migration that repairs the rows) and a restore (section 4). A restore
loses every bill since the backup, so fixing forward is almost always better.

---

## 3. Backups

Until the database moves to Atlas (whose continuous backup with point-in-time restore replaces this section):
`deploy/backup.sh` runs nightly from cron, dumps MongoDB at one consistent point in time (`--oplog`), encrypts it
with [age](https://github.com/FiloSottile/age) on the way to disk, copies it to a bucket outside the VM with
[rclone](https://rclone.org), checks the copy and pings a heartbeat.

### One-time setup
1. **Key pair, on the owner's laptop (not the VM):** `age-keygen -o hara-hisaab-backup.agekey`. Keep the file in the
   password manager and one offline copy. The line `# public key: age1...` is the recipient.
2. **On the VM:** `sudo apt install age rclone`; put the public key in `~/.config/hara-hisaab/backup-recipients.txt`;
   set `BACKUP_AGE_RECIPIENTS_FILE=/home/ubuntu/.config/hara-hisaab/backup-recipients.txt` in `deploy/.env`.
3. **Bucket:** a separate bucket only for backups (Cloudflare R2 `hara-hisaab-backups`, or Backblaze B2). Lifecycle
   rule: delete after 35 days. B2 or AWS: turn on Object Lock (compliance mode, 35 days) so a stolen VM key cannot
   delete backups. R2: an API token with Object Read & Write on that one bucket only.
4. **rclone remote:** `rclone config` -> new remote `r2-backup`, type `s3`, provider `Cloudflare`, the token's keys,
   endpoint `https://<account id>.r2.cloudflarestorage.com`, and `no_check_bucket = true`. Set
   `BACKUP_RCLONE_REMOTE=r2-backup:hara-hisaab-backups/prod` (staging: `.../staging`).
5. **Heartbeat:** a heartbeat monitor in Better Stack or Healthchecks.io expecting one ping a day with a 2 h grace
   period; `BACKUP_HEARTBEAT_URL` = its URL, `BACKUP_HEARTBEAT_FAIL_URL` = its `/fail` URL.
6. **Cron** (re-add after rebuilding a VM; cron jobs are not in git):
   ```
   0 2 * * * /home/ubuntu/Plant_Tech/deploy/backup.sh >> /home/ubuntu/Plant_Tech/deploy/backup.log 2>&1
   30 2 * * * /home/ubuntu/Plant_Tech-staging/deploy/backup.sh >> /home/ubuntu/Plant_Tech-staging/deploy/backup.log 2>&1
   0 0 * * * bash /home/ubuntu/Plant_Tech-staging/deploy/demo-refresh.sh staging 9654346434 >> /home/ubuntu/nurserypos-demo.log 2>&1
   ```
7. Run `deploy/backup.sh` once by hand and check: a `.age` file in `deploy/backups/`, the same file in the bucket,
   the heartbeat green.

Photos: production keeps them in R2 (11 nines durability); a copy to a second provider is a worker job (WP-5).
Staging photos on local disk are not backed up.

### Restore drill (monthly, and after any change to backup.sh)
On a laptop or scratch VM with docker, age and rclone (and the rclone remote configured read-only):
```bash
deploy/restore-drill.sh --identity ~/keys/hara-hisaab-backup.agekey --remote r2-backup:hara-hisaab-backups/prod
```
It fetches the newest backup, checks its sha256, decrypts it into a throwaway `mongo:7.0` container, replays the
oplog, prints row counts and the newest bill's time, and removes the container. Pass = exit 0, counts look right,
newest bill is from the evening before. Add its `drill:` line to the log below.

**Drill log**

| Date | Backup | Size | Fetch | Restore | Total | Who | Notes |
|---|---|---|---|---|---|---|---|
| _first drill pending_ | | | | | | | Run after the one-time setup above; the scripts are tested for syntax and logic only (no docker/age on the build machine). Expected for today's data (well under 1 GB): about 1-3 min in total. |

---

## 4. Restore (for real)

Restoring replaces the database with the backup: every bill since then is lost from the app (the shops' paper and
WhatsApp copies still exist). Only with the owner's decision, and announce it first.

**Self-hosted MongoDB (compose):**
```bash
cd ~/Plant_Tech/deploy
docker compose stop web worker                     # nothing writes while restoring (Caddy shows 503)
./backup.sh                                        # a last backup of the current state, whatever it is
# copy the chosen .age file and the private key to the VM for this step only, then:
age -d -i /tmp/restore.agekey backups/<file>.age | docker compose exec -T mongo mongorestore --archive --gzip --oplogReplay --drop
shred -u /tmp/restore.agekey
docker compose start web worker && curl -s 127.0.0.1:8120/readyz
```
Check: log in as a test shop, open today's bills and the dashboard. Then write down what was lost (from the last
backup's time to the restore) and tell the affected shops.

**Atlas:** Cluster > Backup > Restore > point in time (pick the minute before the damage) into a **new** cluster,
check it, then point `MONGO_URL` in `deploy/.env` at it and run `deploy.sh prod`. Never restore over the live
cluster without a fresh snapshot.

**Losing the whole VM:** new VM -> docker, Caddy (snippet), the deploy user and keys (deploy/README.md, First
deploy) -> restore the latest backup as above (or point at Atlas) -> cron jobs (section 3) -> DNS / Cloudflare.

---

## 5. Incidents

### First 10 minutes
1. **Is it down for everyone?** `curl -s https://<api>/readyz` and the uptime monitor. `{"checks":{"mongo":"down"}}`
   or `"storage":"down"` says which part.
2. **Did something just change?** The last deploy (`tail ~/.nurserypos/prod/deploy.log`, Actions). If the trouble
   started with it: roll back first (section 2), investigate after.
3. **Errors:** Sentry for the release; `docker compose logs --since 15m web | grep '"level": "error"'`.
4. **Resources:** `docker compose ps`, `docker stats --no-stream`, `df -h`, `free -h`. Other sites on the shared VM
   use the same disk and memory.
5. **Tell people:** a line on the status page / the shops' WhatsApp group: what is affected, next update time.

Severity: **S1** billing down or wrong totals for any shop -> everything else stops; **S2** a feature down (photos,
AI scans, WhatsApp links, push); **S3** slow or cosmetic.

### Playbooks
- **API not ready, Mongo down:** `docker compose logs --tail 50 mongo`; disk full is the usual cause (`df -h`; old
  images `docker image prune`, old backups in `deploy/backups/`). `docker compose up -d mongo`, then wait for
  `readyz`. Never `down -v`.
- **Slow bills (p95 over 500 ms):** `/metrics` per route; Atlas Performance Advisor / `db.currentOp()` for
  collection scans; the job queue length in Redis (`docker compose exec redis redis-cli zcard arq:queue`, the arq default queue).
  Short-term relief: more API workers (`WEB_CONCURRENCY`, section 7).
- **AI scans failing:** Anthropic status page; the shop gets its scan refunded when Claude was not reached. A cost
  spike: the workspace spend limit is the hard stop (decision D4); lower `AI_DAILY_LIMIT` and redeploy.
- **Photos failing:** `readyz` storage check; R2 status; the R2 token may have been rotated (section 6).
- **A secret leaked** (in a commit, a log, a chat): rotate it at once (section 6), then check its provider's logs
  for use. gitleaks in CI blocks new ones; a leaked one stays in git history, so rotation is the fix, not a rewrite.
- **Abuse / traffic spike:** Cloudflare WAF rate-limit rules on `/api/auth/*` and `/i/*`; the app's own limits
  (ratelimit.py) need Redis up.

### Afterwards
A short write-up within two days (what happened, impact in shops and minutes, why, what changes), kept in
`docs/incidents/`. Turn each "what changes" into an issue.

---

## 6. Rotating secrets

Rotate once a year, when someone with access leaves, and at once after a leak. Values live only in `deploy/.env`
on the VM, GitHub secrets and the providers' consoles; never in git or chat.

| Secret | Where | How | Effect |
|---|---|---|---|
| `JWT_SECRET` | `deploy/.env` | `openssl rand -hex 32`, edit `.env`, `deploy.sh prod` | **Everyone is logged out** (owners log in again; 1.1.0 phones too). Do it in a quiet hour and tell shops first. To log out just one person instead: a password change through the app raises their `token_version`. |
| `ANTHROPIC_API_KEY` | `.env` | Console: create the new key in the same workspace, put it in `.env`, `deploy.sh`, then delete the old key | none |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | `.env` | Cloudflare: new API token (Object Read & Write, the photos bucket only), `.env`, `deploy.sh`, check `/readyz`, revoke the old one | none |
| MongoDB password (Atlas) | `MONGO_URL` in `.env` | Atlas: add a second database user, switch `MONGO_URL`, `deploy.sh`, delete the old user | none |
| Deploy key `NURSERYPOS_DEPLOY_KEY` | GitHub secret + VM `authorized_keys` | `ssh-keygen -t ed25519 -f nurserypos_deploy_new`; add the new public key with the same `command=` line; update the GitHub secret; push a no-op to staging; remove the old line | none |
| `EXPO_ACCESS_TOKEN`, `EXPO_TOKEN` | `.env`, GitHub environments | expo.dev: new robot token, replace, revoke the old | none |
| `GOOGLE_CLIENT_IDS` | `.env` | Only if the OAuth client is deleted; IDs are not secret | |
| `SENTRY_DSN`, `METRICS_TOKEN` | `.env` | Sentry: new client key, then disable the old; `METRICS_TOKEN`: new value in `.env` and the scraper | none |
| Backup age key | laptop / password manager; public key on the VM | New pair; new public key in the recipients file (keep the old private key until the last backup made with it has expired, 35 days) | none |
| rclone token | `~/.config/rclone/rclone.conf` on the VM | New R2/B2 key for the backup bucket, `rclone config update r2-backup ...`, run `backup.sh`, revoke the old | none |
| GHCR pull token | `docker login ghcr.io` on the VM | New fine-grained token (read:packages), `docker login` again | none |

---

## 7. Scaling steps

Measure first: run the load test (`loadtest/README.md`) before and after each step and log it below. Move to the
next step on these triggers, not on guesses.

| Step | When | What |
|---|---|---|
| 1. Dedicated production VM | Before the first paying shop | Oracle A1 4 OCPU / 24 GB (decision D3). API, worker and Redis as compose services; `WEB_CONCURRENCY=4`; Caddy with the snippet; staging stays on the shared VM. |
| 2. Atlas | Before the first paying shop | M10 in Mumbai with continuous backup; `MONGO_URL` to it; stop the mongo service; this backup section becomes the Atlas one. |
| 3. Second API VM + Cloudflare Load Balancing | p95 bill over 300 ms at the busiest hour, CPU over 60% for 15 min, or 100 shops | Same compose without mongo, pointing at Atlas and a managed Redis; Cloudflare LB with `/readyz` health checks; deploy one VM at a time (rolling, no gap). |
| 4. Bigger Atlas tier | Atlas cache dirty over 5%, reads from disk climbing, or connections over 60% | M10 -> M20 -> M30 (about 400-600 shops) -> M40 (1000). Online, no downtime. |
| 5. Worker on its own VM | Queue length not returning to zero, or the worker's CPU disturbing the API | 1-2 OCPU; exactly one worker (the scheduled jobs must not run twice). |
| 6. Third API VM | 1000 shops, or step 3's triggers again | Any two VMs carry the design peak; the third covers deploys and failures. |

Connections: `WEB_CONCURRENCY x MONGO_MAX_POOL_SIZE x VMs` must stay well under the Atlas tier's limit
(M10: 1500, M30: 3000); 3 VMs x 4 workers x 25 = 300.

**Capacity log**

| Date | Commit | Server | Run | Bills/s reached | p95 POST /invoices | Errors | Notes |
|---|---|---|---|---|---|---|---|
| 2026-11-02 | prod-ready + WP-11 | laptop, 1 uvicorn process | locust smoke, 2 bills/s target | 1.84 | 520 ms | 0% | proves the scripts only; not a capacity number |
