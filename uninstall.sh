#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

ORCHESTRA_ROOT="/opt/orchestra"
LEGACY_ROOT="/opt/docker/hermesops"
REPO="${ORCHESTRA_ROOT}/repo"
TARGET_USER=""
REMOVE_REPO=0
CONFIRM=""

while (($#)); do
    case "$1" in
        --user) TARGET_USER="${2:?Utilisateur manquant}"; shift 2 ;;
        --remove-repo) REMOVE_REPO=1; shift ;;
        --confirm) CONFIRM="${2:-}"; shift 2 ;;
        -h|--help)
            echo "Usage: ./uninstall.sh [--user USER] [--remove-repo --confirm REMOVE_REPO]"
            exit 0
            ;;
        *) echo "Option inconnue: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$TARGET_USER" ]]; then
    [[ "$EUID" == 0 ]] && TARGET_USER="${SUDO_USER:-}" || TARGET_USER="$(id -un)"
fi
[[ -n "$TARGET_USER" ]] || { echo "Préciser --user USER." >&2; exit 1; }
id "$TARGET_USER" >/dev/null
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

# Never let current uninstall logic manage an old HermesOps installation.
LEGACY_FOUND=()
[[ ! -e "$LEGACY_ROOT" ]] || LEGACY_FOUND+=("$LEGACY_ROOT")
for unit in \
    hermesops-controller-api.service hermesops-console.service \
    hermesops-notifier.service hermesops-orchestrator.service \
    hermesops-supervisor.service
do
    candidate="${TARGET_HOME}/.config/systemd/user/${unit}"
    [[ ! -e "$candidate" ]] || LEGACY_FOUND+=("$candidate")
done
if ((${#LEGACY_FOUND[@]})); then
    echo "Installation HermesOps historique détectée; aucune mutation effectuée." >&2
    printf '  %s\n' "${LEGACY_FOUND[@]}" >&2
    echo "Utiliser une procédure de migration/suppression historique explicite." >&2
    exit 1
fi

[[ "$ORCHESTRA_ROOT" == "/opt/orchestra" && ! -L "$ORCHESTRA_ROOT" ]] || {
    echo "Racine Orchestra non canonique ou lien symbolique refusé." >&2
    exit 1
}

sudo_run() { [[ "$EUID" == 0 ]] && "$@" || sudo "$@"; }
user_run() {
    if [[ "$(id -u)" == "$TARGET_UID" ]]; then
        "$@"
    elif [[ "$EUID" == 0 ]]; then
        runuser -u "$TARGET_USER" -- env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" "$@"
    else
        sudo -u "$TARGET_USER" env \
            HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" "$@"
    fi
}

if [[ -x "${REPO}/scripts/orchestra-compose.sh" ]]; then
    user_run env ORCHESTRA_ROOT="$ORCHESTRA_ROOT" \
        ORCHESTRA_UID="$TARGET_UID" ORCHESTRA_GID="$TARGET_GID" \
        "${REPO}/scripts/orchestra-compose.sh" down --remove-orphans
fi

if [[ "$REMOVE_REPO" == 1 ]]; then
    [[ "$CONFIRM" == "REMOVE_REPO" ]] || {
        echo "Suppression refusée. Utiliser --confirm REMOVE_REPO." >&2
        exit 1
    }
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    BACKUP="${ORCHESTRA_ROOT}/backups/uninstall-${STAMP}"
    sudo_run install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" "$BACKUP"
    if [[ -d "$REPO/.git" ]]; then
        user_run git -C "$REPO" bundle create \
            "${BACKUP}/orchestra-before-uninstall.bundle" --all
        user_run git -C "$REPO" bundle verify \
            "${BACKUP}/orchestra-before-uninstall.bundle"
    fi
    [[ "$REPO" == "/opt/orchestra/repo" && ! -L "$REPO" ]] || {
        echo "Cible de suppression du dépôt refusée." >&2
        exit 1
    }
    sudo_run rm -rf /opt/orchestra/repo
fi

echo "ORCHESTRA_UNINSTALL_PASS"
echo "État, secrets, workspaces, données projet et backups conservés."
