# NurseryPOS

A mobile point-of-sale app for plant nurseries. Staff photograph a plant, AI (Claude) names it and finds it in stock, it goes into the bill, and the customer pays (Cash / UPI / Card) and gets an invoice. Stock updates on its own. Supplier bills can be photographed too, and their items are added to stock.

| Part | Tech | Folder |
|---|---|---|
| App (Android, later iOS) | Expo / React Native, Expo Router, React Query | `frontend/` |
| API | FastAPI, MongoDB (Motor), Claude via the Anthropic SDK | `backend/` |
| Photos | Cloudflare R2 (or local disk) | `backend/storage_client.py` |
| Hosting | Docker on the shared Oracle VM, behind its Caddy | `deploy/` |

## Environments

| | Branch | API | App |
|---|---|---|---|
| Production | `main` | https://nurserypos.140-238-251-141.sslip.io | "NurseryPOS" (`eas build --profile prod-apk`) |
| Staging / QA | `staging` | https://nurserypos-staging.140-238-251-141.sslip.io | "NurseryPOS QA" (`eas build --profile staging`) |

Every push and pull request runs the checks in `.github/workflows/ci.yml` (lint, tests on a real MongoDB replica set, dependency and image audits, secret scan, app checks). Pushing to `staging` or `main` then deploys automatically (`.github/workflows/deploy.yml`; `main` waits for an approval). Server details are in [deploy/README.md](deploy/README.md), operations (deploy, rollback, restore, incidents, secrets, scaling) in [docs/RUNBOOK.md](docs/RUNBOOK.md), load tests in [loadtest/README.md](loadtest/README.md), and the manual test script is [docs/QA-CHECKLIST.md](docs/QA-CHECKLIST.md).

## Run locally

**Backend tests** (Claude is faked; MongoDB is a real single-node replica set, as in CI):
```bash
docker run -d --name hh-mongo -p 127.0.0.1:27117:27117 mongo:7.0 --replSet rs0 --bind_ip_all --port 27117
docker exec hh-mongo mongosh --port 27117 --quiet --eval "rs.initiate({_id: 'rs0', members: [{_id: 0, host: '127.0.0.1:27117'}]})"
cd backend
pip install --require-hashes --no-deps -r requirements-dev.lock
ruff check . && python -m pytest       # MONGO_URL defaults to mongodb://127.0.0.1:27117/?replicaSet=rs0
```

**App in a browser** (quick UI check; the camera works only on a phone):
```bash
cd frontend
yarn install
npx expo start --web
```
Before running, create `frontend/.env` from `frontend/.env.example` and point it at the staging API.

## Configuration
- **Server:** `deploy/.env` on the VM. See `deploy/.env.example`. Never commit it.
- **App:** `EXPO_PUBLIC_BACKEND_URL` comes from `frontend/eas.json` (per profile). There is no app key: every API call is checked by the login token.
- **AI model:** `CLAUDE_MODEL` (default `claude-sonnet-5-5`).
