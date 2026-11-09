"""WP-11 CI/CD and deploy: the workflows are well formed and pinned, and deploy/deploy.sh swaps, waits for /readyz
and rolls back by itself. The deploy script runs for real against fake `docker`, `curl` and `git` commands (no
Docker needed), so these tests run in CI and on a laptop with Git Bash."""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from common import IST

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = ROOT / "deploy"


def _bash() -> str | None:
    found = shutil.which("bash")
    if not found or (sys.platform == "win32" and "system32" in found.lower()):  # WSL's launcher, not a shell
        return None
    return found


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash (Git Bash on Windows)")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


# --- workflows -------------------------------------------------------------------------------------------------

def test_every_action_is_pinned_by_commit_sha():
    uses = []
    for f in WORKFLOWS.glob("*.yml"):
        uses += [(f.name, u) for u in re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", f.read_text(encoding="utf-8"), re.M)]
    assert uses
    loose = [(f, u) for f, u in uses if not (u.startswith("./") or re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u))]
    assert loose == [], f"pin these by commit SHA: {loose}"


def test_ci_runs_every_check_and_one_result_gates_the_merge():
    ci = _workflow("ci.yml")
    on = ci[True]  # PyYAML reads the key `on` as True
    assert {"pull_request", "push", "workflow_call"} <= set(on)
    jobs = ci["jobs"]
    assert {"backend", "audit", "image", "secrets", "app", "result"} <= set(jobs)
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    for check in ("ruff check", "python -m pytest", "replSet rs0", "pip-audit", "trivy-action", "gitleaks",
                  "tsc --noEmit", "eslint app src", "i18n:check", "expo-doctor"):
        assert check in text, check
    assert set(jobs["result"]["needs"]) == {"backend", "audit", "image", "secrets", "app"}
    assert jobs["result"]["if"] == "always()"


