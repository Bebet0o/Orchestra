from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
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
    EnvironmentResolver,
    EnvironmentSpec,
    ResolvedEnvironment,
)
from legacy_worker_environment import (  # noqa: E402
    LegacyEnvironmentError,
    LegacyWorkerEnvironmentAdapter,
)


DIGEST = "sha256:" + "c" * 64


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
            (SCRIPTS / "hermesops-reviewer.py").read_text(encoding="utf-8")
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


class LegacyEnvironmentTransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lock_path = Path(self.temporary.name) / "worker-sandbox.lock.toml"
        self.spec = EnvironmentSpec(
            ENVIRONMENT_SCHEMA_VERSION,
            DEFAULT_ENVIRONMENT_ID,
        )

    def write_lock(self, local_config_id: str = DIGEST) -> None:
        self.lock_path.write_text(
            "\n".join(
                (
                    "schema_version = 1",
                    'tag = "hermesops-worker-sandbox:0.2"',
                    f'image_id = "{local_config_id}"',
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def adapter(self, availability: mock.Mock | None = None):
        return LegacyWorkerEnvironmentAdapter(
            self.lock_path,
            mock.Mock() if availability is None else availability,
        )

    def test_adapter_exposes_explicit_local_evidence_and_checks_availability(self) -> None:
        self.write_lock()
        availability = mock.Mock(return_value=subprocess.CompletedProcess([], 0))

        environment = LegacyWorkerEnvironmentAdapter(
            self.lock_path,
            availability,
        ).load(self.spec)

        self.assertEqual(environment.environment_id, DEFAULT_ENVIRONMENT_ID)
        self.assertEqual(environment.local_image_config_id, DIGEST)
        self.assertEqual(environment.provenance, "legacy-worker-sandbox-lock")
        availability.assert_called_once_with(DIGEST)
        with self.assertRaises(FrozenInstanceError):
            environment.local_image_config_id = "changed"  # type: ignore[misc]

    def test_adapter_rejects_noncanonical_local_config_identity(self) -> None:
        availability = mock.Mock()
        for local_config_id in (
            "latest",
            "sha256:short",
            "sha256:" + "C" * 64,
            "sha256:" + "g" * 64,
            " " + DIGEST,
            DIGEST + " ",
        ):
            with self.subTest(local_config_id=local_config_id):
                self.write_lock(local_config_id)
                with self.assertRaises(LegacyEnvironmentError):
                    LegacyWorkerEnvironmentAdapter(
                        self.lock_path,
                        availability,
                    ).load(self.spec)
        availability.assert_not_called()

    def test_repository_current_legacy_lock_is_canonical_and_accepted(self) -> None:
        current_lock = ROOT / "config/worker-sandbox.lock.toml"
        availability = mock.Mock()

        environment = LegacyWorkerEnvironmentAdapter(
            current_lock,
            availability,
        ).load(self.spec)

        self.assertRegex(environment.local_image_config_id, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(environment.local_image_tag, "hermesops-worker-sandbox:0.2")
        availability.assert_called_once_with(environment.local_image_config_id)

    def test_adapter_normalizes_missing_and_malformed_lock_errors(self) -> None:
        with self.assertRaises(LegacyEnvironmentError) as missing:
            self.adapter().load(self.spec)
        self.assertIsInstance(missing.exception.__cause__, OSError)

        self.lock_path.write_text("schema_version = [\n", encoding="utf-8")
        with self.assertRaises(LegacyEnvironmentError) as malformed:
            self.adapter().load(self.spec)
        self.assertNotEqual(type(malformed.exception.__cause__), type(None))

    def test_adapter_rejects_missing_required_lock_keys(self) -> None:
        documents = (
            'tag = "hermesops-worker-sandbox:0.2"\nimage_id = "' + DIGEST + '"\n',
            'schema_version = 1\nimage_id = "' + DIGEST + '"\n',
            'schema_version = 1\ntag = "hermesops-worker-sandbox:0.2"\n',
        )
        for document in documents:
            with self.subTest(document=document):
                self.lock_path.write_text(document, encoding="utf-8")
                with self.assertRaises(LegacyEnvironmentError):
                    self.adapter().load(self.spec)

    def test_adapter_rejects_empty_or_malformed_local_tag(self) -> None:
        invalid_tags = (
            "",
            " ",
            "hermesops-worker-sandbox",
            "hermesops worker:0.2",
            "HermesOps-worker:0.2",
            "hermesops-worker-sandbox: bad",
        )
        for tag in invalid_tags:
            with self.subTest(tag=tag):
                self.lock_path.write_text(
                    "schema_version = 1\n"
                    f'tag = "{tag}"\n'
                    f'image_id = "{DIGEST}"\n',
                    encoding="utf-8",
                )
                with self.assertRaises(LegacyEnvironmentError):
                    self.adapter().load(self.spec)

    def test_adapter_rejects_another_environment(self) -> None:
        self.write_lock()
        adapter = LegacyWorkerEnvironmentAdapter(self.lock_path, mock.Mock())

        with self.assertRaises(LegacyEnvironmentError):
            adapter.load(EnvironmentSpec(1, "another-worker"))

    def test_worker_preparation_uses_the_explicit_transition_adapter(self) -> None:
        self.write_lock()
        specification = importlib.util.spec_from_file_location(
            "environment_transition_worker_test",
            SCRIPTS / "hermesops-worker.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load worker module")
        worker = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(worker)
        completed = subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(worker, "WORKER_LOCK", self.lock_path),
            mock.patch.object(worker, "nested_docker", return_value=completed) as inspect,
        ):
            preparation = worker.prepare_worker_environment()

        self.assertEqual(preparation.local_image_config_id, DIGEST)
        self.assertIsNone(preparation.oci_digest)
        inspect.assert_called_once_with("image", "inspect", DIGEST)

    def test_worker_boundary_normalizes_legacy_lock_failures(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "environment_transition_worker_error_test",
            SCRIPTS / "hermesops-worker.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load worker module")
        worker = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(worker)

        cases = (
            None,
            "schema_version = [\n",
            "schema_version = 1\n",
            "schema_version = 1\n"
            'tag = "hermesops-worker-sandbox:0.2"\n'
            'image_id = "sha256:short"\n',
        )
        for document in cases:
            with self.subTest(document=document):
                if self.lock_path.exists():
                    self.lock_path.unlink()
                if document is not None:
                    self.lock_path.write_text(document, encoding="utf-8")
                with (
                    mock.patch.object(worker, "WORKER_LOCK", self.lock_path),
                    mock.patch.object(worker, "nested_docker") as inspect,
                    self.assertRaises(worker.WorkerError),
                ):
                    worker.prepare_worker_environment()
                inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
