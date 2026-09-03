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

if [[ -f "$INSTALL_ROOT/orchestra.yaml" && -f "$INSTALL_ROOT/orchestra.env" ]]; then
    sudo_run docker compose --project-directory "$INSTALL_ROOT" \
        --env-file "$INSTALL_ROOT/orchestra.env" \
        -f "$INSTALL_ROOT/orchestra.yaml" down --remove-orphans
fi

if [[ "$REMOVE_DATA" == 1 ]]; then
    [[ "$CONFIRM" == "REMOVE_DATA" ]] || {
        echo "Data removal refused; use --confirm REMOVE_DATA." >&2
        exit 1
    }
    [[ "$INSTALL_ROOT/data" == "/opt/orchestra/data" && ! -L "$INSTALL_ROOT/data" ]] || {
        echo "Data removal target refused." >&2
        exit 1
    }
    sudo_run rm -rf /opt/orchestra/data
    echo "Persistent Orchestra data removed. This cannot be recovered without a backup."
else
    echo "Persistent data, secrets, projects, workspaces, and backups were preserved."
fi

echo "ORCHESTRA_UNINSTALL_PASS"