def test_deploy_tests_first_and_production_waits_for_a_reviewer():
    wf = _workflow("deploy.yml")
    jobs = wf["jobs"]
    assert jobs["test"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["test"]["with"]["scope"] == "backend-tests"
    deploy = jobs["deploy"]
    assert set(deploy["needs"]) == {"test", "image"}
    assert "needs.test.result == 'success'" in deploy["if"]
    assert "'production'" in deploy["environment"]["name"] and "'staging'" in deploy["environment"]["name"]
    assert deploy["concurrency"]["cancel-in-progress"] is False
    # the VM's installed forced command only accepts "staging" / "prod": the workflow must send exactly that
    assert "TARGET: ${{ github.ref_name == 'main' && 'prod' || 'staging' }}" in (WORKFLOWS / "deploy.yml").read_text()
    assert wf[True]["push"]["branches"] == ["staging", "main"]


def test_dependabot_covers_every_ecosystem():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert {u["package-ecosystem"] for u in cfg["updates"]} == {"pip", "npm", "github-actions", "docker"}


def test_no_hard_coded_app_key_left_in_docs():
    for f in (ROOT / "README.md", DEPLOY / "README.md", DEPLOY / ".env.example", ROOT / "frontend" / ".env.example"):
        text = f.read_text(encoding="utf-8")
        for stale in ("X-App-Key", "APP_API_KEY", "EXPO_PUBLIC_APP_KEY"):  # the app-key check was removed
            assert stale not in text, (f, stale)


# --- shell scripts ---------------------------------------------------------------------------------------------

@needs_bash
@pytest.mark.parametrize("script", ["deploy.sh", "ci-deploy", "backup.sh", "restore-drill.sh", "demo-refresh.sh"])
def test_scripts_parse(script):
    r = subprocess.run([BASH, "-n", str(DEPLOY / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_bash
def test_ci_deploy_runs_only_the_known_commands(tmp_path):
    for target, folder in (("staging", "Plant_Tech-staging"), ("prod", "Plant_Tech")):
        script = tmp_path / folder / "deploy" / "deploy.sh"
        script.parent.mkdir(parents=True)
        script.write_text('#!/usr/bin/env bash\necho "deploy.sh $*"\n', newline="\n")
        script.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path)}
    for command, expect in (("staging", "deploy.sh staging"), ("prod", "deploy.sh prod"),
                            ("staging rollback", "deploy.sh staging rollback")):
        r = subprocess.run([BASH, str(DEPLOY / "ci-deploy")], env={**env, "SSH_ORIGINAL_COMMAND": command},
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.strip() == expect, (command, r.stdout, r.stderr)
    for command in ("", "bash", "staging; rm -rf ~", "prod rollback 1234", "staging\nprod"):
        r = subprocess.run([BASH, str(DEPLOY / "ci-deploy")], env={**env, "SSH_ORIGINAL_COMMAND": command},
                           capture_output=True, text=True)
        assert r.returncode == 1 and "refused" in r.stderr, command


# Fake docker: images are files in $FAKE/tags (name -> image id); "compose up" makes the web container run
# whatever :latest points at; "compose run ... migrate" exits with $MIGRATE_RC (the rollups rebuild with
# $ROLLUPS_RC); "docker run ... test IMAGE -f FILE" fails for the old image's files listed in $OLD_LACKS;
# every call is logged.
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE/calls"
T="$FAKE/tags"; mkdir -p "$T"
key() { echo "$1" | tr '/:' '__'; }
resolve() {  # an image ID listed in $MISSING_IDS is gone from the image list (its container still runs)
  case "$1" in
    sha256:*) case " ${MISSING_IDS:-} " in *" $1 "*) ;; *) echo "$1" ;; esac ;;
    *) cat "$T/$(key "$1")" 2>/dev/null ;;
  esac
}
if [ "$1" = compose ]; then
  shift
  case "$1" in
    build) echo sha256:new > "$T/$(key "$PROJECT-api:latest")" ;;
    config) printf 'mongo\nredis\ndata-owner\nmigrate\nweb\nworker\n' ;;
    ps) [ -f "$FAKE/running" ] && echo cid-web ;;
    run) case "$*" in *settings*) exit "${SETTINGS_RC:-0}" ;; *rollups*) exit "${ROLLUPS_RC:-0}" ;;
           *) exit "${MIGRATE_RC:-0}" ;; esac ;;
    up) case "$*" in *mongo*) exit 0 ;; esac; resolve "$PROJECT-api:latest" > "$FAKE/running" ;;
  esac
  exit 0
fi
case "$1" in
  run) img=$(resolve "${@: -3:1}"); file="${@: -1}"
       [ "$img" = sha256:old ] && case " ${OLD_LACKS:-} " in *" $file "*) exit 1 ;; esac
       exit 0 ;;
  inspect) cat "$FAKE/running" ;;
  tag) id=$(resolve "$2"); [ -n "$id" ] || exit 1; echo "$id" > "$T/$(key "$3")" ;;
  commit) [ "${COMMIT_RC:-0}" = 0 ] || exit 1; cat "$FAKE/running" > "$T/$(key "$3")" ;;  # same files as it runs
  image)
    case "$2" in
      inspect) id=$(resolve "${@: -1}"); [ -n "$id" ] || exit 1; case "$*" in *'{{.Id}}'*) echo "$id" ;; esac ;;
    esac ;;
  pull) [ "${PULL_RC:-0}" = 0 ] || exit 1
        case " ${PULL_MISSING:-} " in *" ${@: -1} "*) exit 1 ;; esac  # tags the registry does not have
        echo sha256:new > "$T/$(key "${@: -1}")" ;;
esac
exit 0
"""

# Fake curl: /readyz is 200 when the running image is a healthy one ($HEALTHY lists the ids), else 503.
FAKE_CURL = r"""#!/usr/bin/env bash
running=$(cat "$FAKE/running" 2>/dev/null)
case " $HEALTHY " in *" $running "*) printf 200 ;; *) printf 503 ;; esac
"""

FAKE_GIT = r"""#!/usr/bin/env bash
case "$*" in
  *HEAD:backend*) echo 2222222222222222222222222222222222222222 ;;  # the tree hash of backend/
  *rev-parse*) echo 1111111111111111111111111111111111111111 ;;
  *log*) echo "1111111 new version" ;;
