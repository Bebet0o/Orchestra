#!/usr/bin/env bash

# Shared public host-platform contract for preflight and installation.

orchestra_os_supported() {
    local os_id="${1:-}"
    local version_id="${2:-}"
    local major minor

    [[ "$version_id" =~ ^[0-9]+([.][0-9]+)*$ ]] || return 1
    major="${version_id%%.*}"
    case "$os_id" in
        debian)
            ((10#$major >= 12))
            ;;
        ubuntu)
            minor=0
            if [[ "$version_id" == *.* ]]; then
                minor="${version_id#*.}"
                minor="${minor%%.*}"
            fi
            ((10#$major > 22 || (10#$major == 22 && 10#$minor >= 4)))
            ;;
        *)
            return 1
            ;;
    esac
}

orchestra_arch_supported() {
    case "${1:-}" in
        amd64|x86_64) return 0 ;;
        *) return 1 ;;
    esac
}

orchestra_platform_supported() {
    orchestra_os_supported "${1:-}" "${2:-}" &&
        orchestra_arch_supported "${3:-}"
}

orchestra_platform_contract() {
    printf '%s\n' 'Debian 12+ or Ubuntu 22.04+, amd64'
}

orchestra_docker_repository_family() {
    local os_id="${1:-}"
    case "$os_id" in
        debian|ubuntu) printf '%s\n' "$os_id" ;;
        *) return 1 ;;
    esac
}

orchestra_select_locked_apt_version() {
    local upstream_version="${1:-}"
    local candidate normalized selected=""

    [[ "$upstream_version" =~ ^[0-9]+([.][0-9]+)+$ ]] || return 1
    while IFS= read -r candidate; do
        normalized="${candidate#*:}"
        if [[ "$normalized" == "${upstream_version}-"* ]]; then
            if [[ -n "$selected" && "$candidate" != "$selected" ]]; then
                return 1
            fi
            selected="$candidate"
        fi
    done
    [[ -n "$selected" ]] || return 1
    printf '%s\n' "$selected"
}
