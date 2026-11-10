# Deploying NurseryPOS on the shared Oracle VM

NurseryPOS runs on the **shared** Oracle VM (`svc-agent`, `140.238.251.141`). Other live sites run there too. The VM's general rules and setup are in `DEPLOYMENT-GUIDE.md`. This file only covers this app; day-to-day operations (deploy, rollback, restore, incidents, rotating secrets, scaling) are in [docs/RUNBOOK.md](../docs/RUNBOOK.md).

| | Production | Staging (QA) |
|---|---|---|
| Branch | `main` | `staging` |
| URL | **https://nurserypos.140-238-251-141.sslip.io** | **https://nurserypos-staging.140-238-251-141.sslip.io** |
| Port (127.0.0.1 only) | 8120 | 8130 |
| Folder on the VM | `~/Plant_Tech` | `~/Plant_Tech-staging` |
| Compose project / volumes | `nurserypos` (`nurserypos_mongo`, `nurserypos_data`) | `nurserypos-staging` (its own database) |
| Photos (R2 bucket `nurserypos-photos`) | folder `nurserypos/` | folder `nurserypos-staging/` |
| Secrets | `~/Plant_Tech/deploy/.env` | `~/Plant_Tech-staging/deploy/.env` (plus `PROJECT_NAME`, `HOST_PORT`, `STORAGE_PREFIX`) |
| GitHub Environment | `production` (a required reviewer approves each deploy) | `staging` |
| App | "NurseryPOS" (`eas build --profile prod-apk`) | "NurseryPOS QA" (`eas build --profile staging`) |

Health: `curl 127.0.0.1:8120/readyz` (or `8130`) returns `{"ok":true,"checks":{"mongo":"ok","storage":"ok"}}`; `/health` still answers `{"ok":true}` for old monitors.

## Day-to-day: how code reaches the server
1. Open a pull request (or push) to `staging`. CI (`.github/workflows/ci.yml`) runs ruff, the tests on a real MongoDB replica set, pip-audit, a Trivy scan of the image, gitleaks and the app checks. The "CI result" check must be green to merge.
2. On a push to `staging`, `.github/workflows/deploy.yml` runs the backend tests again and SSHes in with the restricted deploy key, which runs `deploy/deploy.sh staging`, then checks the public `/health` and `/readyz`.
3. Test in the "NurseryPOS QA" app with `docs/QA-CHECKLIST.md`.
4. Merge `staging` into `main` and push. The same pipeline builds the arm64 image into GHCR and waits in the `production` environment until the reviewer approves, then deploys.

Only changes under `backend/`, `deploy/` or the workflows trigger a deploy. App (screen) changes need a new APK instead.

**What `deploy.sh` does** (by hand: `~/Plant_Tech/deploy/deploy.sh prod` or `~/Plant_Tech-staging/deploy/deploy.sh staging`):
1. Syncs the folder to its branch and re-runs the new copy of itself.
2. Builds the image on the VM (default), or pulls `GHCR_IMAGE:<commit>` when `.env` has `IMAGE_SOURCE=ghcr`.
3. Checks the settings (`python -m settings`) and runs the migrations (`python -m migrations up`) in one-off containers, while the old version keeps serving. A failure here stops the deploy with nothing swapped.
4. Swaps `web` and `worker`, then waits up to 90 s for `/readyz` to answer 200 three times in a row.
5. If the new version is not ready, it puts the previous image back by itself, waits until that is ready, and exits 1 (the Actions run goes red).

Rollback by hand: `deploy/deploy.sh staging rollback` (the image that ran before the last deploy) or `deploy/deploy.sh staging rollback <commit sha>` (any of the last four). The log of every deploy is in `~/.nurserypos/<staging|prod>/deploy.log`.

**The deploy key:**
- GitHub secret `NURSERYPOS_DEPLOY_KEY`; the private half is `~/.ssh/nurserypos_deploy` on Shashank's PC.
- It's locked in the VM's `~/.ssh/authorized_keys` to `~/bin/nurserypos-ci-deploy` (a copy of `deploy/ci-deploy`). It can only ask for `staging` or `prod` (and `staging rollback` / `prod rollback` once the new copy is installed), and gets no shell.
- After changing `deploy/ci-deploy`, install it again: `install -m 755 ~/Plant_Tech/deploy/ci-deploy ~/bin/nurserypos-ci-deploy`. The workflow only sends `staging` or `prod`, so the old copy keeps working until then.
- To revoke it, delete that line from `authorized_keys`.

