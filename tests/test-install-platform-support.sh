#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "${REPO}/scripts/platform-support.sh"

pass_platforms=(
    'debian 12 amd64'
    'debian 13 amd64'
    'ubuntu 22.04 amd64'
    'ubuntu 24.04 amd64'
    'ubuntu 26.10 x86_64'
)
fail_platforms=(
    'debian 11 amd64'
    'ubuntu 20.04 amd64'
    'debian 12 arm64'
    'ubuntu 24.04 aarch64'
    'fedora 42 amd64'
    'debian malformed amd64'
)

for platform in "${pass_platforms[@]}"; do
    read -r os_id version_id architecture <<<"$platform"
    orchestra_platform_supported "$os_id" "$version_id" "$architecture"
done

for platform in "${fail_platforms[@]}"; do
    read -r os_id version_id architecture <<<"$platform"
    if orchestra_platform_supported "$os_id" "$version_id" "$architecture"; then
        echo "Unsupported platform accepted: $platform" >&2
        exit 1
    fi
done

[[ "$(orchestra_docker_repository_family debian)" == "debian" ]]
[[ "$(orchestra_docker_repository_family ubuntu)" == "ubuntu" ]]
if orchestra_docker_repository_family fedora >/dev/null 2>&1; then
    echo "Unsupported Docker repository family accepted" >&2
    exit 1
fi

debian_package='5:29.6.1-1~debian.12~bookworm'
ubuntu_package='5:29.6.1-1~ubuntu.24.04~noble'
[[ "$(printf '%s\n' "$debian_package" | orchestra_select_locked_apt_version 29.6.1)" == "$debian_package" ]]
[[ "$(printf '%s\n' "$ubuntu_package" | orchestra_select_locked_apt_version 29.6.1)" == "$ubuntu_package" ]]
if printf '%s\n' '5:28.0.0-1~ubuntu.24.04~noble' |
   orchestra_select_locked_apt_version 29.6.1 >/dev/null
then
    echo "Wrong upstream Docker package version accepted" >&2
    exit 1
fi

grep -Fq 'linux/${DOCKER_REPOSITORY_FAMILY}/gpg' "${REPO}/install.sh"
grep -Fq 'linux/${DOCKER_REPOSITORY_FAMILY}' "${REPO}/install.sh"
grep -Fq 'Debian 12+ or Ubuntu 22.04+ on amd64' "${REPO}/README.md"
grep -Fq 'Debian 12 ou version ultérieure' "${REPO}/docs/PUBLIC_INSTALLATION.md"
grep -Fq 'Ubuntu 22.04 ou version ultérieure' "${REPO}/docs/PUBLIC_INSTALLATION.md"

echo "Orchestra public install platform matrix: PASS"
