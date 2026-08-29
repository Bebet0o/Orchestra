#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HERMESOPS_ROOT:-/opt/docker/hermesops}"
MODE="all"
QUIET=0

while (($#)); do
    case "$1" in
        --static) MODE="static"; shift ;;
        --runtime) MODE="runtime"; shift ;;
        --quiet) QUIET=1; shift ;;
        --help|-h)
            cat <<'HELP'
Usage: ./validate.sh [--static|--runtime] [--quiet]
HELP
            exit 0
            ;;
        *) echo "Option inconnue: $1" >&2; exit 2 ;;
    esac
done

log() { [[ "$QUIET" == 1 ]] || printf '%s\n' "$*"; }

static_validation() {
    log "== Validation statique =="
    [[ "$(cat "${REPO}/VERSION")" == "0.1.0-alpha" ]]

    for file in \
        install.sh uninstall.sh preflight.sh validate.sh \
        scripts/check-secrets.sh scripts/check-secrets.py \
        scripts/export-worker-image.sh scripts/init-test-fixtures.sh \
        scripts/environment_resolution/__init__.py \
        scripts/environment_resolution/contract.py \
        scripts/environment_resolution/default.py \
        scripts/legacy_worker_environment.py \
        scripts/oci_reference.py \
        scripts/sandbox_backend.py \
        scripts/check-worker-oci-image.py \
        scripts/hermesops-worker.py scripts/hermesops-reviewer.py \
        config/environments/default-worker.toml \
        images/orchestra-worker.Dockerfile \
        images/orchestra-worker.Dockerfile.dockerignore \
        .github/scripts/anonymous_worker_pull.py \
        .github/scripts/worker_publication.py \
        .github/workflows/publish-worker.yml \
        docs/distribution/WORKER_IMAGE.md \
        tests/test_environment_resolution.py \
        tests/test_environment_resolution_guards.py \
        tests/test_sandbox_backend.py \
        tests/test_worker_distribution.py \
        tests/test_worker_publication.py \
        tests/test_trusted_worker_publisher.py \
        tests/test-public-empty-registry.sh \
        tests/test-preflight-minimal-host.sh \
        tests/test-install-no-auth-contract.sh \
        tests/test-systemd-user-boot-order.sh \
        tests/test-release-documentation.sh \
        tests/test-controller-contracts.sh \
        tests/test-controller-api.sh tests/test_controller_api.py \
        tests/test-controller-objective-reads.sh \
        tests/test_controller_objective_reads.py \
        tests/test-controller-execution-reads.sh \
        tests/test_controller_execution_reads.py \
        tests/test-controller-review-recovery-reads.sh \
        tests/test_controller_review_recovery_reads.py \
        tests/test-controller-objective-commands.sh \
        tests/test_controller_objective_commands.py \
        tests/test-controller-review-commands.sh \
        tests/test_controller_review_commands.py \
  tests/test-controller-event-journal.sh \
  tests/test_controller_event_journal.py \
  tests/test-controller-event-journal-adversarial.sh \
  tests/test_controller_event_journal_adversarial.py \
  tests/test-controller-websocket-transport.sh \
  tests/test_controller_websocket_transport.py \
  tests/test-controller-websocket-adversarial.sh \
  tests/test_controller_websocket_transport_adversarial.py \
  tests/test-controller-browser-auth.sh \
  tests/test_controller_browser_auth.py \
  tests/test-controller-browser-auth-adversarial.sh \
  tests/test_controller_browser_auth_adversarial.py \
  controller_api/orchestration_reads.py \
  controller_api/hermesfile.py \
  controller_api/hermesfile_lifecycle.py \
  controller_api/sandbox_profiles.py \
  controller_api/sandbox_profile_probe.py \
  scripts/hermesops-hermesfile.py \
  scripts/hermesops-sandbox-profile.py \
  scripts/hermesops-controller-sandbox-profile-probe.py \
  tests/test_hermesfile_v1.py \
  tests/test-hermesfile-v1.sh \
  tests/test_sandbox_profiles.py \
  tests/test_controller_sandbox_profile_reads.py \
  tests/test-sandbox-profiles.sh \
  scripts/hermesops-controller-orchestration-probe.py \
  tests/test_controller_orchestration_reads.py \
  scripts/hermesops_review_assignment.py \
  tests/test_reviewer_assignments.py \
        tests/test-controller-service-contract.sh \
        tests/test-controller-service-lifecycle.sh \
        tests/test-controller-service-persistence.sh \
        tests/test_controller_service.py \
        scripts/hermesops-controller-api.py \
        scripts/hermesops-controller-objective-probe.py \
        scripts/hermesops-controller-execution-probe.py \
        scripts/hermesops-controller-review-recovery-probe.py \
        scripts/hermesops-controller-objective-command-probe.py \
        scripts/hermesops-controller-review-command-probe.py \
        scripts/hermesops-controller-session.py \
        scripts/hermesops-controller-probe.py \
        scripts/hermesops-controller-websocket-probe.py \
        scripts/hermesops-controller-operator.py \
        scripts/hermesops-controller-browser-auth-probe.py \
        scripts/hermesops-console-build.py \
        scripts/hermesops-console.py \
        scripts/hermesops-console-probe.py \
        tests/test-console-foundation.sh \
        tests/test-console-controller-client.sh \
        tests/test-console-operational-dashboard.sh \
        tests/test-controller-project-lifecycle.sh \
        tests/test-console-project-lifecycle.sh \
        tests/test-controller-hermesfile-lifecycle.sh \
        tests/test-console-hermesfile-lifecycle.sh \
        tests/test-console-objective-lifecycle.sh \
        tests/test_console_service.py \
        tests/test_console_controller_client.py \
        tests/test_console_operational_dashboard.py \
        tests/test_controller_project_lifecycle.py \
        tests/test_console_project_lifecycle.py \
        tests/test_controller_hermesfile_lifecycle.py \
        tests/test_console_hermesfile_lifecycle.py \
        tests/test_console_objective_lifecycle.py \
        controller_api/__init__.py controller_api/core.py \
        controller_api/server.py controller_api/service_support.py \
        controller_api/objective_reads.py controller_api/objective_probe.py \
        controller_api/execution_reads.py controller_api/execution_probe.py \
        controller_api/review_recovery_reads.py \
        controller_api/review_recovery_probe.py \
        controller_api/objective_commands.py \
        controller_api/objective_command_probe.py \
        controller_api/project_commands.py \
        controller_api/review_commands.py \
        controller_api/review_command_probe.py \
  controller_api/event_journal.py \
  controller_api/websocket_transport.py \
  controller_api/websocket_probe.py \
  controller_api/browser_auth.py \
  controller_api/browser_auth_probe.py \
        migrations/012_controller_command_foundation.sql \
        migrations/013_controller_review_commands.sql \
  migrations/014_controller_review_command_hardening.sql \
  migrations/015_controller_event_journal.sql \
  migrations/016_controller_event_journal_hardening.sql \
  migrations/017_browser_session_lifecycle.sql \
  migrations/020_sandbox_profile_persistence.sql \
  migrations/021_project_lifecycle.sql \
  migrations/022_hermesfile_lifecycle.sql \
        systemd/user/hermesops-controller-api.service \
        systemd/user/hermesops-console.service \
        docs/milestones/2B_CONTROLLER_API_SKELETON.md \
        docs/milestones/2C_CONTROLLER_API_SERVICE.md \
        docs/milestones/2G_SECURE_OBJECTIVE_COMMANDS.md \
        docs/milestones/2H_HUMAN_REVIEW_COMMANDS.md \
  docs/milestones/2I_CONTROLLER_EVENT_JOURNAL.md \
  docs/milestones/2J_AUTHENTICATED_WEBSOCKET_TRANSPORT.md \
  docs/milestones/2M_PUBLIC_ORCHESTRATION_READS.md \
  docs/milestones/2N_HERMESFILE_V1.md \
  docs/milestones/2O_SANDBOX_PROFILE_PERSISTENCE.md \
  docs/milestones/2P_CONSOLE_WEB_FOUNDATION.md \
  docs/milestones/2Q_BROWSER_SESSION_CONTROLLER_CLIENT.md \
  docs/milestones/2R_OPERATIONAL_DASHBOARD.md \
  docs/milestones/2S_PROJECT_LIFECYCLE.md \
  docs/milestones/2T_HERMESFILE_LIFECYCLE.md \
  docs/milestones/2U_OBJECTIVE_LIFECYCLE.md \
  docs/console/FOUNDATION.md docs/console/CONTROLLER_CLIENT.md \
  docs/console/OPERATIONAL_DASHBOARD.md \
  docs/console/PROJECT_LIFECYCLE.md \
  docs/console/OBJECTIVE_LIFECYCLE.md \
  console/src/index.html console/src/app.js \
  console/src/controller-client.js console/src/styles.css \
  console/dist/index.html console/dist/assets/app.js \
  console/dist/assets/controller-client.js \
  console/dist/assets/styles.css console/dist/asset-manifest.json \
  docs/hermesfile/SPECIFICATION_V1.md \
  specs/hermesfile-v1.schema.json \
  config/examples/Hermesfile \
        compose/agent.yaml compose/images.lock.env \
        compose/agent.env.example compose/webui.env.example \
        compose/notifications.env.example config/host-packages.lock.toml
    do
        [[ -f "${REPO}/${file}" ]]
    done

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

    if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        python3 - "$REPO" <<'PY'
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
result = subprocess.run(
    ["git", "-C", str(root), "ls-files", "config/projects.d/*.toml"],
    check=True,
    text=True,
    stdout=subprocess.PIPE,
)
tracked = sorted(
    line for line in result.stdout.splitlines() if line
)
if tracked:
    raise SystemExit(
        f"Active project configuration tracked: {tracked}"
    )

required = (
    root / "tests/fixtures/projects/transaction-fixture.toml",
    root / "tests/fixtures/projects/transaction-fixture-b.toml",
)
missing = [
    path.relative_to(root).as_posix()
    for path in required
    if not path.is_file()
]
if missing:
    raise SystemExit(f"Test fixture templates missing: {missing}")

print("Tracked active project configurations: NONE")
print("Test fixture templates: PASS")
PY
    fi

    python3 - "$REPO/migrations" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
versions = [
    int(path.name.split("_", 1)[0])
    for path in sorted(root.glob("[0-9][0-9][0-9]_*.sql"))
]
if versions != list(range(1, len(versions) + 1)):
    raise SystemExit(f"Migration sequence invalid: {versions}")
print(f"Migration sequence: PASS ({len(versions)})")
PY

    python3 "${REPO}/tests/test_environment_resolution.py"
    python3 "${REPO}/tests/test_environment_resolution_guards.py"
    python3 "${REPO}/tests/test_sandbox_backend.py"
    python3 "${REPO}/tests/test_worker_distribution.py"
    python3 "${REPO}/tests/test_worker_publication.py"
    python3 "${REPO}/tests/test_trusted_worker_publisher.py"
    "${REPO}/tests/test-public-empty-registry.sh"
    "${REPO}/tests/test-preflight-minimal-host.sh"
    "${REPO}/tests/test-install-no-auth-contract.sh"
    "${REPO}/tests/test-systemd-user-boot-order.sh"
    "${REPO}/tests/test-release-documentation.sh"
    "${REPO}/tests/test-controller-contracts.sh"
    "${REPO}/tests/test-controller-api.sh"
    "${REPO}/tests/test-controller-objective-reads.sh"
    "${REPO}/tests/test-controller-execution-reads.sh"
    "${REPO}/tests/test-controller-review-recovery-reads.sh"
    "${REPO}/tests/test-controller-objective-commands.sh"
    "${REPO}/tests/test-controller-review-commands.sh"
  "${REPO}/tests/test-controller-event-journal.sh"
  "${REPO}/tests/test-controller-event-journal-adversarial.sh"
  "${REPO}/tests/test-controller-websocket-transport.sh"
  "${REPO}/tests/test-controller-websocket-adversarial.sh"
  "${REPO}/tests/test-controller-browser-auth.sh"
  "${REPO}/tests/test-controller-browser-auth-adversarial.sh"
  "${REPO}/tests/test-controller-orchestration-reads.sh"
  "${REPO}/tests/test-hermesfile-v1.sh"
  "${REPO}/tests/test-sandbox-profiles.sh"
  "${REPO}/tests/test-console-foundation.sh"
  "${REPO}/tests/test-console-controller-client.sh"
  "${REPO}/tests/test-console-operational-dashboard.sh"
  "${REPO}/tests/test-controller-project-lifecycle.sh"
  "${REPO}/tests/test-console-project-lifecycle.sh"
  "${REPO}/tests/test-controller-hermesfile-lifecycle.sh"
  "${REPO}/tests/test-console-hermesfile-lifecycle.sh"
  "${REPO}/tests/test-console-objective-lifecycle.sh"
  "${REPO}/tests/test-reviewer-assignments.sh"
    "${REPO}/tests/test-controller-service-contract.sh"

    TMP="$(mktemp -d)"
    mkdir -p \
        "$TMP/root/repo/compose" \
        "$TMP/root/secrets" \
        "$TMP/root/state" \
        "$TMP/root/runtime" \
        "$TMP/root/workspaces" \
        "$TMP/root/project-data"
    cp "${REPO}/compose/agent.yaml" "$TMP/root/repo/compose/"
    cp "${REPO}/compose/images.lock.env" "$TMP/root/repo/compose/"
    cp "${REPO}/compose/agent.env.example" "$TMP/root/secrets/agent.env"
    cp "${REPO}/compose/webui.env.example" "$TMP/root/secrets/webui.env"

    HERMES_UID="$(id -u)" HERMES_GID="$(id -g)" \
    docker compose \
        --project-directory "$TMP/root/repo/compose" \
        --env-file "$TMP/root/repo/compose/images.lock.env" \
        -f "$TMP/root/repo/compose/agent.yaml" \
        config --quiet
    rm -rf "$TMP"
    log "HERMESOPS_STATIC_VALIDATION_PASS"
}

