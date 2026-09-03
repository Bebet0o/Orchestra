#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"
cd "$REPO"

for file in \
    compose/orchestra.yaml \
    images/orchestra.Dockerfile \
    controller_api/service_support.py \
    scripts/orchestra-controller-session.py \
    scripts/orchestra-controller-probe.py \
    scripts/orchestra-controller-api.py \
    tests/test_controller_service.py
do
    [[ -f "$file" ]]
done

python3 tests/test_controller_service.py >/dev/null

python3 - "$REPO" <<'PY'
from pathlib import Path
import sys
import yaml

root = Path(sys.argv[1])
compose = yaml.safe_load((root / "compose/orchestra.yaml").read_text(encoding="utf-8"))
controller = compose["services"]["orchestra"]
if controller.get("privileged") is True:
    raise SystemExit("Control-plane appliance is privileged")
if controller.get("restart") != "unless-stopped":
    raise SystemExit("Control-plane restart policy mismatch")
if controller.get("read_only") is not True:
    raise SystemExit("Control-plane root filesystem is not read-only")
if "no-new-privileges:true" not in controller.get("security_opt", []):
    raise SystemExit("Control-plane no-new-privileges policy missing")
if controller.get("cap_drop") != ["ALL"]:
    raise SystemExit("Control-plane capabilities are not dropped")
if not controller.get("healthcheck"):
    raise SystemExit("Control-plane healthcheck missing")

appliance = (root / "scripts/orchestra-appliance.py").read_text(encoding="utf-8")
for token in (
    'processes["controller"] = spawn',
    '"--host", "127.0.0.1", "--port", "8765"',
    'scripts/orchestra-controller-probe.py',
):
    if token not in appliance:
        raise SystemExit(f"Controller appliance contract missing: {token}")

for retired in (
    root / "systemd/user/hermesops-controller-api.service",
    root / "systemd/user/orchestra-controller-api.service",
):
    if retired.exists():
        raise SystemExit(f"Retired application unit remains: {retired}")

for name in ("install.sh", "uninstall.sh", "validate.sh"):
    source = (root / name).read_text(encoding="utf-8")
    if "systemctl --user" in source:
        raise SystemExit(f"Application user-systemd usage remains in {name}")

installer = (root / "install.sh").read_text(encoding="utf-8")
if '"${compose[@]}" up -d' not in installer:
    raise SystemExit("Installer does not start the Compose-owned application")
if "did not become healthy" not in installer:
    raise SystemExit("Installer appliance readiness wait missing")

uninstaller = (root / "uninstall.sh").read_text(encoding="utf-8")
if 'down --remove-orphans' not in uninstaller:
    raise SystemExit("Uninstaller does not stop the Compose-owned application")
if 'rm -f "${ROOT}/secrets/controller-session"' in uninstaller:
    raise SystemExit("Conservative uninstall deletes the Controller session")

print("Orchestra Controller Compose service contract: PASS")
PY

echo "ORCHESTRA_CONTROLLER_SERVICE_CONTRACT_PASS"
