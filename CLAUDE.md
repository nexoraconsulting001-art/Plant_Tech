# Hara Hisaab (Plant_Tech): instructions for Claude sessions

Read `docs/SESSION-NOTES.md` first: it says where the work stands, what is waiting on the owner and how to test.
`docs/PRODUCTION-PLAN.md` is the production and scale plan (status from line ~573, owner decisions D1–D17 from
line ~544); `docs/ROADMAP.md` is the order of work (open issues, then iOS, then production and scale).

## Branches
- `prod-ready`: the working branch. It holds everything: the testers' round (`staging`), the 14 production work
  packages (merged `pkg/wp*` branches), the review fixes and the load-test fixes. Work here unless told otherwise.
- `staging`: pushing it deploys the backend to QA (https://nurserypos-staging.140-238-251-141.sslip.io) through
  `.github/workflows/deploy.yml`. Push it only when the owner asks; bringing `prod-ready` into `staging` is an
  owner decision.
- `main`: production. Released on 9 Nov 2026 (aad471f, the same server code as `staging`); production's data was
  wiped (copy kept on the VM) and gets QA's data on the pilots' move evening (`docs/PROD-RELEASE.md`, step D).
  Pushing it deploys production (after the owner's approval in GitHub): push it only when the owner asks for a
  production release, never on your own.
- GitHub's default branch is `main`; a new session still works on `prod-ready`.
- Remote: `github.com/nexoraconsulting001-art/Plant_Tech`. The old `udyanojha/Nzone` remote is never pushed to.

## Standing rules from the owner
- **No app builds and no over-the-air updates** (EAS build or `eas update`, any profile or channel) until the owner
  has tested the change in the browser and said go for that build. An earlier "build it after X" is not a go.
- **Commit or push only when the owner asks.** Never push `main`.
- **No secrets in the repo.** The owner enters keys in the VM's `deploy/.env` and in EAS themselves.
- **No permanent deletes of data by Claude.** The owner runs wipes themselves.
- **Test logins only against a local API** (in-memory dev server), never typed into a browser pointed at staging.
- **Demo shop:** owner 9654346434, "Green House" on staging; its customers' phone numbers are dummies (never
  WhatsApp them) and no destructive tests on it.
- **Design:** classy, not cheap. No emoji, no "…" cut-off text, quiet branding. Every new text needs its Hindi
  translation (`npm run i18n:check` in `frontend/`).
- **Plan first** for big changes, then build.

## Shared VM (deploy target)
All of the owner's apps run on one shared Oracle VM (140.238.251.141, Arm64, 6 GB) behind a systemd Caddy that
owns ports 80/443. Each app binds `127.0.0.1:<port>` only. Never start another Caddy, never touch other sites,
never `docker compose down -v`. Ports taken: 8000, 8080, 8100, 8110, 8120 (Hara Hisaab prod), 8130 (Hara Hisaab
staging); check `ss -ltn` before picking a new one. Deploys go through GitHub Actions on push; a cloud session
has no SSH access to the VM.

## Checks before a commit
- **Backend** (`backend/`): needs MongoDB 7 as a replica set (transactions). As in CI:
  `docker run -d --name mongo -p 127.0.0.1:27017:27017 mongo:7.0 --replSet rs0 --bind_ip_all`, then
  `rs.initiate({_id: 'rs0', members: [{_id: 0, host: '127.0.0.1:27017'}]})` in mongosh, then
  `pip install --require-hashes --no-deps -r requirements-dev.lock`, `ruff check .` and
  `MONGO_URL='mongodb://127.0.0.1:27017/?replicaSet=rs0' python -m pytest -q` (465 tests on 8 Nov 2026).
- **App** (`frontend/`): `npx tsc --noEmit`, `npx eslint app src` (0 errors), `npm run i18n:check`, and
  `node --test --experimental-transform-types tests/sync-policy.test.mjs tests/cart-sync.test.mjs scripts/test-paging.mjs`.
  The package manager is yarn 1 (`npx yarn@1.22.22 add …`).
