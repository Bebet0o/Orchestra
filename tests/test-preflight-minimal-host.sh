#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT="${REPO}/preflight.sh"
INSTALLER="${REPO}/install.sh"
PLATFORM_SUPPORT="${REPO}/scripts/platform-support.sh"

[[ -f "$PLATFORM_SUPPORT" ]]

grep -Fq \
    'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    "$PREFLIGHT"

grep -Fq 'STATIC_VALIDATION_READY=0' "$PREFLIGHT"

grep -Fq \
    'Validation statique complète reportée après installation des dépendances' \
    "$PREFLIGHT"

if grep -A3 'getent' "$PREFLIGHT" |
   grep -Eq 'getent[[:space:]]+runuser'
then
    echo "runuser reste un prérequis système dur." >&2
    exit 1
fi

grep -Fq 'sqlite3 util-linux)' "$INSTALLER"

grep -Fq \
    'runuser reste absent après installation de util-linux' \
    "$INSTALLER"

grep -Fxq 'config/projects.d/*.toml' "${REPO}/.gitignore"

if grep -Fxq     '!config/projects.d/transaction-fixture.toml'     "${REPO}/.gitignore"
then
    echo "Exception fixture interdite dans .gitignore." >&2
    exit 1
fi

if grep -Fxq     '!config/projects.d/transaction-fixture-b.toml'     "${REPO}/.gitignore"
then
    echo "Exception fixture B interdite dans .gitignore." >&2
    exit 1
fi

TMP_GITIGNORE_REPO="$(mktemp -d)"
cleanup_gitignore_test() {
    rm -rf "$TMP_GITIGNORE_REPO"
}
trap cleanup_gitignore_test EXIT

git -C "$TMP_GITIGNORE_REPO" init -q
cp "${REPO}/.gitignore" "${TMP_GITIGNORE_REPO}/.gitignore"
mkdir -p "${TMP_GITIGNORE_REPO}/config/projects.d"

for relative_path in \
    config/projects.d/example-local.toml \
    config/projects.d/transaction-fixture.toml \
    config/projects.d/transaction-fixture-b.toml
do
    touch "${TMP_GITIGNORE_REPO}/${relative_path}"
    git -C "$TMP_GITIGNORE_REPO" check-ignore -q --no-index \
        "$relative_path"
done

cleanup_gitignore_test
trap - EXIT

bash -n "$PREFLIGHT"
bash -n "$INSTALLER"
bash -n "$PLATFORM_SUPPORT"

echo "HermesOps minimal-host preflight contract: PASS"
