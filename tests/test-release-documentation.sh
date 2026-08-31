#!/usr/bin/env bash
    set -Eeuo pipefail
    export LC_ALL=C

    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    README="${REPO}/README.md"
    LICENSE_FILE="${REPO}/LICENSE"
    VERSION_FILE="${REPO}/VERSION"

    [[ -f "$README" ]]
    [[ -f "$LICENSE_FILE" ]]
    [[ -f "$VERSION_FILE" ]]

    [[ "$(tr -d '\r\n' <"$VERSION_FILE")" == "0.1.0-alpha" ]]

    required_readme_text=(
        'Current status: `v0.1.0-alpha` — foundation release'
        '`v0.2.0-beta` — long-term product milestone'
        'It has no committed release date'
        'HermesOps Console'
        'Blueprints'
        '/blueprints'
        'scripts/hermesops-blueprint.py'
        'docs/blueprint/SPECIFICATION_V1.md'
        'temporary compatibility interface'
        'hermesops-worker-sandbox-0.2.tar.gz'
        'hermesops-worker-sandbox-0.2.tar.gz.sha256'
        'HERMESOPS_PREFLIGHT_PASS'
        'HERMESOPS_INSTALL_PASS'
        './uninstall.sh --user "$USER"'
        'Apache License 2.0'
        'HermesOps does not replace Hermes Agent'
    )

    for text in "${required_readme_text[@]}"; do
        grep -Fq "$text" "$README" || {
            echo "README text missing: $text" >&2
            exit 1
        }
    done

    forbidden_current_readme_text=(
        '/hermesfiles'
        'scripts/hermesops-hermesfile.py'
        'docs/hermesfile/SPECIFICATION_V1.md'
        'hermesfile-v1'
    )
    for text in "${forbidden_current_readme_text[@]}"; do
        if grep -Fq "$text" "$README"; then
            echo "README retains legacy current authority: $text" >&2
            exit 1
        fi
    done

    current_blueprint_docs=(
        docs/api/CONTROLLER_API_V1.md
        docs/api/EVENTS_V1.md
        docs/architecture/CONTROLLER_COMPONENTS.md
        docs/architecture/CONTROLLER_CONSOLE_BOUNDARY.md
        docs/console/CONTROLLER_CLIENT.md
        docs/console/FOUNDATION.md
        docs/console/INFORMATION_ARCHITECTURE.md
        docs/console/OPERATIONAL_DASHBOARD.md
        docs/console/PROJECT_LIFECYCLE.md
        docs/blueprint/SPECIFICATION_V1.md
    )
    for relative in "${current_blueprint_docs[@]}"; do
        path="${REPO}/${relative}"
        [[ -f "$path" ]]
        if grep -Eiq 'hermesfile' "$path"; then
            echo "Current documentation retains Hermesfile authority: $relative" >&2
            exit 1
        fi
    done

    grep -Fq 'Apache License' "$LICENSE_FILE"
    grep -Fq 'Version 2.0, January 2004' "$LICENSE_FILE"
    grep -Fq 'http://www.apache.org/licenses/' "$LICENSE_FILE"
    grep -Fq 'END OF TERMS AND CONDITIONS' "$LICENSE_FILE"

    python3 - "$README" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

if len(text.splitlines()) < 300:
    raise SystemExit("README is unexpectedly short")

required_headings = (
    "# Orchestra",
    "## Release direction",
    "## Architecture",
    "## Roles",
    "## Worker image, archive, engine, and containers",
    "## Orchestra Blueprint",
    "## Security model",
    "## Installation",
    "## Current limitations",
    "## Roadmap",
    "## License",
)

for heading in required_headings:
    if heading not in text:
        raise SystemExit(f"Missing README heading: {heading}")

future = text.index("### `v0.2.0-beta`")
limitations = text.index("## Current limitations")
if future > limitations:
    raise SystemExit("Future milestone positioning is missing from introduction")

if "strictly parse, validate, canonicalize and fingerprint\nBlueprint v1 sources" not in text:
    raise SystemExit("README must describe the implemented Blueprint v1 source tooling")

print("HermesOps release documentation structure: PASS")
PY

    echo "HermesOps Apache-2.0 license: PASS"
    echo "HermesOps release documentation: PASS"
