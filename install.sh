#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="/opt/docker/hermesops"
TARGET_USER=""
AUTH_FILE=""
WORKER_ARCHIVE=""
NON_INTERACTIVE=0
OFFLINE=0
UPGRADE=0
SKIP_START=0
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONTROLLER_UNIT_NAME="hermesops-controller-api.service"
CONTROLLER_UNIT_TARGET=""
CONTROLLER_UNIT_BACKUP=""
CONTROLLER_UNIT_EXISTED=0
CONTROLLER_UNIT_WAS_ENABLED=0
CONTROLLER_UNIT_WAS_ACTIVE=0
CONTROLLER_UNIT_TOUCHED=0
CONSOLE_UNIT_NAME="hermesops-console.service"
CONSOLE_UNIT_TARGET=""
CONSOLE_UNIT_BACKUP=""
CONSOLE_UNIT_EXISTED=0
CONSOLE_UNIT_WAS_ENABLED=0
CONSOLE_UNIT_WAS_ACTIVE=0
CONSOLE_UNIT_TOUCHED=0

PLATFORM_SUPPORT="${SOURCE}/scripts/platform-support.sh"
[[ -r "$PLATFORM_SUPPORT" ]] || {
    echo "Contrat de plateforme absent: $PLATFORM_SUPPORT" >&2
    exit 1
}
# shellcheck disable=SC1090
. "$PLATFORM_SUPPORT"

usage() {
    cat <<'HELP'
Usage: ./install.sh [options]

  --user USER                  Utilisateur des services systemd.
  --auth-file PATH             auth.json OpenAI Codex à installer.
  --worker-image-archive PATH  Archive .tar ou .tar.gz du worker verrouillé.
  --upgrade                    Autoriser une mise à niveau divergente sauvegardée.
  --offline                    Interdire tout téléchargement.
  --skip-start                 Installer sans démarrer les services.
  --non-interactive            Refuser toute demande sudo interactive.
  -h, --help                   Afficher cette aide.

La racine 0.1.0-alpha est fixe : /opt/docker/hermesops
HELP
}

while (($#)); do
    case "$1" in
        --user) TARGET_USER="${2:?Utilisateur manquant}"; shift 2 ;;
        --auth-file) AUTH_FILE="${2:?Chemin auth manquant}"; shift 2 ;;
        --worker-image-archive) WORKER_ARCHIVE="${2:?Archive manquante}"; shift 2 ;;
        --upgrade) UPGRADE=1; shift ;;
        --offline) OFFLINE=1; shift ;;
        --skip-start) SKIP_START=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Option inconnue: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$TARGET_USER" ]]; then
    if [[ "$EUID" == 0 ]]; then
        TARGET_USER="${SUDO_USER:-}"
    else
        TARGET_USER="$(id -un)"
    fi
fi
[[ -n "$TARGET_USER" ]] || {
    echo "Exécution root: préciser --user USER." >&2
    exit 1
}
id "$TARGET_USER" >/dev/null
if [[ "$EUID" != 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    echo "sudo est requis pour une installation lancée sans root." >&2
    exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ "$TARGET_UID" == "1000" && "$TARGET_GID" == "1000" ]] || {
    echo "HermesOps 0.1.0-alpha est validé uniquement pour UID/GID 1000:1000." >&2
    echo "Utilisateur observé: ${TARGET_UID}:${TARGET_GID}" >&2
    exit 1
}
REPO="${ROOT}/repo"
REPORT_DIR="${ROOT}/runtime/install-reports"
REPORT="${REPORT_DIR}/install-${STAMP}.log"
STATUS="${REPORT_DIR}/install-${STAMP}.status"

sudo_run() {
    if [[ "$EUID" == 0 ]]; then
        "$@"
    elif [[ "$NON_INTERACTIVE" == 1 ]]; then
        sudo -n "$@"
    else
        sudo "$@"
    fi
}

user_run() {
    if [[ "$(id -u)" == "$TARGET_UID" ]]; then
        "$@"
    elif [[ "$EUID" == 0 ]]; then
        runuser -u "$TARGET_USER" -- env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
            XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" "$@"
    elif [[ "$NON_INTERACTIVE" == 1 ]]; then
        sudo -n -u "$TARGET_USER" env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
            XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" "$@"
    else
        sudo -u "$TARGET_USER" env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
            XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" "$@"
    fi
}

sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$REPORT_DIR"
sudo_run touch "$REPORT" "$STATUS"
sudo_run chown "$TARGET_USER:$TARGET_GROUP" "$REPORT" "$STATUS"
sudo_run chmod 0640 "$REPORT" "$STATUS"
exec > >(tee -a "$REPORT") 2>&1
printf 'RUNNING\n' >"$STATUS"

restore_controller_unit() {
    [[ "$CONTROLLER_UNIT_TOUCHED" == 1 ]] || return 0

    user_run systemctl --user disable --now \
        "$CONTROLLER_UNIT_NAME" >/dev/null 2>&1 || true
    user_run systemctl --user reset-failed \
        "$CONTROLLER_UNIT_NAME" >/dev/null 2>&1 || true

    if [[ "$CONTROLLER_UNIT_EXISTED" == 1 ]]; then
        sudo_run install -m 0640 \
            -o "$TARGET_USER" -g "$TARGET_GROUP" \
            "$CONTROLLER_UNIT_BACKUP" "$CONTROLLER_UNIT_TARGET"
    else
        sudo_run rm -f \
            "$CONTROLLER_UNIT_TARGET" \
            "${TARGET_HOME}/.config/systemd/user/default.target.wants/${CONTROLLER_UNIT_NAME}"
    fi

    user_run systemctl --user daemon-reload >/dev/null 2>&1 || true

    if [[ "$CONTROLLER_UNIT_EXISTED" == 1 &&
          "$CONTROLLER_UNIT_WAS_ENABLED" == 1 ]]; then
        user_run systemctl --user enable \
            "$CONTROLLER_UNIT_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$CONTROLLER_UNIT_EXISTED" == 1 &&
          "$CONTROLLER_UNIT_WAS_ACTIVE" == 1 ]]; then
        user_run systemctl --user start \
            "$CONTROLLER_UNIT_NAME" >/dev/null 2>&1 || true
    fi
}

restore_console_unit() {
    [[ "$CONSOLE_UNIT_TOUCHED" == 1 ]] || return 0

    user_run systemctl --user disable --now \
        "$CONSOLE_UNIT_NAME" >/dev/null 2>&1 || true
    user_run systemctl --user reset-failed \
        "$CONSOLE_UNIT_NAME" >/dev/null 2>&1 || true

    if [[ "$CONSOLE_UNIT_EXISTED" == 1 ]]; then
        sudo_run install -m 0640 \
            -o "$TARGET_USER" -g "$TARGET_GROUP" \
            "$CONSOLE_UNIT_BACKUP" "$CONSOLE_UNIT_TARGET"
    else
        sudo_run rm -f \
            "$CONSOLE_UNIT_TARGET" \
            "${TARGET_HOME}/.config/systemd/user/default.target.wants/${CONSOLE_UNIT_NAME}"
    fi

    user_run systemctl --user daemon-reload >/dev/null 2>&1 || true

    if [[ "$CONSOLE_UNIT_EXISTED" == 1 &&
          "$CONSOLE_UNIT_WAS_ENABLED" == 1 ]]; then
        user_run systemctl --user enable \
            "$CONSOLE_UNIT_NAME" >/dev/null 2>&1 || true
    fi
    if [[ "$CONSOLE_UNIT_EXISTED" == 1 &&
          "$CONSOLE_UNIT_WAS_ACTIVE" == 1 ]]; then
        user_run systemctl --user start \
            "$CONSOLE_UNIT_NAME" >/dev/null 2>&1 || true
    fi
}

on_error() {
    rc=$?
    trap - ERR
    restore_console_unit || true
    restore_controller_unit || true
    printf 'FAILED rc=%s\n' "$rc" >"$STATUS"
    echo "Installation échouée. Rapport: $REPORT" >&2
    exit "$rc"
}
trap on_error ERR

echo "HermesOps installation ${STAMP}"
echo "Source      : $SOURCE"
echo "Destination : $ROOT"
echo "Utilisateur : $TARGET_USER ($TARGET_UID:$TARGET_GID)"

# shellcheck disable=SC1091
. /etc/os-release
orchestra_os_supported "${ID:-}" "${VERSION_ID:-}" || {
    echo "Système non pris en charge: ${ID:-inconnu} ${VERSION_ID:-inconnue}; $(orchestra_platform_contract) requis." >&2
    exit 1
}
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
orchestra_arch_supported "$ARCH" || {
    echo "Architecture amd64 requise." >&2
    exit 1
}
DOCKER_REPOSITORY_FAMILY="$(orchestra_docker_repository_family "$ID")"
[[ "${VERSION_CODENAME:-}" =~ ^[a-z][a-z0-9-]*$ ]] || {
    echo "VERSION_CODENAME invalide ou absent dans /etc/os-release." >&2
    exit 1
}

