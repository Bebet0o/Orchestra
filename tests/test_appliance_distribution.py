from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose/orchestra.yaml"
COMPOSE = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
HOST_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}


class ApplianceDistributionTest(unittest.TestCase):
    def test_public_service_inventory_is_exact(self) -> None:
        self.assertEqual(set(SERVICES), {"orchestra", "orchestra-runtime"})

    def test_only_runtime_is_privileged(self) -> None:
        self.assertIsNot(SERVICES["orchestra"].get("privileged"), True)
        self.assertIs(SERVICES["orchestra-runtime"].get("privileged"), True)
        self.assertEqual(
            [name for name, service in SERVICES.items() if service.get("privileged")],
            ["orchestra-runtime"],
        )

    def test_control_plane_is_restricted(self) -> None:
        service = SERVICES["orchestra"]
        self.assertIs(service["read_only"], True)
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["cap_drop"], ["ALL"])

    def test_no_host_docker_socket_mounts(self) -> None:
        for name, service in SERVICES.items():
            for mount in service.get("volumes", []):
                fields = str(mount).split(":")
                self.assertFalse(HOST_SOCKETS.intersection(fields), (name, mount))

    def test_private_socket_is_shared(self) -> None:
        for name in SERVICES:
            mounts = "\n".join(SERVICES[name]["volumes"])
            self.assertIn("orchestra-runtime-socket:/run/orchestra-docker", mounts)
        self.assertEqual(
            SERVICES["orchestra"]["environment"]["DOCKER_HOST"],
            "unix:///run/orchestra-docker/docker.sock",
        )

    def test_canonical_data_root_is_shared(self) -> None:
        for name in SERVICES:
            mounts = "\n".join(SERVICES[name]["volumes"])
            self.assertIn(":/var/lib/orchestra", mounts)
        self.assertEqual(
            SERVICES["orchestra"]["environment"]["ORCHESTRA_DATA_ROOT"],
            "/var/lib/orchestra",
        )

    def test_public_compose_has_no_build_or_fixed_names(self) -> None:
        for service in SERVICES.values():
            self.assertNotIn("build", service)
            self.assertNotIn("container_name", service)

    def test_public_compose_uses_publishable_images(self) -> None:
        self.assertIn("ghcr.io/bebet0o/orchestra:v0.1.0", SERVICES["orchestra"]["image"])
        self.assertIn("ghcr.io/bebet0o/orchestra-runtime:v0.1.0", SERVICES["orchestra-runtime"]["image"])
        self.assertNotIn(":latest", COMPOSE_PATH.read_text(encoding="utf-8"))

    def test_worker_authority_is_exact(self) -> None:
        self.assertEqual(
            SERVICES["orchestra-runtime"]["environment"]["ORCHESTRA_WORKER_IMAGE"],
            "ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49",
        )

    def test_hermes_images_are_immutable_and_internal(self) -> None:
        environment = SERVICES["orchestra-runtime"]["environment"]
        pattern = re.compile(r"^[^@]+@sha256:[0-9a-f]{64}$")
        self.assertRegex(environment["HERMES_AGENT_IMAGE"], pattern)
        self.assertRegex(environment["HERMES_WEBUI_IMAGE"], pattern)
        runtime = (ROOT / "scripts/orchestra-runtime-entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("orchestra-hermes-agent", runtime)
        self.assertIn("orchestra-hermes-webui", runtime)

    def test_appliance_supervises_all_mandatory_processes(self) -> None:
        source = (ROOT / "scripts/orchestra-appliance.py").read_text(encoding="utf-8")
        for component in ("controller", "console", "supervisor", "orchestrator", "notifier"):
            self.assertIn(f'processes["{component}"] = spawn', source)
        self.assertIn("os.killpg(process.pid, signal.SIGTERM)", source)
        self.assertIn("mandatory component", source)

    def test_health_is_product_readiness(self) -> None:
        source = (ROOT / "scripts/orchestra-appliance.py").read_text(encoding="utf-8")
        self.assertIn("scripts/orchestra-controller-probe.py", source)
        self.assertIn("http://127.0.0.1:8080/", source)
        self.assertIn("host=PUBLIC_HOST", source)
        self.assertIn('["docker", "info"]', source)
        self.assertIn("healthcheck", SERVICES["orchestra"])
        self.assertIn("healthcheck", SERVICES["orchestra-runtime"])

    def test_official_images_exclude_repository_bulk(self) -> None:
        application = (ROOT / "images/orchestra.Dockerfile").read_text(encoding="utf-8")
        runtime = (ROOT / "images/orchestra-runtime.Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY . ", application)
        self.assertNotIn("tests", application)
        self.assertNotIn("docs", application)
        self.assertNotIn("controller_api", runtime)
        for development_tool in (
            "check-secrets.py", "init-test-fixtures.sh", "orchestra-console-build.py",
            "platform-support.sh", "verify-layout.sh",
        ):
            self.assertIn(f"/scripts/{development_tool}", application)

    def test_installer_has_no_source_or_build_dependency(self) -> None:
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        for forbidden in ("git clone", "git checkout", "rsync", "pip install", "docker compose build"):
            self.assertNotIn(forbidden, source)
        self.assertIn("orchestra.yaml", source)
        self.assertIn("/opt/orchestra/data", source)

    def test_schema_and_blueprint_authorities_are_current(self) -> None:
        migrations = sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
        self.assertEqual(int(migrations[-1].name[:3]), 24)
        blueprint = (ROOT / "controller_api/blueprint.py").read_text(encoding="utf-8")
        self.assertIn('API_VERSION = "orchestra.dev/v1"', blueprint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