runtime_validation() {
    log "== Validation runtime =="
    [[ "$REPO" == "${ROOT}/repo" ]] || {
        echo "Lancer depuis ${ROOT}/repo." >&2
        exit 1
    }

    "${REPO}/scripts/verify-layout.sh"
    "${REPO}/scripts/hermesops-db.py" integrity
    "${REPO}/scripts/hermesops-registry.py" validate
    "${REPO}/scripts/hermesops-roles.py" validate
    "${REPO}/scripts/hermesops-roles.py" verify-profiles

    COMPOSE=(
        docker compose
        --project-directory "${REPO}/compose"
        --env-file "${REPO}/compose/images.lock.env"
        -f "${REPO}/compose/agent.yaml"
    )
    "${COMPOSE[@]}" config --quiet

    for container in hermesops-sandbox-engine hermesops-agent hermesops-webui; do
        health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
        [[ "$health" == "healthy" || "$health" == "running" ]] || {
            echo "$container non sain: $health" >&2
            exit 1
        }
    done

    curl --silent --show-error --fail --max-time 5 \
        http://127.0.0.1:8642/health >/dev/null
    curl --silent --show-error --fail --max-time 5 \
        http://127.0.0.1:8787/health >/dev/null
    curl --silent --show-error --fail --max-time 5 \
        http://127.0.0.1:8788/health >/dev/null

    readarray -t WORKER_LOCK < <(
        python3 - "${REPO}/config/worker-sandbox.lock.toml" <<'PY'
import sys
import tomllib
from pathlib import Path
with Path(sys.argv[1]).open("rb") as stream:
    data = tomllib.load(stream)
print(data["tag"])
print(data["image_id"])
PY
    )
    worker_actual="$(docker exec hermesops-sandbox-engine docker image inspect --format '{{.Id}}' "${WORKER_LOCK[0]}")"
    [[ "$worker_actual" == "${WORKER_LOCK[1]}" ]]

    for unit in hermesops-supervisor.service hermesops-orchestrator.service hermesops-notifier.service hermesops-controller-api.service hermesops-console.service; do
        systemctl --user is-enabled "$unit" >/dev/null
        systemctl --user is-active "$unit" >/dev/null
    done

    "${REPO}/tests/test-controller-service-persistence.sh"
    "${REPO}/scripts/hermesops-console-probe.py" \
        --base-url http://127.0.0.1:8788 \
        --wait-seconds 10
    "${REPO}/scripts/hermesops-controller-objective-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    "${REPO}/scripts/hermesops-controller-execution-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    "${REPO}/scripts/hermesops-controller-review-recovery-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    "${REPO}/scripts/hermesops-controller-objective-command-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    "${REPO}/scripts/hermesops-controller-review-command-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    "${REPO}/scripts/hermesops-controller-session.py" check
    "${REPO}/scripts/hermesops-controller-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --session-file "${ROOT}/secrets/controller-session" \
        --wait-seconds 10
    [[ "$(stat -c '%a' "${ROOT}/secrets/controller-session")" == "600" ]]

    [[ -f "${ROOT}/state/hermes-home/auth.json" ]]
    [[ "$(stat -c '%a' "${ROOT}/state/hermes-home/auth.json")" == "600" ]]
    [[ "$(stat -c '%a' "${ROOT}/secrets")" == "700" ]]
    log "HERMESOPS_RUNTIME_VALIDATION_PASS"
}

case "$MODE" in
    static) static_validation ;;
    runtime) runtime_validation ;;
    all) static_validation; runtime_validation ;;
esac
log "HERMESOPS_VALIDATION_PASS"