BASE_PACKAGES=(ca-certificates curl git gzip python3 python3-yaml rsync sqlite3 util-linux)
MISSING_PACKAGES=()
for package in "${BASE_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null |
        grep -Fq 'install ok installed' || MISSING_PACKAGES+=("$package")
done
if ((${#MISSING_PACKAGES[@]})); then
    [[ "$OFFLINE" == 0 ]] || {
        echo "Paquets absents en mode offline: ${MISSING_PACKAGES[*]}" >&2
        exit 1
    }
    sudo_run apt-get update
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        --no-install-recommends "${MISSING_PACKAGES[@]}"
fi

command -v runuser >/dev/null 2>&1 || {
    echo "runuser reste absent après installation de util-linux." >&2
    exit 1
}

HOST_LOCK="${SOURCE}/config/host-packages.lock.toml"
[[ -f "$HOST_LOCK" ]] || {
    echo "Lock des paquets hôte absent: $HOST_LOCK" >&2
    exit 1
}
readarray -t HOST_VERSIONS < <(
    python3 - "$HOST_LOCK" <<'PY'
import sys
import tomllib
from pathlib import Path
with Path(sys.argv[1]).open("rb") as stream:
    data = tomllib.load(stream)
print(data["docker_ce"])
print(data["docker_ce_cli"])
print(data["docker_compose_plugin"])
PY
)
DOCKER_CE_VERSION="${HOST_VERSIONS[0]}"
DOCKER_CLI_VERSION="${HOST_VERSIONS[1]}"
DOCKER_COMPOSE_VERSION="${HOST_VERSIONS[2]}"

configure_docker_repository() {
    [[ "$OFFLINE" == 0 ]] || {
        echo "Docker absent et mode offline actif." >&2
        exit 1
    }

    sudo_run install -m 0755 -d /etc/apt/keyrings
    KEY_TMP="$(mktemp)"
    SOURCE_TMP="$(mktemp)"
    trap 'rm -f "$KEY_TMP" "$SOURCE_TMP"' RETURN

    curl --fail --silent --show-error --location \
        "https://download.docker.com/linux/${DOCKER_REPOSITORY_FAMILY}/gpg" \
        -o "$KEY_TMP"
    sudo_run install -m 0644 "$KEY_TMP" /etc/apt/keyrings/docker.asc

    cat >"$SOURCE_TMP" <<EOF
Types: deb
URIs: https://download.docker.com/linux/${DOCKER_REPOSITORY_FAMILY}
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    sudo_run install -m 0644 "$SOURCE_TMP" \
        /etc/apt/sources.list.d/docker.sources
    sudo_run apt-get update

    rm -f "$KEY_TMP" "$SOURCE_TMP"
    trap - RETURN
}

resolve_locked_apt_version() {
    local package="${1:?Paquet manquant}"
    local upstream_version="${2:?Version amont manquante}"
    local selected

    selected="$(
        apt-cache madison "$package" | awk '{print $3}' |
            orchestra_select_locked_apt_version "$upstream_version"
    )" || {
        echo "Version amont verrouillée indisponible pour ${package}: ${upstream_version}" >&2
        return 1
    }
    printf '%s\n' "$selected"
    return 0

}

install_docker_engine() {
    CONFLICTS=()
    for package in docker.io docker-compose docker-doc podman-docker containerd runc; do
        if dpkg-query -W -f='${Status}' "$package" 2>/dev/null |
           grep -Fq 'install ok installed'; then
            CONFLICTS+=("$package")
        fi
    done
    if ((${#CONFLICTS[@]})); then
        echo "Paquets Docker incompatibles détectés: ${CONFLICTS[*]}" >&2
        echo "HermesOps refuse de les supprimer automatiquement." >&2
        exit 1
    fi

    configure_docker_repository

    DOCKER_CE_PACKAGE_VERSION="$(resolve_locked_apt_version docker-ce "$DOCKER_CE_VERSION")"
    DOCKER_CLI_PACKAGE_VERSION="$(resolve_locked_apt_version docker-ce-cli "$DOCKER_CLI_VERSION")"
    DOCKER_COMPOSE_PACKAGE_VERSION="$(resolve_locked_apt_version docker-compose-plugin "$DOCKER_COMPOSE_VERSION")"

    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        "docker-ce=${DOCKER_CE_PACKAGE_VERSION}" \
        "docker-ce-cli=${DOCKER_CLI_PACKAGE_VERSION}" \
        containerd.io \
        docker-buildx-plugin \
        "docker-compose-plugin=${DOCKER_COMPOSE_PACKAGE_VERSION}"
    sudo_run systemctl enable --now docker.service containerd.service
}

if ! command -v docker >/dev/null 2>&1; then
    install_docker_engine
elif ! dpkg-query -W -f='${Status}' docker-ce 2>/dev/null |
     grep -Fq 'install ok installed'; then
    echo "Une installation Docker non officielle est présente." >&2
    echo "HermesOps 0.1.0-alpha exige les paquets Docker CE officiels." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    configure_docker_repository
    DOCKER_COMPOSE_PACKAGE_VERSION="$(resolve_locked_apt_version docker-compose-plugin "$DOCKER_COMPOSE_VERSION")"
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        "docker-compose-plugin=${DOCKER_COMPOSE_PACKAGE_VERSION}"
fi

sudo_run systemctl enable --now docker.service containerd.service

ENGINE_VERSION="$(sudo_run docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
COMPOSE_VERSION="$(docker compose version --short 2>/dev/null || true)"
[[ -n "$ENGINE_VERSION" ]] || {
    echo "Docker Engine inaccessible après installation." >&2
    exit 1
}
[[ -n "$COMPOSE_VERSION" ]] || {
    echo "Docker Compose inaccessible après installation." >&2
    exit 1
}

if [[ "$ENGINE_VERSION" != "29.6.1" ]]; then
    echo "WARN: Docker Engine observé $ENGINE_VERSION, version testée 29.6.1."
fi
if [[ "$COMPOSE_VERSION" != "5.3.0" ]]; then
    echo "WARN: Docker Compose observé $COMPOSE_VERSION, version testée 5.3.0."
fi

sudo_run groupadd -f docker
relogin_required() {
    printf 'RELOGIN_REQUIRED\n' >"$STATUS"
    trap - ERR
    echo
    echo "HERMESOPS_INSTALL_RELOGIN_REQUIRED"
    echo "Fermer complètement la session SSH, se reconnecter, puis relancer install.sh."
    echo "Rapport : $REPORT"
    exit 20
}

if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -Fxq docker; then
    sudo_run usermod -aG docker "$TARGET_USER"
    echo "$TARGET_USER a été ajouté au groupe docker."
    relogin_required
fi

if [[ "$(id -u)" == "$TARGET_UID" ]] &&
   ! id -nG | tr ' ' '\n' | grep -Fxq docker; then
    echo "La session courante ne connaît pas encore le groupe docker."
    relogin_required
fi

user_run docker version >/dev/null
"${SOURCE}/validate.sh" --static

for path in \
    "$ROOT" "$ROOT/state" "$ROOT/state/controller" "$ROOT/state/sandboxes" \
    "$ROOT/runtime" "$ROOT/workspaces" "$ROOT/project-data" \
    "$ROOT/backups" "$ROOT/logs"
do
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$path"
done
sudo_run install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_GROUP" \
    "$ROOT/state/hermes-home" "$ROOT/secrets"

BACKUP_DIR="${ROOT}/backups/installations/${STAMP}"
if [[ -d "$REPO/.git" ]]; then
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$BACKUP_DIR"
    user_run git -C "$REPO" bundle create \
        "${BACKUP_DIR}/hermesops-before-install.bundle" --all
    user_run git -C "$REPO" bundle verify \
        "${BACKUP_DIR}/hermesops-before-install.bundle"
    if [[ -f "${ROOT}/state/controller/hermesops.db" ]]; then
        user_run sqlite3 "${ROOT}/state/controller/hermesops.db" \
            ".backup '${BACKUP_DIR}/controller-before-install.sqlite'"
        user_run sqlite3 "${BACKUP_DIR}/controller-before-install.sqlite" \
            'PRAGMA quick_check;' | grep -Fxq ok
    fi
fi

SOURCE_REAL="$(readlink -f "$SOURCE")"
TARGET_REAL="$(readlink -f "$REPO" 2>/dev/null || true)"
if [[ ! -d "$REPO/.git" ]]; then
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$REPO"
    sudo_run rsync -a "$SOURCE/" "$REPO/"
    sudo_run chown -R "$TARGET_USER:$TARGET_GROUP" "$REPO"
elif [[ "$SOURCE_REAL" == "$TARGET_REAL" ]]; then
    :
else
    SOURCE_HEAD="$(git -C "$SOURCE" rev-parse HEAD 2>/dev/null || true)"
    TARGET_HEAD="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$SOURCE_HEAD" != "$TARGET_HEAD" && "$UPGRADE" != 1 ]]; then
        echo "Installation existante divergente." >&2
        echo "Relancer avec --upgrade après revue de $BACKUP_DIR" >&2
        exit 1
    fi
    sudo_run rsync -a --exclude='config/projects.d/*.toml' "$SOURCE/" "$REPO/"
    sudo_run chown -R "$TARGET_USER:$TARGET_GROUP" "$REPO"
fi
[[ "$(cat "${REPO}/VERSION")" == "0.1.0-alpha" ]]

umask 077
API_KEY=""
if [[ -f "${ROOT}/secrets/agent.env" ]]; then
    API_KEY="$(sed -n 's/^API_SERVER_KEY=//p' "${ROOT}/secrets/agent.env" | head -n 1)"
fi
if [[ -z "$API_KEY" ]]; then
    API_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    printf 'API_SERVER_KEY=%s\n' "$API_KEY" >"${ROOT}/secrets/agent.env"
fi
if [[ ! -f "${ROOT}/secrets/webui.env" ]]; then
    WEBUI_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    {
        printf 'HERMES_WEBUI_GATEWAY_API_KEY=%s\n' "$API_KEY"
        printf 'HERMES_WEBUI_PASSWORD=%s\n' "$WEBUI_PASSWORD"
    } >"${ROOT}/secrets/webui.env"
fi

WEBUI_GATEWAY_KEY="$(
    sed -n 's/^HERMES_WEBUI_GATEWAY_API_KEY=//p'         "${ROOT}/secrets/webui.env" | head -n 1
)"
WEBUI_PASSWORD_PRESENT="$(
    sed -n 's/^HERMES_WEBUI_PASSWORD=//p'         "${ROOT}/secrets/webui.env" | head -n 1
)"
[[ ${#API_KEY} -ge 8 ]] || {
    echo "API_SERVER_KEY trop courte." >&2
    exit 1
}
[[ "$WEBUI_GATEWAY_KEY" == "$API_KEY" ]] || {
    echo "Contrat de clé Agent/WebUI incohérent." >&2
    exit 1
}
[[ -n "$WEBUI_PASSWORD_PRESENT" ]] || {
    echo "HERMES_WEBUI_PASSWORD absente." >&2
    exit 1
}

sudo_run chmod 0600 "${ROOT}/secrets/agent.env" "${ROOT}/secrets/webui.env"
sudo_run chown "$TARGET_USER:$TARGET_GROUP" \
    "${ROOT}/secrets/agent.env" "${ROOT}/secrets/webui.env"

user_run env HERMESOPS_ROOT="$ROOT" \
    "${REPO}/scripts/hermesops-controller-session.py" ensure

if [[ -n "$AUTH_FILE" ]]; then
    [[ -f "$AUTH_FILE" ]] || {
        echo "auth.json absent: $AUTH_FILE" >&2
        exit 1
    }
    sudo_run install -m 0600 -o "$TARGET_USER" -g "$TARGET_GROUP" \
        "$AUTH_FILE" "${ROOT}/state/hermes-home/auth.json"
fi

user_run env HERMESOPS_ROOT="$ROOT" HERMES_UID="$TARGET_UID" HERMES_GID="$TARGET_GID" \
    "${REPO}/scripts/hermes-agent-compose.sh" config --quiet

if [[ "$SKIP_START" == 0 ]]; then
    if [[ "$OFFLINE" == 0 ]]; then
        user_run env HERMESOPS_ROOT="$ROOT" HERMES_UID="$TARGET_UID" HERMES_GID="$TARGET_GID" \
            "${REPO}/scripts/hermes-agent-compose.sh" pull
    fi

    user_run env HERMESOPS_ROOT="$ROOT" HERMES_UID="$TARGET_UID" HERMES_GID="$TARGET_GID" \
        "${REPO}/scripts/hermes-agent-compose.sh" up -d sandbox-engine

    health=""
    for _ in $(seq 1 60); do
        health="$(user_run docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' hermesops-sandbox-engine 2>/dev/null || true)"
        [[ "$health" == "healthy" ]] && break
        sleep 2
    done
    [[ "$health" == "healthy" ]] || {
        echo "sandbox-engine non sain." >&2
        exit 1
    }

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
    WORKER_TAG="${WORKER_LOCK[0]}"
    WORKER_ID="${WORKER_LOCK[1]}"
    CURRENT_WORKER_ID="$(user_run docker exec hermesops-sandbox-engine docker image inspect --format '{{.Id}}' "$WORKER_TAG" 2>/dev/null || true)"

    if [[ "$CURRENT_WORKER_ID" != "$WORKER_ID" ]]; then
        if [[ -z "$WORKER_ARCHIVE" ]]; then
            [[ "$OFFLINE" == 0 ]] || {
                echo "Archive worker requise en mode offline." >&2
                exit 1
            }
            ASSET_BASE="https://github.com/Bebet0o/HermesOps/releases/download/v0.1.0-alpha"
            DOWNLOAD_DIR="${ROOT}/runtime/bootstrap"
            sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$DOWNLOAD_DIR"
            WORKER_ARCHIVE="${DOWNLOAD_DIR}/hermesops-worker-sandbox-0.2.tar.gz"
            CHECKSUM_FILE="${WORKER_ARCHIVE}.sha256"
            user_run curl --fail --location --silent --show-error \
                "${ASSET_BASE}/hermesops-worker-sandbox-0.2.tar.gz" -o "$WORKER_ARCHIVE"
            user_run curl --fail --location --silent --show-error \
                "${ASSET_BASE}/hermesops-worker-sandbox-0.2.tar.gz.sha256" -o "$CHECKSUM_FILE"
            (cd "$DOWNLOAD_DIR" && sha256sum -c "$(basename "$CHECKSUM_FILE")")
        fi

        [[ -f "$WORKER_ARCHIVE" ]] || {
            echo "Archive worker absente: $WORKER_ARCHIVE" >&2
            exit 1
        }
        case "$WORKER_ARCHIVE" in
            *.tar.gz|*.tgz)
                gzip -dc "$WORKER_ARCHIVE" |
                    user_run docker exec -i hermesops-sandbox-engine docker image load
                ;;
            *.tar)
                user_run docker exec -i hermesops-sandbox-engine docker image load <"$WORKER_ARCHIVE"
                ;;
            *) echo "Format worker non pris en charge." >&2; exit 1 ;;
        esac
    fi

    CURRENT_WORKER_ID="$(user_run docker exec hermesops-sandbox-engine docker image inspect --format '{{.Id}}' "$WORKER_TAG")"
    [[ "$CURRENT_WORKER_ID" == "$WORKER_ID" ]] || {
        echo "ID worker invalide après chargement." >&2
        exit 1
    }

    user_run env HERMESOPS_ROOT="$ROOT" HERMES_UID="$TARGET_UID" HERMES_GID="$TARGET_GID" \
        "${REPO}/scripts/hermes-agent-compose.sh" up -d

    user_run env HERMESOPS_ROOT="$ROOT" "${REPO}/scripts/hermesops-db.py" migrate
    user_run env HERMESOPS_ROOT="$ROOT" "${REPO}/scripts/hermesops-db.py" integrity
    user_run env HERMESOPS_ROOT="$ROOT" \
    "${REPO}/scripts/hermesops-controller-operator.py" ensure
    user_run env HERMESOPS_ROOT="$ROOT" "${REPO}/scripts/hermesops-roles.py" sync

    if [[ -f "${ROOT}/state/hermes-home/auth.json" ]]; then
        user_run env HERMESOPS_ROOT="$ROOT" \
            "${REPO}/scripts/hermesops-roles.py" verify-profiles
    else
        echo "ATTENTION: auth.json absent; validation des profils IA reportée."
    fi

    user_run env HERMESOPS_ROOT="$ROOT" "${REPO}/scripts/hermesops-registry.py" validate
    user_run env HERMESOPS_ROOT="$ROOT" "${REPO}/scripts/hermesops-registry.py" sync
