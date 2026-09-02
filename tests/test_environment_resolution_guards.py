from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PACKAGE = SCRIPTS / "environment_resolution"
PURE_RESOLUTION_PATHS = (*sorted(PACKAGE.glob("*.py")), SCRIPTS / "oci_reference.py")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from environment_resolution import (  # noqa: E402
    DEFAULT_ENVIRONMENT_ID,
    ENVIRONMENT_SCHEMA_VERSION,
    DefaultEnvironmentResolver,
    EnvironmentResolutionError,
    EnvironmentResolver,
    EnvironmentSpec,
    ResolvedEnvironment,
)
from agent_runtime import RuntimeSandboxContext  # noqa: E402
from sandbox_backend import (  # noqa: E402
    PreparedEnvironment,
    SandboxPreparationError,
)


DIGEST = "sha256:" + "c" * 64
ACCEPTED_DIGEST = (
    "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
)
ACCEPTED_REFERENCE = "ghcr.io/bebet0o/orchestra-worker@" + ACCEPTED_DIGEST


def qualified_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = qualified_name(node.value, aliases)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                aliases[imported.asname or imported.name.split(".", 1)[0]] = (
                    imported.name
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for imported in node.names:
                aliases[imported.asname or imported.name] = (
                    f"{node.module}.{imported.name}"
                )
    return aliases


class EnvironmentArchitectureGuardTest(unittest.TestCase):
    def test_new_package_has_no_inherited_host_or_archive_mechanisms(self) -> None:
        forbidden = (
            "docker load",
            "docker save",
            ".tar.gz",
            "worker-image-archive",
            "systemctl",
            "loginctl",
            "/var/run/docker.sock",
        )
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in PURE_RESOLUTION_PATHS
        ).lower()

        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_package_import_boundary_excludes_side_effect_capabilities(self) -> None:
        forbidden_modules = {
            "subprocess",
            "socket",
            "urllib.request",
            "http.client",
            "requests",
            "tarfile",
            "zipfile",
            "shutil",
            "docker",
            "legacy_worker_environment",
        }

        for path in PURE_RESOLUTION_PATHS:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(item.name for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)
                    imported_modules.update(
                        f"{node.module}.{item.name}" for item in node.names
                    )

            for imported in imported_modules:
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(
                        any(
                            imported == forbidden
                            or imported.startswith(forbidden + ".")
                            for forbidden in forbidden_modules
                        ),
                        f"Forbidden resolver import: {imported}",
                    )

    def test_package_call_boundary_excludes_external_side_effects(self) -> None:
        forbidden_prefixes = (
            "subprocess.",
            "socket.",
            "urllib.request.",
            "http.client.",
            "requests.",
            "tarfile.",
            "zipfile.",
            "docker.",
        )
        forbidden_calls = {
            "os.system",
            "os.popen",
            "os.chdir",
            "os.putenv",
            "os.unsetenv",
            "os.environ.clear",
            "os.environ.pop",
            "os.environ.popitem",
            "os.environ.setdefault",
            "os.environ.update",
            "os.environ.__delitem__",
            "os.environ.__setitem__",
            "shutil.unpack_archive",
            "shutil.make_archive",
            "eval",
            "exec",
            "__import__",
            "importlib.import_module",
        }

        for path in PURE_RESOLUTION_PATHS:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            aliases = import_aliases(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = qualified_name(node.func, aliases)
                if called is None:
                    continue
                forbidden = (
                    called in forbidden_calls
                    or called.startswith("os.spawn")
                    or called.startswith("os.exec")
                    or any(
                        called.startswith(prefix)
                        for prefix in forbidden_prefixes
                    )
                )
                with self.subTest(path=path.name, called=called):
                    self.assertFalse(forbidden, f"Forbidden resolver call: {called}")

            assignment_targets: list[ast.expr] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    assignment_targets.extend(node.targets)
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    assignment_targets.append(node.target)
                elif isinstance(node, ast.Delete):
                    assignment_targets.extend(node.targets)
            for target in assignment_targets:
                environment_target = (
                    isinstance(target, ast.Subscript)
                    and qualified_name(target.value, aliases) == "os.environ"
                ) or qualified_name(target, aliases) == "os.environ"
                with self.subTest(path=path.name, target=ast.dump(target)):
                    self.assertFalse(
                        environment_target,
                        "Resolver must not mutate os.environ",
                    )

    def test_package_has_no_legacy_authority_dependency(self) -> None:
        for path in PURE_RESOLUTION_PATHS:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            constants = {
                node.value.lower()
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            names = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            }

            with self.subTest(path=path.name):
                self.assertNotIn("LegacyWorkerEnvironmentAdapter", names)
                self.assertNotIn("WORKER_LOCK", names)
                self.assertFalse(
                    any(
                        "worker-sandbox.lock.toml" in value
                        or "legacy_worker_environment" in value
                        for value in constants
                    )
                )

    def test_contract_fields_do_not_mix_selection_with_implementation(self) -> None:
        spec_fields = {field.name for field in fields(EnvironmentSpec)}
        resolved_fields = {field.name for field in fields(ResolvedEnvironment)}

        self.assertEqual(spec_fields, {"schema_version", "environment_id"})
        self.assertNotIn("image_id", resolved_fields)
        for field_name in spec_fields:
            normalized = field_name.lower()
            with self.subTest(field_name=field_name):
                self.assertNotIn("ghcr", normalized)
                self.assertNotIn("docker", normalized)
                self.assertNotIn("hermesfile", normalized)
                self.assertNotIn("blueprint", normalized)

    def test_default_resolver_satisfies_shared_protocol(self) -> None:
        resolver = DefaultEnvironmentResolver(PACKAGE / "fixture.toml")
        self.assertIsInstance(resolver, EnvironmentResolver)

    def test_reviewer_uses_the_workers_single_shared_loading_policy(self) -> None:
        tree = ast.parse(
            (SCRIPTS / "orchestra-reviewer.py").read_text(encoding="utf-8")
        )
        shared_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "prepare_worker_environment"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "WORKER"
        ]
        reviewer_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }

        self.assertEqual(len(shared_calls), 1)
        self.assertNotIn("WORKER_LOCK", reviewer_names)
        self.assertNotIn("LegacyWorkerEnvironmentAdapter", reviewer_names)


