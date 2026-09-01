#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"
cd "$REPO"

for file in \
    compose/agent.yaml \
    images/orchestra-control-plane.Dockerfile \
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
compose = yaml.safe_load((root / "compose/agent.yaml").read_text(encoding="utf-8"))
controller = compose["services"]["controller"]
expected = [
    "python3",
    "/opt/orchestra/repo/scripts/orchestra-controller-api.py",
    "serve",
    "--host",
    "127.0.0.1",
    "--port",
    "8765",
    "--log-level",
    "INFO",
]
if controller["command"] != expected:
    raise SystemExit(f"Controller Compose command mismatch: {controller['command']!r}")
if controller.get("network_mode") != "host":
    raise SystemExit("Controller does not preserve loopback host networking")
if controller.get("restart") != "unless-stopped":
    raise SystemExit("Controller restart policy mismatch")
if controller.get("read_only") is not True:
    raise SystemExit("Controller root filesystem is not read-only")
if "no-new-privileges:true" not in controller.get("security_opt", []):
    raise SystemExit("Controller no-new-privileges policy missing")
if not controller.get("healthcheck"):
    raise SystemExit("Controller healthcheck missing")

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
if '"${REPO}/scripts/orchestra-compose.sh" up -d' not in installer:
    raise SystemExit("Installer does not start the Compose-owned application")
if '"${REPO}/scripts/orchestra-controller-probe.py"' not in installer:
    raise SystemExit("Installer Controller readiness probe missing")

uninstaller = (root / "uninstall.sh").read_text(encoding="utf-8")
if '"${REPO}/scripts/orchestra-compose.sh" down' not in uninstaller:
    raise SystemExit("Uninstaller does not stop the Compose-owned application")
if 'rm -f "${ROOT}/secrets/controller-session"' in uninstaller:
    raise SystemExit("Conservative uninstall deletes the Controller session")

print("Orchestra Controller Compose service contract: PASS")
PY

echo "ORCHESTRA_CONTROLLER_SERVICE_CONTRACT_PASS"
