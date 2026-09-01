#!/usr/bin/env bash
set -Eeuo pipefail

AGENT="orchestra-hermes-agent"
ENGINE="orchestra-sandbox-engine"
SOCKET="/run/orchestra-docker/docker.sock"
ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
PRIVATE_DOCKER_HOST="unix://${ROOT}/runtime/sandbox-engine-socket/docker.sock"

echo "=== Services hôte ==="
docker ps \
    --filter "name=^/${AGENT}$" \
    --filter "name=^/${ENGINE}$" \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo
echo "=== Transport sandbox ==="
docker exec "$AGENT" sh -lc '
    printf "DOCKER_HOST=%s\n" "$DOCKER_HOST"
    stat -c "%A mode=%a uid=%u gid=%g path=%n" \
      /run/orchestra-docker/docker.sock
'

echo
echo "=== Docker dédié vu par Hermes ==="
docker --host "$PRIVATE_DOCKER_HOST" info \
    --format 'Name={{.Name}} Driver={{.Driver}} Containers={{.Containers}} Images={{.Images}}'

echo
echo "=== Sandboxes imbriquées ==="
docker --host "$PRIVATE_DOCKER_HOST" ps -a \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Labels}}'

echo
echo "=== Vérification TCP ==="
docker inspect "$ENGINE" \
    --format 'Command={{json .Config.Cmd}}'

docker exec "$ENGINE" sh -lc '
    if grep -qiE ":(0947|0948) " /proc/net/tcp /proc/net/tcp6; then
        echo "ALERTE: listener TCP 2375/2376 détecté"
        exit 1
    fi
    echo "Aucun listener TCP 2375/2376"
'
