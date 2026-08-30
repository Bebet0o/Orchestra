from __future__ import annotations

import ast
import copy
import inspect
import json
import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from controller_api.execution_reads import ExecutionReadStore  # noqa: E402
from environment_resolution import ResolvedEnvironment  # noqa: E402
from legacy_worker_environment import LegacyLocalEnvironment  # noqa: E402
import sandbox_backend as sandbox_backend_module  # noqa: E402
from sandbox_backend import (  # noqa: E402
    LegacyPreparedEnvironment,
    NestedDaemonSandboxBackend,
    NestedDockerImageClient,
    PreparedEnvironment,
    SandboxContainerExpectation,
    SandboxPreparationError,
    prepare_legacy_environment,
    verify_prepared_container,
)


OCI_DIGEST = "sha256:" + "a" * 64
LOCAL_CONFIG_ID = "sha256:" + "b" * 64
IMAGE_REFERENCE = "registry.example.com/team/worker@" + OCI_DIGEST
CONTAINER_ID = "c" * 64


def resolved_environment() -> ResolvedEnvironment:
    return ResolvedEnvironment(
        schema_version=1,
        environment_id="default-worker",
        image_reference=IMAGE_REFERENCE,
        oci_digest=OCI_DIGEST,
        platform="linux/amd64",
        provenance="test-publication",
    )


def prepared_environment() -> PreparedEnvironment:
    return PreparedEnvironment(resolved_environment(), LOCAL_CONFIG_ID)


def legacy_prepared_environment() -> LegacyPreparedEnvironment:
    return prepare_legacy_environment(
        LegacyLocalEnvironment(
            environment_id="default-worker",
            local_image_config_id=LOCAL_CONFIG_ID,
            local_image_tag="hermesops-worker-sandbox:0.2",
        )
    )


def inspected_container(
    preparation: PreparedEnvironment | LegacyPreparedEnvironment,
    *,
    read_only: bool = False,
) -> dict[str, object]:
    return {
        "Id": CONTAINER_ID,
        "Image": preparation.local_image_config_id,
        "State": {"Status": "running"},
        "Config": {
            "Image": preparation.executable_image_selector,
            "User": "1000:1000",
            "Labels": {
                "hermesops-sandbox": "1",
                "hermesops-task-id": "task-test",
                "hermesops-runtime-request-id": "request-test",
            },
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/srv/workspace",
                "Destination": "/workspace",
                "RW": not read_only,
            },
            {
                "Type": "tmpfs",
                "Source": "",
                "Destination": "/tmp",
                "RW": True,
            },
        ],
    }


def expectation(
    preparation: PreparedEnvironment | LegacyPreparedEnvironment,
    *,
    read_only: bool = False,
) -> SandboxContainerExpectation:
    return SandboxContainerExpectation(
        container_id=CONTAINER_ID,
        preparation=preparation,
        task_id="task-test",
        runtime_request_id="request-test",
        workspace=Path("/srv/workspace"),
        read_only=read_only,
    )


class PreparedEnvironmentTest(unittest.TestCase):
    def test_is_immutable_and_retains_exact_resolved_identity(self) -> None:
        resolved = resolved_environment()
        prepared = PreparedEnvironment(resolved, LOCAL_CONFIG_ID)
        self.assertIs(prepared.resolved_environment, resolved)
        self.assertEqual(prepared.executable_image_selector, IMAGE_REFERENCE)
        self.assertEqual(prepared.image_reference, IMAGE_REFERENCE)
        self.assertEqual(prepared.oci_digest, OCI_DIGEST)
        with self.assertRaises(FrozenInstanceError):
            prepared.local_image_config_id = "changed"  # type: ignore[misc]

    def test_rejects_malformed_or_noncanonical_local_config_ids(self) -> None:
        for candidate in (
            "",
            "sha256:short",
            "sha256:" + "A" * 64,
            "sha512:" + "b" * 64,
            " " + LOCAL_CONFIG_ID,
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                PreparedEnvironment(resolved_environment(), candidate)

    def test_oci_digest_cannot_silently_replace_local_config_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "OCI digest"):
            PreparedEnvironment(resolved_environment(), OCI_DIGEST)

    def test_backend_prepares_only_matching_materialized_reference(self) -> None:
        calls: list[str] = []

        def inspect_image(selector: str) -> dict[str, object]:
            calls.append(selector)
            return {"Id": LOCAL_CONFIG_ID, "RepoDigests": [IMAGE_REFERENCE]}

        prepared = NestedDaemonSandboxBackend(inspect_image).prepare(
            resolved_environment()
        )
        self.assertEqual(calls, [IMAGE_REFERENCE])
        self.assertEqual(prepared.local_image_config_id, LOCAL_CONFIG_ID)

    def test_backend_rejects_local_image_without_authoritative_reference(self) -> None:
        backend = NestedDaemonSandboxBackend(
            lambda _selector: {"Id": LOCAL_CONFIG_ID, "RepoDigests": []}
        )
        with self.assertRaises(SandboxPreparationError):
            backend.prepare(resolved_environment())

    def test_legacy_bridge_has_no_oci_identity(self) -> None:
        prepared = legacy_prepared_environment()
        self.assertEqual(prepared.local_image_config_id, LOCAL_CONFIG_ID)
        self.assertEqual(prepared.executable_image_selector, LOCAL_CONFIG_ID)
        self.assertIsNone(prepared.oci_digest)
        self.assertIsNone(prepared.image_reference)
        self.assertFalse(hasattr(prepared, "resolved_environment"))