class PublishedEnvironmentRuntimeTest(unittest.TestCase):
    def test_worker_preparation_materializes_resolved_oci_environment(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "published_environment_worker_test",
            SCRIPTS / "orchestra-worker.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load worker module")
        worker = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(worker)
        resolved = ResolvedEnvironment(
            schema_version=ENVIRONMENT_SCHEMA_VERSION,
            environment_id=DEFAULT_ENVIRONMENT_ID,
            image_reference=ACCEPTED_REFERENCE,
            oci_digest=ACCEPTED_DIGEST,
            platform="linux/amd64",
            provenance="orchestra-3a-accepted-publication",
        )
        prepared = PreparedEnvironment(resolved, DIGEST)
        resolver = mock.Mock()
        resolver.resolve.return_value = resolved
        backend = mock.Mock()
        backend.materialize.return_value = prepared

        with (
            mock.patch.object(
                worker,
                "DefaultEnvironmentResolver",
                return_value=resolver,
            ),
            mock.patch.object(
                worker.NestedDaemonSandboxBackend,
                "for_dedicated_nested_daemon",
                return_value=backend,
            ),
        ):
            preparation = worker.prepare_worker_environment()

        self.assertIs(preparation, prepared)
        resolver.resolve.assert_called_once_with(worker.DEFAULT_WORKER_ENVIRONMENT_SPEC)
        backend.materialize.assert_called_once_with(resolved)

    def test_worker_boundary_normalizes_resolution_and_materialization_failures(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "published_environment_worker_error_test",
            SCRIPTS / "orchestra-worker.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load worker module")
        worker = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(worker)

        with (
            mock.patch.object(
                worker,
                "DefaultEnvironmentResolver",
                side_effect=EnvironmentResolutionError("resolution failed"),
            ),
            self.assertRaisesRegex(worker.WorkerError, "resolution failed"),
        ):
            worker.prepare_worker_environment()

        resolved = ResolvedEnvironment(
            schema_version=ENVIRONMENT_SCHEMA_VERSION,
            environment_id=DEFAULT_ENVIRONMENT_ID,
            image_reference=ACCEPTED_REFERENCE,
            oci_digest=ACCEPTED_DIGEST,
            platform="linux/amd64",
            provenance="orchestra-3a-accepted-publication",
        )
        backend = mock.Mock()
        backend.materialize.side_effect = SandboxPreparationError(
            "materialization failed"
        )
        with (
            mock.patch.object(
                worker,
                "DefaultEnvironmentResolver",
                return_value=mock.Mock(resolve=mock.Mock(return_value=resolved)),
            ),
            mock.patch.object(
                worker.NestedDaemonSandboxBackend,
                "for_dedicated_nested_daemon",
                return_value=backend,
            ),
            self.assertRaisesRegex(worker.WorkerError, "materialization failed"),
        ):
            worker.prepare_worker_environment()

    def test_runtime_context_preserves_accepted_oci_authority(self) -> None:
        resolved = ResolvedEnvironment(
            schema_version=ENVIRONMENT_SCHEMA_VERSION,
            environment_id=DEFAULT_ENVIRONMENT_ID,
            image_reference=ACCEPTED_REFERENCE,
            oci_digest=ACCEPTED_DIGEST,
            platform="linux/amd64",
            provenance="orchestra-3a-accepted-publication",
        )
        context = RuntimeSandboxContext(
            workspace=Path("/tmp/orchestra-runtime-context"),
            prepared_environment=PreparedEnvironment(resolved, DIGEST),
            cpu_limit=1,
            memory_mb=512,
            read_only=False,
            network_enabled=False,
            sandbox_handle="a" * 64,
            task_id="task-s3b-runtime-context",
            runtime_user="2001:3001",
        )

        self.assertEqual(
            context.prepared_environment.executable_image_selector,
            ACCEPTED_REFERENCE,
        )
        self.assertEqual(
            context.prepared_environment.oci_digest,
            ACCEPTED_DIGEST,
        )
        self.assertEqual(context.prepared_environment.local_image_config_id, DIGEST)
        self.assertNotEqual(
            context.prepared_environment.local_image_config_id,
            context.prepared_environment.oci_digest,
        )


if __name__ == "__main__":
    unittest.main()