fi

SYSTEMD_DIR="${TARGET_HOME}/.config/systemd/user"
CONTROLLER_UNIT_TARGET="${SYSTEMD_DIR}/${CONTROLLER_UNIT_NAME}"
CONTROLLER_UNIT_BACKUP="${BACKUP_DIR}/${CONTROLLER_UNIT_NAME}.before-install"
CONSOLE_UNIT_TARGET="${SYSTEMD_DIR}/${CONSOLE_UNIT_NAME}"
CONSOLE_UNIT_BACKUP="${BACKUP_DIR}/${CONSOLE_UNIT_NAME}.before-install"

sudo_run install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_GROUP" "$SYSTEMD_DIR"
sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$BACKUP_DIR"

if [[ -f "$CONTROLLER_UNIT_TARGET" ]]; then
    CONTROLLER_UNIT_EXISTED=1
    sudo_run install -m 0600 \
        -o "$TARGET_USER" -g "$TARGET_GROUP" \
        "$CONTROLLER_UNIT_TARGET" "$CONTROLLER_UNIT_BACKUP"
fi
if user_run systemctl --user is-enabled --quiet \
   "$CONTROLLER_UNIT_NAME" 2>/dev/null; then
    CONTROLLER_UNIT_WAS_ENABLED=1
fi
if user_run systemctl --user is-active --quiet \
   "$CONTROLLER_UNIT_NAME" 2>/dev/null; then
    CONTROLLER_UNIT_WAS_ACTIVE=1
