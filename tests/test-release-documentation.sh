#!/usr/bin/env bash
    set -Eeuo pipefail
    export LC_ALL=C

    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    README="${REPO}/README.md"
    CHANGELOG="${REPO}/CHANGELOG.md"
    SECURITY="${REPO}/SECURITY.md"
    LICENSE_FILE="${REPO}/LICENSE"
    VERSION_FILE="${REPO}/VERSION"

    [[ -f "$README" ]]
    [[ -f "$CHANGELOG" ]]
    [[ -f "$SECURITY" ]]
    [[ -f "$LICENSE_FILE" ]]
    [[ -f "$VERSION_FILE" ]]

    [[ "$(tr -d '\r\n' <"$VERSION_FILE")" == "0.1.0-alpha" ]]

    required_readme_text=(
        'Orchestra is under active development'
        'not yet a production-ready autonomous agent platform'
        'Hermes Agent is an upstream integration'
        'AgentRuntime'
        'HermesRuntime'
        'NativeRuntime'
        'ModelProvider'
        'repository@sha256:digest'
        'blueprint-v1'
        'scripts/hermesops-blueprint.py'
        'docs/blueprint/SPECIFICATION_V1.md'
        'specs/blueprint-v1.schema.json'
        'config/examples/Blueprint'
        './install.sh --user "$USER"'
        './validate.sh --static --quiet'
        'SECURITY.md'
        'CONTRIBUTING.md'
        'Apache License 2.0'
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

    grep -Fq '### HermesOps 0.2.0 (historical release)' "$CHANGELOG"
    ! grep -Fq 'HermesOps 0.2.0 development (unreleased historical material)' \
        "$CHANGELOG"
    grep -Fq 'working transitional deployment path' "$README"
    grep -Fq 'and service layout is inherited from HermesOps' "$README"
    grep -Fq 'GitHub private vulnerability reporting is not currently enabled' \
        "$SECURITY"

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

if len(text.splitlines()) < 150:
    raise SystemExit("README is unexpectedly short")

required_headings = (
    "# Orchestra",
    "## What is Orchestra?",
    "## Why Orchestra?",
    "## Current status",
    "## Architecture",
    "## Orchestra Blueprint",
    "## Getting started",
    "## Validation and tests",
    "## Repository layout",
    "## Security",
    "## Contributing",
    "## Roadmap",
    "## License",
)

for heading in required_headings:
    if heading not in text:
        raise SystemExit(f"Missing README heading: {heading}")

if text.index("## Current status") > text.index("## Architecture"):
    raise SystemExit("Current maturity must be established before architecture")
if "Foundation-only or planned work includes" not in text:
    raise SystemExit("README must distinguish foundations from planned work")
if "Blueprint lifecycle operations produce and version the existing\n`SandboxProfile` identity" not in text:
    raise SystemExit("README must describe the Blueprint/SandboxProfile identity model")

print("Orchestra public documentation structure: PASS")
PY

    echo "Orchestra Apache-2.0 license: PASS"
    echo "Orchestra release documentation: PASS"
