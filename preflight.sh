#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="/opt/orchestra"
LEGACY_ROOT="/opt/docker/hermesops"
TARGET_USER="${USER:-$(id -un)}"
CI_MODE=0

PLATFORM_SUPPORT="${SOURCE}/scripts/platform-support.sh"
if [[ ! -r "$PLATFORM_SUPPORT" ]]; then
    echo "Contrat de plateforme absent: $PLATFORM_SUPPORT" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$PLATFORM_SUPPORT"

while (($#)); do
    case "$1" in
        --target-user)
            TARGET_USER="${2:?Utilisateur manquant}"
            shift 2
            ;;
        --ci)
            CI_MODE=1
            shift
            ;;
        --help|-h)
            cat <<'HELP'
Usage: ./preflight.sh [--target-user USER] [--ci]

Lecture seule. Vérifie Debian 12+ ou Ubuntu 22.04+, amd64, Docker, Compose, dépendances,
ports et contenu public du dépôt.
HELP
            exit 0
            ;;
        *)
            echo "Option inconnue: $1" >&2
            exit 2
            ;;
    esac
done

FAILURES=0
WARNINGS=0
pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

printf 'Orchestra preflight\n'
printf 'Source       : %s\n' "$SOURCE"
printf 'Racine cible : %s\n' "$ROOT"
printf 'Utilisateur  : %s\n\n' "$TARGET_USER"

if [[ "$CI_MODE" == 0 ]]; then
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if orchestra_os_supported "${ID:-}" "${VERSION_ID:-}"; then
            pass "Système pris en charge: ${ID} ${VERSION_ID}"
        else
            fail "Système non pris en charge: ${ID:-inconnu} ${VERSION_ID:-inconnue}; $(orchestra_platform_contract) requis"
        fi
    else
        fail "/etc/os-release absent"
    fi

    ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
    orchestra_arch_supported "$ARCH" \
        && pass "Architecture amd64" \
        || fail "Architecture non prise en charge: $ARCH"
fi

for command_name in \
    bash sha256sum systemctl timeout flock install \
    stat find grep sed awk apt-get apt-cache dpkg-query getent
 do
    command -v "$command_name" >/dev/null 2>&1 \
        && pass "Commande système présente: $command_name" \
        || fail "Commande système absente: $command_name"
done

STATIC_VALIDATION_READY=1

for command_name in git python3 sqlite3 curl rsync gzip; do
    if command -v "$command_name" >/dev/null 2>&1; then
        pass "Dépendance présente: $command_name"
    else
        warn "Dépendance absente; install.sh peut installer: $command_name"
        case "$command_name" in
            python3|sqlite3|rsync)
                STATIC_VALIDATION_READY=0
                ;;
        esac
    fi
done

if command -v runuser >/dev/null 2>&1; then
    pass "Commande système présente: runuser"
else
    warn "runuser absent; install.sh installera le paquet util-linux"
fi

if command -v python3 >/dev/null 2>&1; then
    if python3 - <<'PY' >/dev/null 2>&1
import yaml
PY
    then
        pass "Module Python yaml présent"
    else
        warn "Module Python yaml absent; install.sh installera python3-yaml"
        STATIC_VALIDATION_READY=0
    fi
else
    warn "Contrôle du module yaml reporté"
    STATIC_VALIDATION_READY=0
fi