esac
"""
SWAP = "docker compose up -d --no-build --no-deps --remove-orphans web worker"
GHCR = "ghcr.io/acme/hara-hisaab-api"


@pytest.fixture
def vm(tmp_path):
    """A pretend VM: the checkout, a fake docker/curl/git/sleep on PATH, the old version running and healthy."""
    if BASH is None:
        pytest.skip("needs bash (Git Bash on Windows)")
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    shutil.copy(DEPLOY / "deploy.sh", checkout / "deploy" / "deploy.sh")
    (checkout / "deploy" / ".env").write_text("PROJECT_NAME=hh\nHOST_PORT=8130\n", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("git", FAKE_GIT),
                       ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body, newline="\n")
        (bin_dir / name).chmod(0o755)
    fake = tmp_path / "fake"
    (fake / "tags").mkdir(parents=True)
    (fake / "running").write_text("sha256:old\n", newline="\n")
    (fake / "tags" / "hh-api_latest").write_text("sha256:old\n", newline="\n")

    def run(*args, **env):
        e = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE": str(fake), "PROJECT": "hh",
             "NURSERYPOS_DIR": str(checkout), "NURSERYPOS_STATE": str(tmp_path / "state"), "NURSERYPOS_SYNCED": "1",
             "READY_TIMEOUT": "6", "READY_STREAK": "2", "HEALTHY": "sha256:old sha256:new", **env}
        r = subprocess.run([BASH, str(checkout / "deploy" / "deploy.sh"), *args], env=e, capture_output=True,
                           text=True, timeout=60)
        calls = (fake / "calls").read_text().splitlines() if (fake / "calls").exists() else []
        return r, calls, (fake / "running").read_text().strip()

    run.state = tmp_path / "state" / "staging"
    run.tags = fake / "tags"
    run.env_file = checkout / "deploy" / ".env"
    return run


def test_deploy_migrates_before_the_swap_and_keeps_the_new_version(vm):
    r, calls, running = vm("staging")
    assert r.returncode == 0, r.stdout + r.stderr
    assert running == "sha256:new"
    migrate = calls.index("docker compose run --rm --no-TTY migrate")
    swap = calls.index(SWAP)  # web and worker only: migrate and data-owner do not run a second time
    assert calls.index("docker compose build web") < migrate < swap
    assert not any(c.startswith("docker compose up") and "--no-deps" not in c and "mongo" not in c for c in calls)
    assert "docker tag sha256:old hh-api:rollback" in calls  # what ran before is kept for a rollback
    assert "docker tag hh-api:latest hh-api:1111111111111111111111111111111111111111" in calls
    assert (vm.state / "good").read_text().strip() == "1111111111111111111111111111111111111111"


def test_a_broken_image_is_rolled_back_by_itself(vm):
    r, calls, running = vm("staging", HEALTHY="sha256:old")  # the new image never becomes ready
    assert r.returncode == 1
    assert running == "sha256:old"
    assert "docker compose up -d --no-build --no-deps web worker" in calls  # the old one back, no migration run
    assert "rolled back" in r.stdout
    assert not (vm.state / "good").exists()
    assert (vm.tags / "hh-api_latest").read_text().strip() == "sha256:old"


def test_failed_migrations_never_swap(vm):
    r, calls, running = vm("staging", MIGRATE_RC="1")
    assert r.returncode == 1
    assert running == "sha256:old"
    assert SWAP not in calls
    assert (vm.tags / "hh-api_latest").read_text().strip() == "sha256:old"  # a restart starts the old image too
    assert "FAILED: migrations" in (vm.state / "deploy.log").read_text()


def test_bad_settings_stop_before_the_migrations(vm):
    r, calls, running = vm("staging", SETTINGS_RC="2")
    assert r.returncode == 1 and running == "sha256:old"
    assert "docker compose run --rm --no-TTY migrate" not in calls


def _ghcr(tmp_path):
    env_file = tmp_path / "checkout" / "deploy" / ".env"
    env_file.write_text(env_file.read_text() + f"IMAGE_SOURCE=ghcr\nGHCR_IMAGE={GHCR}\n", newline="\n")


def test_prebuilt_image_from_ghcr(vm, tmp_path):
    """Found by the tree hash of backend/ (what it was built from), so a newer docs-only commit still finds it."""
    _ghcr(tmp_path)
    r, calls, running = vm("staging")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"docker pull --quiet {GHCR}:tree-2222222222222222222222222222222222222222" in calls
    assert f"docker pull --quiet {GHCR}:1111111111111111111111111111111111111111" not in calls
    assert "docker compose build web" not in calls
    assert running == "sha256:new"


def test_ghcr_image_by_commit_when_it_has_no_tree_tag(vm, tmp_path):
    """Images built before the tree tags existed are found by their commit."""
    _ghcr(tmp_path)
    r, calls, running = vm("staging", PULL_MISSING=f"{GHCR}:tree-2222222222222222222222222222222222222222")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"docker pull --quiet {GHCR}:1111111111111111111111111111111111111111" in calls
    assert running == "sha256:new"


def test_no_ghcr_image_yet_changes_nothing(vm, tmp_path):
    _ghcr(tmp_path)
    r, calls, running = vm("staging", PULL_MISSING=f"{GHCR}:tree-2222222222222222222222222222222222222222 "
                                                   f"{GHCR}:1111111111111111111111111111111111111111")
    assert r.returncode == 1 and running == "sha256:old"
    assert SWAP not in calls and "docker compose run --rm --no-TTY migrate" not in calls
    assert "no image for 1111111111111111111111111111111111111111" in (vm.state / "deploy.log").read_text()
    assert (vm.tags / "hh-api_latest").read_text().strip() == "sha256:old"


def test_manual_rollback(vm):
    assert vm("staging")[0].returncode == 0
    r, calls, running = vm("staging", "rollback")
    assert r.returncode == 0, r.stdout + r.stderr
    assert running == "sha256:old"


def _rebuilds(calls):
    return [c for c in calls if "rollups.py rebuild" in c]


def test_bills_of_a_version_without_rollups_are_rebuilt_after_the_swap(vm):
    """Review fix: the old version bills while the migrations run (after 0202 filled the rollups) and never updates
    them, so after the swap the days it may have changed are rebuilt; a version with rollups needs nothing."""
    r, calls, running = vm("staging", OLD_LACKS="rollups.py")
    assert r.returncode == 0, r.stdout + r.stderr
    today = datetime.now(IST).date().isoformat()
    assert _rebuilds(calls) == [f"docker compose run --rm --no-TTY --no-deps migrate python rollups.py rebuild "
                                f"--touched-since {today}"]
    assert calls.index(SWAP) < calls.index(_rebuilds(calls)[0])
    assert not (vm.state / "rollups-stale-since").exists()
    before = len(calls)
    r, calls, _ = vm("staging")  # now the running version keeps them itself (the call log goes on)
    assert r.returncode == 0 and _rebuilds(calls[before:]) == []


def test_a_failed_rollups_rebuild_keeps_the_new_version_and_is_tried_again(vm):
    r, calls, running = vm("staging", OLD_LACKS="rollups.py", ROLLUPS_RC="1")
    assert r.returncode == 1 and running == "sha256:new"
    assert (vm.state / "good").exists() and (vm.state / "rollups-stale-since").exists()
    assert "REBUILDING THE DAILY ROLLUPS FAILED" in (vm.state / "deploy.log").read_text()
    before = len(calls)
    r, calls, _ = vm("staging")
    assert r.returncode == 0 and len(_rebuilds(calls[before:])) == 1
    assert not (vm.state / "rollups-stale-since").exists()


def test_a_rollback_to_an_image_before_the_worker_and_rollups(vm):
    """Review fix: the old image has no worker.py (its worker would crash-loop) and no rollups (its bills are
    rebuilt after the next deploy)."""
    r, calls, running = vm("staging", HEALTHY="sha256:old", OLD_LACKS="rollups.py worker.py")
    assert r.returncode == 1 and running == "sha256:old"
    assert "docker compose rm --stop --force worker" in calls
    assert "docker compose up -d --no-build --no-deps web" in calls
    assert "docker compose up -d --no-build --no-deps web worker" not in calls
    assert (vm.state / "rollups-stale-since").exists() and _rebuilds(calls) == []
    before = len(calls)
    r, calls, running = vm("staging")  # fixed and deployed again: the days the old version billed are rebuilt
    assert r.returncode == 0 and running == "sha256:new" and len(_rebuilds(calls[before:])) == 1


def test_unknown_target_or_action():
    if BASH is None:
        pytest.skip("needs bash")
    for args in (["qa"], ["staging", "nuke"], []):
        r = subprocess.run([BASH, str(DEPLOY / "deploy.sh"), *args], capture_output=True, text=True)
        assert r.returncode == 2 and "usage" in r.stderr


def test_a_running_image_missing_from_the_list_is_kept_by_saving_the_container(vm):
    """8 Nov on QA: the running container's image was no longer listed ("No such image"), so tagging it as the
    rollback image failed and the deploy stopped. A copy of the running container is the rollback image now."""
    r, calls, running = vm("staging", MISSING_IDS="sha256:old")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker commit cid-web hh-api:rollback" in calls and running == "sha256:new"
    assert "no longer listed" in (vm.state / "deploy.log").read_text()


def test_a_saved_container_is_what_a_failed_deploy_goes_back_to(vm):
    r, calls, running = vm("staging", MISSING_IDS="sha256:old", HEALTHY="sha256:old")  # the new one never gets ready
    assert r.returncode == 1 and running == "sha256:old"
    assert "docker tag hh-api:rollback hh-api:latest" in calls and "rolled back" in r.stdout


def test_no_rollback_image_at_all(vm, tmp_path):
    """The running image is gone and the container cannot be copied either (its image files were pruned, as on QA
    on 8 Nov): production stops before any change; staging goes on without an automatic rollback, says so, and
    still rebuilds the days the old version billed."""
    r, calls, running = vm("prod", MISSING_IDS="sha256:old", COMMIT_RC="1")
    assert r.returncode == 1 and running == "sha256:old"
    assert "docker compose build web" not in calls and SWAP not in calls
    log = (vm.state.parent / "prod" / "deploy.log").read_text()
    assert "could not keep a rollback image; nothing was changed" in log and "ALLOW_NO_ROLLBACK=1" in log

    r, calls, running = vm("staging", MISSING_IDS="sha256:old", COMMIT_RC="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert running == "sha256:new" and SWAP in calls
    log = (vm.state / "deploy.log").read_text()
    assert "WARNING: no rollback image could be kept" in log and len(_rebuilds(calls)) == 1


def test_production_without_users_may_deploy_without_a_rollback_image(vm):
    """The first production deploy of this version (no users yet): ALLOW_NO_ROLLBACK=1 lets it go on like staging."""
    with vm.env_file.open("a", newline="\n") as f:
        f.write("ALLOW_NO_ROLLBACK=1\n")
    r, calls, running = vm("prod", MISSING_IDS="sha256:old", COMMIT_RC="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert running == "sha256:new" and SWAP in calls
    assert "WARNING: no rollback image could be kept" in (vm.state.parent / "prod" / "deploy.log").read_text()


def test_production_settings_are_made_from_qas(tmp_path):
    """deploy/prod-env-from-qa.sh: QA's keys and limits, production's own name, port, folder, address and login
    secret, QA-only lines left out, the old file kept; and the result passes production's settings check."""
    if BASH is None:
        pytest.skip("needs bash")
    qa, prod = tmp_path / "qa.env", tmp_path / "prod" / ".env"
    prod.parent.mkdir()
    # no ENV line, as on the VM (compose.yaml defaults it to staging)
    qa.write_text("PROJECT_NAME=nurserypos-staging\nHOST_PORT=8130\nSTORAGE_PREFIX=nurserypos-staging\n"
                  "CORS_ORIGINS=*\nJWT_SECRET=qa-secret-qa-secret-qa-secret-qa-secret\nHEARTBEAT_URL=https://hb/qa\n"
                  "BACKUP_RCLONE_REMOTE=r2:backups/staging\nANTHROPIC_API_KEY=sk-qa\nSTORAGE_BACKEND=r2\n"
                  "R2_ACCOUNT_ID=acct\nR2_ACCESS_KEY_ID=key\nR2_SECRET_ACCESS_KEY=secret\nR2_BUCKET=photos\n"
                  "DB_NAME=nurserypos\nAI_DAILY_LIMIT=300\n", newline="\n")
    prod.write_text("APP_API_KEY=old\nPROJECT_NAME=nurserypos\n", newline="\n")
    script = DEPLOY / "prod-env-from-qa.sh"
    env = {**os.environ, "QA_ENV": str(qa), "PROD_ENV": str(prod)}
    r = subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    made = dict(line.split("=", 1) for line in prod.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert made["ENV"] == "production" and made["PROJECT_NAME"] == "nurserypos" and made["HOST_PORT"] == "8120"
    assert made["STORAGE_PREFIX"] == "nurserypos" and made["CORS_ORIGINS"] == "" and made["ALLOW_NO_ROLLBACK"] == "1"
    assert made["PUBLIC_BASE_URL"] == "https://nurserypos.140-238-251-141.sslip.io"
    assert len(made["JWT_SECRET"]) == 64 and made["JWT_SECRET"] != "qa-secret-qa-secret-qa-secret-qa-secret"
    assert made["ANTHROPIC_API_KEY"] == "sk-qa" and made["R2_BUCKET"] == "photos" and made["AI_DAILY_LIMIT"] == "300"
    assert not {"HEARTBEAT_URL", "BACKUP_RCLONE_REMOTE", "APP_API_KEY"} & made.keys()
    assert "sk-qa" not in r.stdout and made["JWT_SECRET"] not in r.stdout  # no value printed
    assert [p.name for p in prod.parent.glob(".env.before-*")] and (prod.stat().st_mode & 0o777) == 0o600
    check = subprocess.run([sys.executable, "-c", "import settings; settings.check()"], cwd=ROOT / "backend", timeout=60,
                           env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", ""), **made},
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    again = subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert again.returncode == 1 and "already says ENV=production" in again.stderr
    qa.write_text(qa.read_text().replace("STORAGE_PREFIX=nurserypos-staging\n", ""), newline="\n")
    # production's own Claude key and bucket, already in its file, are kept with --keep-own-keys
    with prod.open("a", newline="\n") as f:
        f.write("ANTHROPIC_API_KEY=sk-prod\nR2_BUCKET=hara-hisaab-prod\nR2_ACCESS_KEY_ID=pk\nR2_SECRET_ACCESS_KEY=ps\n"
                "R2_PUBLIC_BUCKET=\n")
    r = subprocess.run([BASH, str(script), "--again", "--keep-own-keys"], env=env, capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    made = dict(line.split("=", 1) for line in prod.read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert (made["ANTHROPIC_API_KEY"], made["R2_BUCKET"], made["R2_ACCESS_KEY_ID"], made["R2_SECRET_ACCESS_KEY"]) == \
        ("sk-prod", "hara-hisaab-prod", "pk", "ps")
    assert made["R2_ACCOUNT_ID"] == "acct" and made["ENV"] == "production"  # not in production's file: QA's
    assert prod.read_text().count("ANTHROPIC_API_KEY=") == 1 and "sk-prod" not in r.stdout
    assert "kept from production's previous file: ANTHROPIC_API_KEY R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET" \
        in r.stdout  # R2_ACCOUNT_ID was in production's file (made from QA's earlier); empty R2_PUBLIC_BUCKET is not kept
    not_qa = tmp_path / "not-qa.env"
    not_qa.write_text("ENV=production\n", newline="\n")
    r = subprocess.run([BASH, str(script), "--again"], env={**env, "QA_ENV": str(not_qa)}, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 1 and "not staging" in r.stderr
    r = subprocess.run([BASH, str(script), "--again"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "STORAGE_PREFIX=nurserypos-prod" in prod.read_text()  # QA keeps the default folder
