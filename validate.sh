#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
MODE="all"
QUIET=0

while (($#)); do
    case "$1" in
        --static) MODE="static"; shift ;;
        --runtime) MODE="runtime"; shift ;;
        --quiet) QUIET=1; shift ;;
        --help|-h)
            printf '%s\n' 'Usage: ./validate.sh [--static|--runtime] [--quiet]'
            exit 0
            ;;
        *) echo "Option inconnue: $1" >&2; exit 2 ;;
    esac
done

log() { [[ "$QUIET" == 1 ]] || printf '%s\n' "$*"; }

static_validation() {
    log "== Validation statique =="

    for file in \
        install.sh preflight.sh uninstall.sh validate.sh \
        compose/orchestra.yaml compose/orchestra.dev.yaml compose/images.lock.env \
        images/orchestra.Dockerfile images/orchestra.Dockerfile.dockerignore \
        images/orchestra-runtime.Dockerfile \
        images/orchestra-runtime.Dockerfile.dockerignore \
        images/orchestra-worker.Dockerfile \
        images/orchestra-worker.Dockerfile.dockerignore \
        config/host-packages.lock.toml \
        config/environments/default-worker.toml \
        scripts/platform-support.sh scripts/orchestra-compose.sh \
        scripts/orchestra-appliance.py scripts/orchestra-runtime-entrypoint.sh \
        scripts/sandbox_backend.py scripts/agent_runtime/contract.py \
        scripts/agent_runtime/hermes.py \
        scripts/orchestra-worker.py scripts/orchestra-reviewer.py \
        scripts/orchestra-orchestrator.py scripts/orchestra-supervisor.py \
        scripts/orchestra-recovery.py scripts/orchestra-notifier.py \
        scripts/orchestra-controller-api.py scripts/orchestra-console.py \
        scripts/orchestra-controller-probe.py scripts/orchestra-console-probe.py \
        scripts/orchestra-worker-entry.py scripts/orchestra-planner-entry.py \
        .github/workflows/publish-worker.yml \
        .github/workflows/accept-worker-publication.yml \
        tests/test-distribution-contract.sh \
        tests/test-install-platform-support.sh \
        tests/test-preflight-minimal-host.sh \
        tests/test-install-no-auth-contract.sh \
        tests/test-blueprint-v1.sh \
        tests/test-controller-blueprint-lifecycle.sh \
        tests/test-controller-contracts.sh \
        tests/test-controller-service-contract.sh
    do
        [[ -f "${REPO}/${file}" ]] || {
            echo "Fichier requis absent: ${file}" >&2
            return 1
        }
    done

    for retired in \
        VERSION \
        config/worker-sandbox.lock.toml \
        images/worker-sandbox.Dockerfile \
        scripts/export-worker-image.sh \
        scripts/legacy_worker_environment.py \
        tests/test-systemd-user-boot-order.sh \
        tests/test-controller-service-lifecycle.sh \
        tests/test-controller-service-persistence.sh
    do
        [[ ! -e "${REPO}/${retired}" ]] || {
            echo "Artefact retiré encore présent: ${retired}" >&2
            return 1
        }
    done
    if [[ -d "${REPO}/systemd/user" ]] && find "${REPO}/systemd/user" -maxdepth 1 -type f -name '*.service' -print -quit | grep -q .; then
        echo "Unités user-systemd applicatives encore présentes" >&2
        return 1
    fi

    while IFS= read -r -d '' script; do
        bash -n "$script"
    done < <(
        find "$REPO" -path "$REPO/.git" -prune -o \
            -type f -name '*.sh' -print0
    )

    python3 - "$REPO" <<'PY'
from pathlib import Path
import ast
import sys

root = Path(sys.argv[1])
for path in sorted(root.rglob("*.py")):
    if ".git" in path.parts:
        continue
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("Python AST: PASS")
PY

    "${REPO}/scripts/check-secrets.sh" --root "$REPO"

    python3 - "$REPO" <<'PY'
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
tracked = subprocess.run(
    ["git", "-C", str(root), "ls-files", "config/projects.d/*.toml"],
    check=True,
    text=True,
    stdout=subprocess.PIPE,
).stdout.splitlines()
if tracked:
    raise SystemExit(f"Active project configuration tracked: {tracked}")
for relative in (
    "tests/fixtures/projects/transaction-fixture.toml",
    "tests/fixtures/projects/transaction-fixture-b.toml",
):
    if not (root / relative).is_file():
        raise SystemExit(f"Test fixture missing: {relative}")

versions = [
    int(path.name.split("_", 1)[0])
    for path in sorted((root / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
]
if versions != list(range(1, len(versions) + 1)):
    raise SystemExit(f"Migration sequence invalid: {versions}")
print(f"Migration sequence: PASS ({len(versions)})")
PY

    PYTHONPATH="$REPO" python3 "${REPO}/tests/test_environment_resolution.py"
    PYTHONPATH="$REPO" python3 "${REPO}/tests/test_environment_resolution_guards.py"
    PYTHONPATH="$REPO" python3 "${REPO}/tests/test_sandbox_backend.py"
    PYTHONPATH="$REPO" python3 "${REPO}/tests/test_worker_distribution.py"
    PYTHONPATH="$REPO" python3 "${REPO}/tests/test_worker_publication.py"
    PYTHONPATH="$REPO" python3 "${REPO}/tests/test_trusted_worker_publisher.py"
    "${REPO}/tests/test-install-platform-support.sh"
    "${REPO}/tests/test-preflight-minimal-host.sh"
    "${REPO}/tests/test-install-no-auth-contract.sh"
    "${REPO}/tests/test-blueprint-v1.sh"
    "${REPO}/tests/test-controller-blueprint-lifecycle.sh"
    "${REPO}/tests/test-controller-contracts.sh"
    "${REPO}/tests/test-public-empty-registry.sh"
    "${REPO}/tests/test-release-documentation.sh"
    "${REPO}/tests/test-controller-service-contract.sh"
    "${REPO}/tests/test-distribution-contract.sh"
    python3 "${REPO}/scripts/orchestra-console-build.py" check \
        --source "${REPO}/console/src" \
        --expected "${REPO}/console/dist"

    local task_compose_tmp
    task_compose_tmp="$(mktemp -d)"
    docker compose --project-directory "$task_compose_tmp" \
        -f "${REPO}/compose/orchestra.yaml" \
        config --format json >"$task_compose_tmp/rendered.json"

    python3 - "$task_compose_tmp/rendered.json" <<'PY'
from pathlib import Path
import json
import sys

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {"orchestra", "orchestra-runtime"}
services = document.get("services", {})
if set(services) != expected:
    raise SystemExit(f"Rendered Compose service mismatch: {sorted(services)}")
privileged = [name for name, service in services.items() if service.get("privileged")]
if privileged != ["orchestra-runtime"]:
    raise SystemExit(f"Privileged service mismatch: {privileged}")
for name, service in services.items():
    for volume in service.get("volumes", []) or []:
        source = str(volume.get("source", ""))
        target = str(volume.get("target", ""))
        if source in {"/var/run/docker.sock", "/run/docker.sock"} or target in {
            "/var/run/docker.sock", "/run/docker.sock"
        }:
            raise SystemExit(f"Host Docker socket mounted by {name}")
print("Rendered Compose authority: PASS")
PY
    rm -rf -- "$task_compose_tmp"
    log "ORCHESTRA_STATIC_VALIDATION_PASS"
}

runtime_validation() {
    log "== Validation runtime =="
    [[ "$REPO" == "${ROOT}/repo" ]] || {
        echo "Lancer depuis ${ROOT}/repo." >&2
        return 1
    }

    local orchestra_uid orchestra_gid
    orchestra_uid="$(id -u)"
    orchestra_gid="$(id -g)"
    export ORCHESTRA_UID="$orchestra_uid" ORCHESTRA_GID="$orchestra_gid"

    "${REPO}/scripts/verify-layout.sh"
    "${REPO}/scripts/orchestra-db.py" integrity
    "${REPO}/scripts/orchestra-registry.py" validate
    "${REPO}/scripts/orchestra-roles.py" validate
    "${REPO}/scripts/orchestra-roles.py" verify-profiles
    "${REPO}/scripts/orchestra-compose.sh" config --quiet

    for container in orchestra orchestra-runtime
    do
        health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
        [[ "$health" == "healthy" || "$health" == "running" ]] || {
            echo "$container non sain: $health" >&2
            return 1
        }
    done

    curl --silent --show-error --fail --max-time 5 http://127.0.0.1:8080/ >/dev/null

    python3 - "$REPO" "$ROOT" <<'PY'
from pathlib import Path
import json
import subprocess
import sys
import tomllib

repo = Path(sys.argv[1])
root = Path(sys.argv[2])
with (repo / "config/environments/default-worker.toml").open("rb") as stream:
    environment = tomllib.load(stream)
reference = environment["image_reference"]
result = subprocess.run(
    [
        "docker",
        "--host",
        "unix:///run/orchestra-docker/docker.sock",
        "image",
        "inspect",
        reference,
    ],
    check=True,
    text=True,
    stdout=subprocess.PIPE,
)
payload = json.loads(result.stdout)
if len(payload) != 1 or reference not in payload[0].get("RepoDigests", []):
    raise SystemExit("Default worker exact RepoDigest is not materialized")
print("Default worker private-DIND materialization: PASS")
PY

    "${REPO}/scripts/orchestra-controller-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    "${REPO}/scripts/orchestra-console-probe.py" \
        --base-url http://127.0.0.1:8080 \
        --wait-seconds 10
    "${REPO}/scripts/orchestra-controller-session.py" check
    [[ "$(stat -c '%a' "${ROOT}/secrets/controller-session")" == "600" ]]
    [[ -f "${ROOT}/state/hermes-home/auth.json" ]]
    [[ "$(stat -c '%a' "${ROOT}/state/hermes-home/auth.json")" == "600" ]]
    [[ "$(stat -c '%a' "${ROOT}/secrets")" == "700" ]]
    log "ORCHESTRA_RUNTIME_VALIDATION_PASS"
}

case "$MODE" in
    static) static_validation ;;
    runtime) runtime_validation ;;
    all) static_validation; runtime_validation ;;
esac
log "ORCHESTRA_VALIDATION_PASS"
