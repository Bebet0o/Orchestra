#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

INSTALL_ROOT="/opt/orchestra"
REMOVE_DATA=0
CONFIRM=""

while (($#)); do
    case "$1" in
        --remove-data) REMOVE_DATA=1; shift ;;
        --confirm) CONFIRM="${2:-}"; shift 2 ;;
        -h|--help)
            echo "Usage: uninstall.sh [--remove-data --confirm REMOVE_DATA]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

sudo_run() { [[ "$EUID" == 0 ]] && "$@" || sudo "$@"; }

installation_state="$({
    sudo_run sh -eu -c '
        root=$1
        compose_file=$root/orchestra.yaml
        environment_file=$root/orchestra.env

        if [ -f "$compose_file" ] && [ -f "$environment_file" ]; then
            printf "%s\n" installed
        elif [ ! -e "$root" ] && [ ! -L "$root" ]; then
            printf "%s\n" absent
        else
            printf "%s\n" incomplete
        fi
    ' sh "$INSTALL_ROOT"
} 2>/dev/null)" || {
    echo "Unable to inspect the protected Orchestra installation." >&2
    exit 1
}

case "$installation_state" in
    absent|installed) ;;
    incomplete)
        echo "Orchestra installation exists but its deployment files are incomplete." >&2
        exit 1
        ;;
    *)
        echo "Unable to determine the protected Orchestra installation state." >&2
        exit 1
        ;;
esac

if [[ "$installation_state" == "installed" ]]; then
    sudo_run docker compose --project-directory "$INSTALL_ROOT" \
        --env-file "$INSTALL_ROOT/orchestra.env" \
        -f "$INSTALL_ROOT/orchestra.yaml" down --remove-orphans

    remaining_containers="$(sudo_run docker compose --project-directory "$INSTALL_ROOT" \
        --env-file "$INSTALL_ROOT/orchestra.env" \
        -f "$INSTALL_ROOT/orchestra.yaml" ps --all --quiet \
        orchestra orchestra-runtime)" || {
        echo "Unable to verify Orchestra service teardown." >&2
        exit 1
    }
    [[ -z "$remaining_containers" ]] || {
        echo "Orchestra service containers remain after teardown." >&2
        exit 1
    }
fi

if [[ "$REMOVE_DATA" == 1 ]]; then
    [[ "$CONFIRM" == "REMOVE_DATA" ]] || {
        echo "Data removal refused; use --confirm REMOVE_DATA." >&2
        exit 1
    }
    [[ "$INSTALL_ROOT/data" == "/opt/orchestra/data" ]] || {
        echo "Data removal target refused." >&2
        exit 1
    }
    sudo_run test ! -L "$INSTALL_ROOT/data" || {
        echo "Data removal target refused." >&2
        exit 1
    }
    sudo_run rm -rf /opt/orchestra/data
    sudo_run test ! -e /opt/orchestra/data || {
        echo "Persistent Orchestra data removal could not be verified." >&2
        exit 1
    }
    echo "Persistent Orchestra data removed. This cannot be recovered without a backup."
else
    echo "Persistent data, secrets, projects, workspaces, and backups were preserved."
fi

echo "ORCHESTRA_UNINSTALL_PASS"