fi

if [[ -f "$CONSOLE_UNIT_TARGET" ]]; then
    CONSOLE_UNIT_EXISTED=1
    sudo_run install -m 0600 \
        -o "$TARGET_USER" -g "$TARGET_GROUP" \
        "$CONSOLE_UNIT_TARGET" "$CONSOLE_UNIT_BACKUP"
fi
if user_run systemctl --user is-enabled --quiet \
   "$CONSOLE_UNIT_NAME" 2>/dev/null; then
    CONSOLE_UNIT_WAS_ENABLED=1
fi
if user_run systemctl --user is-active --quiet \
   "$CONSOLE_UNIT_NAME" 2>/dev/null; then
    CONSOLE_UNIT_WAS_ACTIVE=1
fi

CONTROLLER_UNIT_TOUCHED=1
CONSOLE_UNIT_TOUCHED=1
for unit in "${REPO}"/systemd/user/*.service; do
    sudo_run install -m 0640 -o "$TARGET_USER" -g "$TARGET_GROUP" "$unit" "$SYSTEMD_DIR/"
done
sudo_run loginctl enable-linger "$TARGET_USER"

if [[ "$SKIP_START" == 0 ]]; then
    USER_UNITS=(
        hermesops-supervisor.service
        hermesops-orchestrator.service
        hermesops-notifier.service
        hermesops-controller-api.service
        hermesops-console.service
    )

    user_run systemctl --user daemon-reload
    user_run systemctl --user enable "${USER_UNITS[@]}"

    user_run systemctl --user restart hermesops-supervisor.service
    user_run systemctl --user restart hermesops-orchestrator.service
    user_run systemctl --user restart hermesops-notifier.service
    user_run systemctl --user restart hermesops-controller-api.service
    user_run systemctl --user restart hermesops-console.service

    for unit in "${USER_UNITS[@]}"; do
        user_run systemctl --user is-active --quiet "$unit" || {
            echo "Service utilisateur inactif après installation: $unit" >&2
            exit 1
        }
    done

    user_run env HERMESOPS_ROOT="$ROOT" \
        "${REPO}/scripts/hermesops-controller-probe.py" \
        --base-url http://127.0.0.1:8765 \
        --wait-seconds 30

    user_run \
        "${REPO}/scripts/hermesops-console-probe.py" \
        --base-url http://127.0.0.1:8788 \
        --wait-seconds 30

    if [[ -f "${ROOT}/state/hermes-home/auth.json" ]]; then
        user_run env HERMESOPS_ROOT="$ROOT" "${REPO}/validate.sh" --runtime
    else
        echo "ATTENTION: auth.json absent; les objectifs IA ne fonctionneront pas encore."
    fi
fi

CONTROLLER_UNIT_TOUCHED=0
CONSOLE_UNIT_TOUCHED=0
printf 'FINISHED_SUCCESS\n' >"$STATUS"
trap - ERR
echo "HERMESOPS_INSTALL_PASS"
echo "Rapport : $REPORT"
echo "Statut  : $STATUS"
