#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

ROOT="${ORCHESTRA_ROOT:-/opt/orchestra}"
REPO="${ROOT}/repo"
CONFIG_DIR="${REPO}/config/projects.d"
TEMPLATE_DIR="${REPO}/tests/fixtures/projects"
FIXTURE_ROOT="${ROOT}/workspaces/.fixtures"
DATA_ROOT="${ROOT}/project-data/.fixtures"
PRIMARY="${FIXTURE_ROOT}/transaction-fixture"
SECONDARY="${FIXTURE_ROOT}/transaction-fixture-b"

[[ "${ORCHESTRA_ENABLE_TEST_FIXTURES:-0}" == "1" ]] || {
    cat >&2 <<'EOF'
Refus d'initialiser les fixtures de test.

Cette commande ajoute deux projets désactivés au registre local.
Relancer explicitement avec :

  ORCHESTRA_ENABLE_TEST_FIXTURES=1 \
    /opt/orchestra/repo/scripts/init-test-fixtures.sh
EOF
    exit 2
}

for template in \
    transaction-fixture.toml \
    transaction-fixture-b.toml
do
    [[ -f "${TEMPLATE_DIR}/${template}" ]] || {
        echo "Template absent: ${TEMPLATE_DIR}/${template}" >&2
        exit 1
    }
done

mkdir -p \
    "$CONFIG_DIR" \
    "$FIXTURE_ROOT" \
    "$DATA_ROOT/transaction-fixture" \
    "$DATA_ROOT/transaction-fixture-b"

install -m 0640 \
    "${TEMPLATE_DIR}/transaction-fixture.toml" \
    "${CONFIG_DIR}/transaction-fixture.toml"

install -m 0640 \
    "${TEMPLATE_DIR}/transaction-fixture-b.toml" \
    "${CONFIG_DIR}/transaction-fixture-b.toml"

create_primary() {
    rm -rf "$PRIMARY"
    mkdir -p "$PRIMARY"
    git -C "$PRIMARY" init -b main
    git -C "$PRIMARY" config user.name "Orchestra Fixture"
    git -C "$PRIMARY" config user.email "fixture@orchestra.invalid"
    cat >"${PRIMARY}/README.md" <<'EOF'
# Orchestra Transaction Fixture

Repository used only by Orchestra foundation tests.
No remote is configured.
EOF
    git -C "$PRIMARY" add README.md
    git -C "$PRIMARY" commit \
        -m "fixture: initialize transaction repository"
}

if [[ ! -d "${PRIMARY}/.git" ]]; then
    create_primary
fi

git -C "$PRIMARY" rev-parse --verify HEAD >/dev/null
[[ -z "$(git -C "$PRIMARY" status --porcelain)" ]]

if [[ ! -d "${SECONDARY}/.git" ]]; then
    rm -rf "$SECONDARY"
    git clone --no-hardlinks "$PRIMARY" "$SECONDARY"
    git -C "$SECONDARY" remote remove origin 2>/dev/null || true
fi

git -C "$SECONDARY" rev-parse --verify HEAD >/dev/null
[[ -z "$(git -C "$SECONDARY" status --porcelain)" ]]

for fixture_repo in "$PRIMARY" "$SECONDARY"; do
    while IFS= read -r remote; do
        [[ -n "$remote" ]] || continue
        git -C "$fixture_repo" remote remove "$remote"
    done < <(git -C "$fixture_repo" remote)

    [[ -z "$(git -C "$fixture_repo" status --porcelain)" ]]
done

"${REPO}/scripts/orchestra-registry.py" validate
"${REPO}/scripts/orchestra-registry.py" sync

echo "Orchestra test fixtures: PASS"
echo "Les deux projets de test sont enregistrés avec enabled=false."
