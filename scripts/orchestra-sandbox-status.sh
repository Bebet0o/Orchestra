#!/usr/bin/env bash
set -Eeuo pipefail

AGENT="orchestra-hermes-agent"
ENGINE="orchestra-sandbox-engine"
SOCKET="/run/orchestra-docker/docker.sock"
ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
PRIVATE_DOCKER_HOST="unix://${ROOT}/runtime/sandbox-engine-socket/docker.sock"

echo "=== Host services ==="
docker ps \
    --filter "name=^/${AGENT}$" \
    --filter "name=^/${ENGINE}$" \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo
echo "=== Sandbox transport ==="
docker exec "$AGENT" sh -lc '
    printf "DOCKER_HOST=%s\n" "$DOCKER_HOST"
    stat -c "%A mode=%a uid=%u gid=%g path=%n" \
      /run/orchestra-docker/docker.sock
'

echo
echo "=== Dedicated Docker daemon visible to Hermes ==="
docker --host "$PRIVATE_DOCKER_HOST" info \
    --format 'Name={{.Name}} Driver={{.Driver}} Containers={{.Containers}} Images={{.Images}}'

echo
echo "=== Nested sandboxes ==="
docker --host "$PRIVATE_DOCKER_HOST" ps -a \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Labels}}'

echo
echo "=== TCP exposure check ==="
docker inspect "$ENGINE" \
    --format 'Command={{json .Config.Cmd}}'

docker exec "$ENGINE" sh -lc '
    if grep -qiE ":(0947|0948) " /proc/net/tcp /proc/net/tcp6; then
        echo "ALERT: TCP listener 2375/2376 detected"
        exit 1
    fi
    echo "No TCP listener on 2375/2376"
'
