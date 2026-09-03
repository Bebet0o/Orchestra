#!/bin/sh
set -eu

SOCKET=/run/orchestra-docker/docker.sock
DATA_ROOT="${ORCHESTRA_DATA_ROOT:-/var/lib/orchestra}"
RUNTIME_UID="${ORCHESTRA_RUNTIME_UID:-1000}"
RUNTIME_GID="${ORCHESTRA_RUNTIME_GID:-1000}"
AGENT_IMAGE="${HERMES_AGENT_IMAGE:?HERMES_AGENT_IMAGE is required}"
WEBUI_IMAGE="${HERMES_WEBUI_IMAGE:?HERMES_WEBUI_IMAGE is required}"
WORKER_IMAGE="${ORCHESTRA_WORKER_IMAGE:?ORCHESTRA_WORKER_IMAGE is required}"

log() { printf '[runtime] %s\n' "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

require_digest_reference() {
    printf '%s\n' "$1" | grep -Eq '^[a-z0-9.-]+(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$' ||
        fail "immutable OCI reference required: $1"
}

docker_cli() {
    DOCKER_HOST="unix://${SOCKET}" DOCKER_CONTEXT=default \
        DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config docker "$@"
}

health() {
    docker_cli info >/dev/null 2>&1 || exit 1
    for child in orchestra-hermes-agent orchestra-hermes-webui; do
        state="$(docker_cli inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$child" 2>/dev/null || true)"
        case "$state" in
            'true none'|'true healthy') ;;
            *) exit 1 ;;
        esac
    done
}

ensure_secret_files() {
    install -d -m 0750 -o "$RUNTIME_UID" -g "$RUNTIME_GID" \
        "$DATA_ROOT" "$DATA_ROOT/state" "$DATA_ROOT/state/controller" \
        "$DATA_ROOT/state/hermes-home" "$DATA_ROOT/state/sandboxes" \
        "$DATA_ROOT/runtime" "$DATA_ROOT/workspaces" \
        "$DATA_ROOT/project-data" "$DATA_ROOT/backups" "$DATA_ROOT/logs"
    install -d -m 0700 -o "$RUNTIME_UID" -g "$RUNTIME_GID" "$DATA_ROOT/secrets"
    if [ ! -s "$DATA_ROOT/secrets/agent.env" ]; then
        key="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
        printf 'API_SERVER_KEY=%s\n' "$key" >"$DATA_ROOT/secrets/agent.env"
    else
        key="$(sed -n 's/^API_SERVER_KEY=//p' "$DATA_ROOT/secrets/agent.env" | head -n 1)"
    fi
    [ -n "$key" ] || fail "agent API key is empty"
    if [ ! -s "$DATA_ROOT/secrets/webui.env" ]; then
        password="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
        {
            printf 'HERMES_WEBUI_GATEWAY_API_KEY=%s\n' "$key"
            printf 'HERMES_WEBUI_PASSWORD=%s\n' "$password"
        } >"$DATA_ROOT/secrets/webui.env"
    fi
    chown "$RUNTIME_UID:$RUNTIME_GID" \
        "$DATA_ROOT/secrets/agent.env" "$DATA_ROOT/secrets/webui.env"
    chmod 0600 "$DATA_ROOT/secrets/agent.env" "$DATA_ROOT/secrets/webui.env"
}

ensure_child() {
    name="$1"
    expected_image="$2"
    command_one="$3"
    command_two="$4"
    shift 4
    if docker_cli container inspect "$name" >/dev/null 2>&1; then
        actual_image="$(docker_cli inspect --format '{{.Config.Image}}' "$name")"
        [ "$actual_image" = "$expected_image" ] || {
            docker_cli rm -f "$name" >/dev/null
        }
    fi
    if ! docker_cli container inspect "$name" >/dev/null 2>&1; then
        if [ "$command_one" = : ]; then
            docker_cli run -d --restart unless-stopped --name "$name" "$@" "$expected_image" >/dev/null
        else
            docker_cli run -d --restart unless-stopped --name "$name" "$@" "$expected_image" "$command_one" "$command_two" >/dev/null
        fi
    elif [ "$(docker_cli inspect --format '{{.State.Running}}' "$name")" != true ]; then
        docker_cli start "$name" >/dev/null
    fi
}