class MaterializationTest(unittest.TestCase):
    def test_materialize_pulls_and_inspects_the_exact_immutable_reference(self) -> None:
        calls: list[tuple[object, ...]] = []
        resolved = resolved_environment()

        def pull(reference: str, platform: str) -> None:
            calls.append(("pull", reference, platform))

        def inspect_image(reference: str) -> dict[str, object]:
            calls.append(("inspect", reference))
            return {"Id": LOCAL_CONFIG_ID, "RepoDigests": [IMAGE_REFERENCE]}

        prepared = NestedDaemonSandboxBackend(
            inspect_image,
            pull,
        ).materialize(resolved)

        self.assertEqual(
            calls,
            [
                ("pull", IMAGE_REFERENCE, "linux/amd64"),
                ("inspect", IMAGE_REFERENCE),
            ],
        )
        self.assertIs(prepared.resolved_environment, resolved)
        self.assertEqual(prepared.local_image_config_id, LOCAL_CONFIG_ID)

    def test_materialization_pull_failure_is_normalized_and_stops_inspection(
        self,
    ) -> None:
        inspect_image = mock.Mock()
        backend = NestedDaemonSandboxBackend(
            inspect_image,
            mock.Mock(side_effect=OSError("pull unavailable")),
        )
        with self.assertRaisesRegex(
            SandboxPreparationError,
            "materialization failed",
        ):
            backend.materialize(resolved_environment())
        inspect_image.assert_not_called()

    def test_materialization_requires_exact_repository_digest_after_pull(
        self,
    ) -> None:
        wrong_repository = "registry.example.com/other/worker@" + OCI_DIGEST
        for repo_digests in ([], [wrong_repository], None, "invalid"):
            with self.subTest(repo_digests=repo_digests):
                backend = NestedDaemonSandboxBackend(
                    lambda _reference, value=repo_digests: {
                        "Id": LOCAL_CONFIG_ID,
                        "RepoDigests": value,
                    },
                    lambda _reference, _platform: None,
                )
                with self.assertRaises(SandboxPreparationError):
                    backend.materialize(resolved_environment())

    def test_materialization_rejects_invalid_local_config_identity(self) -> None:
        for local_id in (None, "sha256:short", OCI_DIGEST):
            with self.subTest(local_id=local_id):
                backend = NestedDaemonSandboxBackend(
                    lambda _reference, value=local_id: {
                        "Id": value,
                        "RepoDigests": [IMAGE_REFERENCE],
                    },
                    lambda _reference, _platform: None,
                )
                with self.assertRaises(SandboxPreparationError):
                    backend.materialize(resolved_environment())

    def test_prepare_only_backend_cannot_silently_materialize(self) -> None:
        backend = NestedDaemonSandboxBackend(
            lambda _reference: {
                "Id": LOCAL_CONFIG_ID,
                "RepoDigests": [IMAGE_REFERENCE],
            }
        )
        with self.assertRaisesRegex(
            SandboxPreparationError,
            "no OCI materialization",
        ):
            backend.materialize(resolved_environment())

    def test_nested_client_uses_only_the_dedicated_engine_and_exact_selector(
        self,
    ) -> None:
        calls: list[list[str]] = []

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            output = "" if "pull" in arguments else json.dumps(
                [{"Id": LOCAL_CONFIG_ID, "RepoDigests": [IMAGE_REFERENCE]}]
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")

        client = NestedDockerImageClient(run)
        prepared = NestedDaemonSandboxBackend.for_dedicated_nested_daemon(
            client
        ).materialize(resolved_environment())

        self.assertEqual(prepared.image_reference, IMAGE_REFERENCE)
        self.assertEqual(
            calls,
            [
                [
                    "docker",
                    "exec",
                    "hermesops-sandbox-engine",
                    "docker",
                    "image",
                    "pull",
                    "--platform",
                    "linux/amd64",
                    IMAGE_REFERENCE,
                ],
                [
                    "docker",
                    "exec",
                    "hermesops-sandbox-engine",
                    "docker",
                    "image",
                    "inspect",
                    IMAGE_REFERENCE,
                ],
            ],
        )
        self.assertNotIn("/var/run/docker.sock", repr(calls))

    def test_nested_client_rejects_cli_and_inspection_failures(self) -> None:
        failures = (
            subprocess.CompletedProcess([], 1, "", "pull failed"),
            subprocess.CompletedProcess([], 0, "not-json", ""),
            subprocess.CompletedProcess([], 0, "[]", ""),
        )
        for result in failures:
            with self.subTest(result=result):
                client = NestedDockerImageClient(
                    lambda _arguments, **_kwargs: result
                )
                backend = NestedDaemonSandboxBackend.for_dedicated_nested_daemon(
                    client
                )
                with self.assertRaises(SandboxPreparationError):
                    backend.materialize(resolved_environment())

    def test_nested_client_rejects_mutable_or_wrong_platform_pull_inputs(
        self,
    ) -> None:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, "", "")
        )
        client = NestedDockerImageClient(runner)
        for reference, platform in (
            ("worker:latest", "linux/amd64"),
            (OCI_DIGEST, "linux/amd64"),
            (IMAGE_REFERENCE, "linux/arm64"),
        ):
            with self.subTest(reference=reference, platform=platform):
                with self.assertRaises((ValueError, SandboxPreparationError)):
                    client.pull_exact(reference, platform)
        runner.assert_not_called()

    def test_nested_client_sets_operation_timeouts_and_output_limit(self) -> None:
        calls: list[dict[str, object]] = []

        def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            output = "" if "pull" in arguments else json.dumps(
                [{"Id": LOCAL_CONFIG_ID, "RepoDigests": [IMAGE_REFERENCE]}]
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")

        client = NestedDockerImageClient(run)
        client.pull_exact(IMAGE_REFERENCE, "linux/amd64")
        client.inspect_exact(IMAGE_REFERENCE)
        self.assertEqual(calls[0]["timeout"], 900)
        self.assertEqual(calls[1]["timeout"], 30)
        self.assertEqual(calls[0]["output_limit"], 262_144)
        self.assertEqual(calls[1]["output_limit"], 262_144)

    def test_bounded_runner_stops_timeout_and_excess_output(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            sandbox_backend_module._run_bounded_command(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout=0.01,
                output_limit=100,
                env={"PATH": os.defpath},
            )
        with self.assertRaisesRegex(subprocess.SubprocessError, "output limit"):
            sandbox_backend_module._run_bounded_command(
                [sys.executable, "-c", "print('x' * 1000)"],
                timeout=5,
                output_limit=100,
                env={"PATH": os.defpath},
            )

    def test_nested_client_does_not_inherit_docker_authority_environment(self) -> None:
        observed: list[dict[str, str]] = []

        def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed.append(kwargs["env"])  # type: ignore[arg-type]
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with mock.patch.dict(
            os.environ,
            {
                "DOCKER_HOST": "tcp://attacker.invalid:2375",
                "DOCKER_CONTEXT": "attacker",
                "DOCKER_CONFIG": "/tmp/attacker",
                "HTTPS_PROXY": "http://attacker.invalid",
            },
        ):
            NestedDockerImageClient(run).pull_exact(
                IMAGE_REFERENCE,
                "linux/amd64",
            )
        self.assertEqual(observed[0]["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertEqual(observed[0]["DOCKER_CONTEXT"], "default")
        self.assertEqual(
            observed[0]["DOCKER_CONFIG"],
            "/nonexistent/orchestra-empty-docker-config",
        )
        self.assertNotIn("HTTPS_PROXY", observed[0])


class ContainerAuthorityTest(unittest.TestCase):
    def test_oci_container_requires_reference_and_local_evidence(self) -> None:
        prepared = prepared_environment()
        self.assertEqual(
            verify_prepared_container(
                inspected_container(prepared),
                expectation(prepared),
            ),
            CONTAINER_ID,
        )

    def test_wrong_config_image_is_detected(self) -> None:
        prepared = prepared_environment()
        container = inspected_container(prepared)
        container["Config"]["Image"] = LOCAL_CONFIG_ID  # type: ignore[index]
        with self.assertRaisesRegex(SandboxPreparationError, "selector"):
            verify_prepared_container(container, expectation(prepared))

    def test_wrong_local_config_id_is_detected(self) -> None:
        prepared = prepared_environment()
        container = inspected_container(prepared)
        container["Image"] = "sha256:" + "d" * 64
        with self.assertRaisesRegex(SandboxPreparationError, "local image"):
            verify_prepared_container(container, expectation(prepared))

    def test_wrong_task_and_request_bindings_are_detected(self) -> None:
        prepared = prepared_environment()
        for label in ("hermesops-task-id", "hermesops-runtime-request-id"):
            container = inspected_container(prepared)
            container["Config"]["Labels"][label] = "wrong"  # type: ignore[index]
            with self.subTest(label=label), self.assertRaises(
                SandboxPreparationError
            ):
                verify_prepared_container(container, expectation(prepared))

    def test_wrong_full_id_and_name_only_authority_fail_closed(self) -> None:
        prepared = prepared_environment()
        container = inspected_container(prepared)
        container["Id"] = "d" * 64
        with self.assertRaisesRegex(SandboxPreparationError, "full container ID"):
            verify_prepared_container(container, expectation(prepared))
        with self.assertRaisesRegex(ValueError, "full container ID"):
            SandboxContainerExpectation(
                container_id="hermesops-sandbox-friendly-name",
                preparation=prepared,
                task_id="task-test",
                runtime_request_id="request-test",
                workspace=Path("/srv/workspace"),
                read_only=False,
            )

    def test_reviewer_read_only_workspace_is_preserved(self) -> None:
        prepared = legacy_prepared_environment()
        verify_prepared_container(
            inspected_container(prepared, read_only=True),
            expectation(prepared, read_only=True),
        )
        writable = inspected_container(prepared, read_only=False)
        with self.assertRaisesRegex(SandboxPreparationError, "workspace"):
            verify_prepared_container(
                writable,
                expectation(prepared, read_only=True),
            )

    def test_host_docker_socket_mount_is_rejected(self) -> None:
        prepared = prepared_environment()
        container = inspected_container(prepared)
        container["Mounts"].append(  # type: ignore[union-attr]
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            }
        )
        with self.assertRaisesRegex(SandboxPreparationError, "Docker socket"):
            verify_prepared_container(container, expectation(prepared))

    def test_worker_and_reviewer_call_shared_authority_verifier(self) -> None:
        for path, function_name in (
            (SCRIPTS / "hermesops-worker.py", "audit_sandbox"),
            (SCRIPTS / "hermesops-reviewer.py", "audit_reviewer_sandbox"),
        ):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                function = next(
                    node
                    for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == function_name
                )
                verifier_calls = [
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "verify_prepared_container"
                ]
                self.assertEqual(len(verifier_calls), 1)
                self.assertNotIn("image_id:", source)


class ProjectionAuthorityTest(unittest.TestCase):
    def test_legacy_local_config_id_is_not_reported_as_oci_digest(self) -> None:
        raw = json.dumps(
            {
                "local_image_config_id": LOCAL_CONFIG_ID,
                "audit": {"image": LOCAL_CONFIG_ID},
                "image_id": LOCAL_CONFIG_ID,
            }
        )
        self.assertIsNone(ExecutionReadStore._image_digest(raw))

    def test_oci_reference_reports_its_authoritative_digest(self) -> None:
        raw = json.dumps(
            {
                "sandbox_image_reference": IMAGE_REFERENCE,
                "sandbox_image_digest": OCI_DIGEST,
            }
        )
        self.assertEqual(ExecutionReadStore._image_digest(raw), OCI_DIGEST)

    def test_conflicting_projected_digest_fails_closed(self) -> None:
        raw = json.dumps(
            {
                "sandbox_image_reference": IMAGE_REFERENCE,
                "sandbox_image_digest": LOCAL_CONFIG_ID,
            }
        )
        self.assertIsNone(ExecutionReadStore._image_digest(raw))

    def test_projection_uses_the_canonical_environment_reference_contract(self) -> None:
        malformed = (
            "ghcr.io//worker@" + OCI_DIGEST,
            "-registry.example/team/worker@" + OCI_DIGEST,
            "registry-.example/team/worker@" + OCI_DIGEST,
            "ghcr.io/team/../worker@" + OCI_DIGEST,
            "ghcr.io/team/.worker@" + OCI_DIGEST,
            "ghcr.io/team//worker@" + OCI_DIGEST,
            "ghcr.io/Team/worker@" + OCI_DIGEST,
            OCI_DIGEST,
            "worker:latest",
            "ghcr.io/team/worker:latest@" + OCI_DIGEST,
        )
        for image_reference in malformed:
            with self.subTest(image_reference=image_reference):
                with self.assertRaises(ValueError):
                    ResolvedEnvironment(
                        schema_version=1,
                        environment_id="default-worker",
                        image_reference=image_reference,
                        oci_digest=OCI_DIGEST,
                        platform="linux/amd64",
                        provenance="projection-test",
                    )
                raw = json.dumps(
                    {"sandbox_image_reference": image_reference}
                )
                self.assertIsNone(ExecutionReadStore._image_digest(raw))


class AuthoritySourceTest(unittest.TestCase):
    def test_no_ambiguous_image_id_enters_new_contract(self) -> None:
        source = inspect.getsource(sys.modules["sandbox_backend"])
        self.assertNotIn(" image_id", source)
        self.assertNotIn("sandbox_image_digest = local", source)


if __name__ == "__main__":
    unittest.main()