if [[ "$EUID" != 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    fail "sudo absent pour une installation non-root"
fi

if [[ "$CI_MODE" == 0 ]]; then
    command -v docker >/dev/null 2>&1 || warn "Docker absent; install.sh peut installer Docker CE"
    if command -v docker >/dev/null 2>&1; then
        docker version >/dev/null 2>&1 \
            && pass "Docker Engine accessible" \
            || warn "Docker Engine inaccessible; install.sh tentera de le démarrer"
        docker compose version >/dev/null 2>&1 \
            && pass "Docker Compose disponible" \
            || warn "Plugin Docker Compose absent; install.sh peut l’installer"
    fi

    id "$TARGET_USER" >/dev/null 2>&1 \
        && pass "Utilisateur cible présent" \
        || fail "Utilisateur cible absent: $TARGET_USER"
    if id "$TARGET_USER" >/dev/null 2>&1; then
        TARGET_UID="$(id -u "$TARGET_USER")"
        TARGET_GID="$(id -g "$TARGET_USER")"
        [[ "$TARGET_UID" =~ ^[0-9]+$ && "$TARGET_GID" =~ ^[0-9]+$ ]] \
            && pass "Identité cible numérique: ${TARGET_UID}:${TARGET_GID}" \
            || fail "Identité cible invalide: ${TARGET_UID}:${TARGET_GID}"
        id -nG "$TARGET_USER" | tr ' ' '\n' | grep -Fxq docker \
            && pass "Utilisateur membre du groupe docker" \
            || warn "install.sh ajoutera $TARGET_USER au groupe docker puis demandera une reconnexion"
    fi

    LEGACY_FOUND=()
    [[ ! -e "$LEGACY_ROOT" ]] || LEGACY_FOUND+=("$LEGACY_ROOT")
    for unit in \
        hermesops-controller-api.service hermesops-console.service \
        hermesops-notifier.service hermesops-orchestrator.service \
        hermesops-supervisor.service
    do
        candidate="$(getent passwd "$TARGET_USER" | cut -d: -f6)/.config/systemd/user/${unit}"
        [[ ! -e "$candidate" ]] || LEGACY_FOUND+=("$candidate")
    done
    if ((${#LEGACY_FOUND[@]})); then
        fail "Installation HermesOps historique détectée; migration explicite requise"
    else
        pass "Aucune installation HermesOps historique détectée"
    fi

    for port in 8642 8765 8787 8788; do
        if command -v ss >/dev/null 2>&1 &&
           ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q .; then
            if docker ps --format '{{.Names}}' 2>/dev/null |
                 grep -Eq '^orchestra-(controller|console|hermes-agent|hermes-webui)$'; then
                warn "Port ${port} déjà utilisé par l'installation Orchestra"
            else
                fail "Port ${port} déjà utilisé"
            fi
        else
            pass "Port ${port} disponible"
        fi
    done
fi

for required in \
    README.md compose/orchestra.yaml compose/orchestra.dev.yaml compose/images.lock.env \
    config/controller.toml config/roles.toml migrations \
    console/src console/dist profiles scripts \
    images/orchestra.Dockerfile images/orchestra-runtime.Dockerfile tests
do
    [[ -e "${SOURCE}/${required}" ]] \
        && pass "Présent: ${required}" \
        || fail "Absent: ${required}"
done

if [[ -x "${SOURCE}/scripts/check-secrets.sh" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        "${SOURCE}/scripts/check-secrets.sh" --root "$SOURCE" \
            && pass "Contrôle anti-secrets" \
            || fail "Contrôle anti-secrets"
    else
        warn "Contrôle anti-secrets reporté jusqu’à l’installation de Python"
    fi
else
    fail "scripts/check-secrets.sh absent"
fi

if [[ -x "${SOURCE}/validate.sh" ]]; then
    if [[ "$STATIC_VALIDATION_READY" == "1" ]] &&
       command -v docker >/dev/null 2>&1 &&
       docker compose version >/dev/null 2>&1; then
        "${SOURCE}/validate.sh" --static --quiet \
            && pass "Validation statique complète" \
            || fail "Validation statique complète"
    else
        warn "Validation statique complète reportée après installation des dépendances"
    fi
else
    fail "validate.sh absent"
fi

printf '\nRésumé: failures=%d warnings=%d\n' "$FAILURES" "$WARNINGS"
if ((FAILURES)); then
    echo "ORCHESTRA_PREFLIGHT_FAIL"
    exit 1
fi
echo "ORCHESTRA_PREFLIGHT_PASS"
