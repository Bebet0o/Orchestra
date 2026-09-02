#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$REPO" <<'PY'
from pathlib import Path
import sys
import tomllib
import yaml

root = Path(sys.argv[1])
accepted_digest = "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
accepted_reference = "ghcr.io/bebet0o/orchestra-worker@" + accepted_digest

with (root / "config/environments/default-worker.toml").open("rb") as stream:
    worker = tomllib.load(stream)
if worker.get("status") != "published":
    raise SystemExit("Default worker is not published")
if worker.get("oci_digest") != accepted_digest:
    raise SystemExit("Accepted worker digest drifted")
if worker.get("image_reference") != accepted_reference:
    raise SystemExit("Accepted worker reference drifted")

compose = yaml.safe_load((root / "compose/agent.yaml").read_text(encoding="utf-8"))
services = compose.get("services", {})
expected_services = {
    "sandbox-engine",
    "hermes-agent",
    "hermes-webui",
    "controller",
    "console",
    "supervisor",
    "orchestrator",
    "notifier",
}
if set(services) != expected_services:
    raise SystemExit(f"Compose service set mismatch: {sorted(services)}")

application_services = {"controller", "console", "supervisor", "orchestrator", "notifier"}
application_images = {services[name].get("image") for name in application_services}
if len(application_images) != 1:
    raise SystemExit("Control-plane services do not share one application image")
for name in application_services:
    service = services[name]
    if service.get("user") != "${ORCHESTRA_UID:?ORCHESTRA_UID is required}:${ORCHESTRA_GID:?ORCHESTRA_GID is required}":
        raise SystemExit(f"{name} does not use the target numeric identity")
    if service.get("restart") != "unless-stopped":
        raise SystemExit(f"{name} restart policy mismatch")

host_sockets = {"/var/run/docker.sock", "/run/docker.sock"}
host_socket_mounts = []
private_socket_consumers = set()
for name, service in services.items():
    for volume in service.get("volumes", []) or []:
        rendered = str(volume)
        source = rendered.split(":", 1)[0]
        destination = rendered.split(":", 2)[1] if ":" in rendered else ""
        if source in host_sockets or destination in host_sockets:
            host_socket_mounts.append((name, rendered))
        if "/run/orchestra-docker" in rendered:
            private_socket_consumers.add(name)
if host_socket_mounts:
    raise SystemExit(f"Host Docker socket mounts remain: {host_socket_mounts}")
if private_socket_consumers != {
    "sandbox-engine",
    "hermes-agent",
    "supervisor",
    "orchestrator",
}:
    raise SystemExit(
        f"Private socket consumer set mismatch: {sorted(private_socket_consumers)}"
    )

retired = (
    "VERSION",
    "config/worker-sandbox.lock.toml",
    "images/worker-sandbox.Dockerfile",
    "scripts/export-worker-image.sh",
    "scripts/legacy_worker_environment.py",
    "scripts/hermes-sandbox-status.sh",
    "docs/hermesfile/SPECIFICATION_V0.md",
    "specs/hermesfile-v0.schema.json",
    "systemd/.gitkeep",
    "tests/test-systemd-user-boot-order.sh",
    "tests/test-controller-service-lifecycle.sh",
    "tests/test-controller-service-persistence.sh",
)
for relative in retired:
    if (root / relative).exists():
        raise SystemExit(f"Retired distribution artifact remains: {relative}")
if not (root / "scripts/orchestra-sandbox-status.sh").is_file():
    raise SystemExit("Current Orchestra sandbox status helper is missing")
if list((root / "systemd/user").glob("*.service")):
    raise SystemExit("Application user-systemd units remain")

active_lifecycle = "\n".join(
    (root / name).read_text(encoding="utf-8")
    for name in ("install.sh", "preflight.sh", "uninstall.sh", "validate.sh")
)
for forbidden in (
    "systemctl --user",
    "enable-linger",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "--worker-image-archive",
    "--offline",
    "1000:1000",
    "0.1.0-alpha",
):
    if forbidden in active_lifecycle:
        raise SystemExit(f"Retired lifecycle contract remains: {forbidden}")

installer = (root / "install.sh").read_text(encoding="utf-8")
if 'ORCHESTRA_ROOT="/opt/orchestra"' not in installer:
    raise SystemExit("Installer root authority mismatch")
if 'TARGET_UID="$(id -u "$TARGET_USER")"' not in installer:
    raise SystemExit("Installer does not derive the target UID")
if 'TARGET_GID="$(id -g "$TARGET_USER")"' not in installer:
    raise SystemExit("Installer does not derive the target GID")
if installer.index("LEGACY_FOUND=()") > installer.index("REPORT_DIR="):
    raise SystemExit("Legacy installation gate occurs after mutation setup")

uninstaller = (root / "uninstall.sh").read_text(encoding="utf-8")
if 'REMOVE_REPO=0' not in uninstaller:
    raise SystemExit("Uninstaller does not preserve state by default")
if '[[ "$CONFIRM" == "REMOVE_REPO" ]]' not in uninstaller:
    raise SystemExit("Repository removal is not confirmation-gated")

sandbox_sources = "\n".join(
    (root / relative).read_text(encoding="utf-8")
    for relative in (
        "scripts/sandbox_backend.py",
        "scripts/agent_runtime/hermes.py",
        "scripts/orchestra-worker.py",
        "scripts/orchestra-reviewer.py",
        "scripts/orchestra-orchestrator.py",
        "scripts/orchestra-recovery.py",
    )
)
if "unix:///run/orchestra-docker/docker.sock" not in sandbox_sources:
    raise SystemExit("Private DIND authority missing")
if '"docker", "exec", "orchestra-sandbox-engine"' in sandbox_sources:
    raise SystemExit("Host-daemon docker exec bridge remains")
for forbidden in (
    "LegacyLocalEnvironment",
    "LegacyPreparedEnvironment",
    "prepare_legacy_environment",
    "worker-sandbox.lock.toml",
):
    if forbidden in sandbox_sources:
        raise SystemExit(f"Legacy worker path remains: {forbidden}")

old_entrypoints = sorted((root / "scripts").glob("hermesops-*"))
if old_entrypoints:
    raise SystemExit(f"Old current entrypoints remain: {old_entrypoints}")

print("ORCHESTRA_DISTRIBUTION_CONTRACT_PASS")
PY