shutdown() {
    trap - TERM INT EXIT
    log "stopping private runtime"
    docker_cli stop -t 30 orchestra-hermes-webui orchestra-hermes-agent >/dev/null 2>&1 || true
    for pid in ${LOG_PIDS:-}; do kill "$pid" 2>/dev/null || true; done
    if [ -n "${DOCKERD_PID:-}" ]; then
        kill -TERM "$DOCKERD_PID" 2>/dev/null || true
        wait "$DOCKERD_PID" 2>/dev/null || true
    fi
}

case "${1:-run}" in
    health) health; exit $? ;;
    run) ;;
    *) fail "usage: orchestra-runtime-entrypoint [run|health]" ;;
esac

require_digest_reference "$AGENT_IMAGE"
require_digest_reference "$WEBUI_IMAGE"
require_digest_reference "$WORKER_IMAGE"
ensure_secret_files
install -d -m 0770 -o root -g "$RUNTIME_GID" /run/orchestra-docker
rm -f "$SOCKET"

trap shutdown TERM INT EXIT
log "starting private Docker daemon"
dockerd --host="unix://${SOCKET}" --group="$RUNTIME_GID" \
    --storage-driver=overlay2 --log-level=warn &
DOCKERD_PID=$!

ready=0
for _ in $(seq 1 90); do
    if docker_cli info >/dev/null 2>&1; then ready=1; break; fi
    kill -0 "$DOCKERD_PID" 2>/dev/null || fail "private Docker daemon exited"
    sleep 1
done
[ "$ready" = 1 ] || fail "private Docker daemon readiness timeout"
log "private Docker daemon ready"

docker_cli network inspect orchestra-internal >/dev/null 2>&1 ||
    docker_cli network create orchestra-internal >/dev/null
docker_cli volume inspect orchestra-hermes-agent-src >/dev/null 2>&1 ||
    docker_cli volume create orchestra-hermes-agent-src >/dev/null

for image in "$AGENT_IMAGE" "$WEBUI_IMAGE" "$WORKER_IMAGE"; do
    log "materializing immutable image $image"
    docker_cli pull "$image"
done

ensure_child orchestra-hermes-agent "$AGENT_IMAGE" gateway run \
    --network orchestra-internal \
    --env-file "$DATA_ROOT/secrets/agent.env" \
    --env HERMES_HOME=/home/hermes/.hermes \
    --env API_SERVER_ENABLED=true --env API_SERVER_HOST=0.0.0.0 --env API_SERVER_PORT=8642 \
    --env DOCKER_HOST="unix://${SOCKET}" --env DOCKER_TLS_CERTDIR= \
    --env HERMES_DOCKER_BINARY=/usr/bin/docker \
    --env TERMINAL_SANDBOX_DIR="$DATA_ROOT/state/sandboxes" \
    --volume "$DATA_ROOT/state/hermes-home:/home/hermes/.hermes" \
    --volume orchestra-hermes-agent-src:/opt/hermes \
    --volume "$SOCKET:$SOCKET" \
    --volume "$DATA_ROOT/state/sandboxes:$DATA_ROOT/state/sandboxes" \
    --volume "$DATA_ROOT/workspaces:$DATA_ROOT/workspaces" \
    --volume "$DATA_ROOT/project-data:$DATA_ROOT/project-data"

ensure_child orchestra-hermes-webui "$WEBUI_IMAGE" : : \
    --network orchestra-internal \
    --env-file "$DATA_ROOT/secrets/webui.env" \
    --env HERMES_WEBUI_HOST=0.0.0.0 --env HERMES_WEBUI_PORT=8787 \
    --env HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui \
    --env HERMES_API_URL=http://orchestra-hermes-agent:8642 \
    --env HERMES_WEBUI_CHAT_BACKEND=gateway \
    --env HERMES_WEBUI_GATEWAY_BASE_URL=http://orchestra-hermes-agent:8642 \
    --volume "$DATA_ROOT/state/hermes-home:/home/hermeswebui/.hermes" \
    --volume orchestra-hermes-agent-src:/home/hermeswebui/.hermes/hermes-agent:ro \
    --volume "$DATA_ROOT/workspaces:/workspace:ro"

log "Hermes integration containers ready"
LOG_PIDS=""
docker_cli logs --follow orchestra-hermes-agent 2>&1 | sed 's/^/[hermes-agent] /' &
LOG_PIDS="$LOG_PIDS $!"
docker_cli logs --follow orchestra-hermes-webui 2>&1 | sed 's/^/[hermes-webui] /' &
LOG_PIDS="$LOG_PIDS $!"
wait "$DOCKERD_PID"