## First deploy
Log in from Windows PowerShell. Disconnect the Voltas VPN first:
```powershell
ssh -i "$env:USERPROFILE\.ssh\oracle-svc-agent.key" ubuntu@140.238.251.141
```

Then, on the VM:
```bash
free -h                                     # check there's room; Mongo + Redis + API + worker need about 1.5 GB
git clone https://github.com/nexoraconsulting001-art/Plant_Tech.git ~/Plant_Tech
cd ~/Plant_Tech/deploy
cp .env.example .env
openssl rand -hex 32                        # copy this: it's your JWT_SECRET
nano .env                                   # fill in ANTHROPIC_API_KEY, JWT_SECRET, R2_*, ENV (never commit)

./deploy.sh prod                            # build, migrate, start, wait for /readyz
docker compose ps                           # mongo, redis, web, worker: running (healthy)
curl -s 127.0.0.1:8120/readyz               # {"ok":true,...}

sudo bash ~/Service-Call-Bot/infra/oracle-vm/add-site.sh nurserypos proxy 8120
```
Then put the Caddy settings in place (compression, body limits, security headers, health checks, log redaction): see the top of `deploy/Caddyfile.snippet`.

Check it from outside:
- `https://nurserypos.140-238-251-141.sslip.io/api/` should return `{"message":"NurseryPOS API"}`, and `/api/products` without a login `401`.
- The other sites must still work: `https://140-238-251-141.sslip.io/health`, `https://alerts.140-238-251-141.sslip.io`, `https://mehtamart.140-238-251-141.sslip.io` and `https://plantstore.140-238-251-141.sslip.io`.

## Logs
```bash
cd ~/Plant_Tech/deploy
docker compose logs -f web                  # one JSON line per request; "Claude ... in=… out=…" lines show tokens per scan
docker compose logs -f worker               # daily summary and other background jobs
docker compose restart web
```

**Never run `docker compose down -v`.** The `-v` flag deletes the database volume.

## Backups
`deploy/backup.sh` dumps MongoDB (one consistent point in time), encrypts it with age, copies it off the VM with rclone (R2 or B2) and pings a heartbeat. One-time setup and the monthly restore drill (`deploy/restore-drill.sh`) are in [docs/RUNBOOK.md](../docs/RUNBOOK.md#backups).
```bash
crontab -e   # add:  0 2 * * * /home/ubuntu/Plant_Tech/deploy/backup.sh >> /home/ubuntu/Plant_Tech/deploy/backup.log 2>&1
```
Until `BACKUP_AGE_RECIPIENTS_FILE` is set it still makes the plain local dump it always made (kept 14 days in `deploy/backups/`) and reports a failure, so the heartbeat stays red until backups leave the VM.

## Demo shop
A showcase shop with 25 plants, 15 other products, 45 customers and a month of orders (staging: Green House,
owner 9654346434). Every day at 5:30 am India time all its dates move forward one day, so "today" is always full
and nothing looks old. Seeding and removing touch only rows marked `demo`; the refresh moves every date in that
shop, so it refuses to run on a shop without demo data.
```bash
cd ~/Plant_Tech-staging/deploy
docker compose exec web python demo_data.py seed --phone <owner phone>    # once (--force starts again)
crontab -e   # add (the VM runs on UTC):  0 0 * * * bash /home/ubuntu/Plant_Tech-staging/deploy/demo-refresh.sh staging <owner phone> >> /home/ubuntu/nurserypos-demo.log 2>&1
#              production (only into a new, empty shop):  2 0 * * * bash /home/ubuntu/Plant_Tech/deploy/demo-refresh.sh prod <owner phone> >> /home/ubuntu/nurserypos-demo-prod.log 2>&1
docker compose exec web python demo_data.py remove --phone <owner phone>  # take the demo data out again
```

## Settings
All of them are in `deploy/.env.example` (and `backend/settings.py`). After changing `.env`, run `./deploy.sh <target>` (or `docker compose up -d` to restart with the same image).
- **Claude model:** `CLAUDE_MODEL` (default `claude-sonnet-5-5`); per kind of call `CLAUDE_MODEL_PLANT`, `CLAUDE_MODEL_INVOICE`, `CLAUDE_MODEL_NAMES`.
- **Photo storage:** `STORAGE_BACKEND=r2` (default; required in production) or `local` (the VM's disk, in the `data` volume).
