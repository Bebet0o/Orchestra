from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_runtime import (  # noqa: E402
    HermesRuntime,
    NativeRuntime,
    RuntimeError,
    RuntimeKind,
    RuntimeRole,
    create_runtime,
    parse_runtime_kind,
)
from model_provider import (  # noqa: E402
    FakeModelProvider,
    FakeModelProviderOutcome,
    ModelProviderError,
    ModelProviderErrorKind,
)


def load_script(name: str, filename: str):  # type: ignore[no-untyped-def]
    specification = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if specification is None or specification.loader is None:
        raise AssertionError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class RuntimeSelectionContractTest(unittest.TestCase):
    def test_default_and_explicit_hermes_selection_preserve_v01(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            (installed / "repo").symlink_to(ROOT, target_is_directory=True)
            default = create_runtime(installed, required_role=RuntimeRole.PLANNER)
            explicit = create_runtime(
                installed,
                required_role=RuntimeRole.PLANNER,
                kind=RuntimeKind.HERMES,
            )
        self.assertIsInstance(default, HermesRuntime)
        self.assertIsInstance(explicit, HermesRuntime)

    def test_explicit_native_selection_uses_injected_model_provider(self) -> None:
        provider = FakeModelProvider([FakeModelProviderOutcome.success("done")])
        runtime = create_runtime(
            ROOT,
            required_role=RuntimeRole.PLANNER,
            kind="native",
            model="fixed-model",
            provider=provider,
        )
        self.assertIsInstance(runtime, NativeRuntime)
        self.assertEqual(runtime.runtime_kind, "native")

    def test_invalid_selector_is_rejected(self) -> None:
        for value in ("automatic", "NATIVE", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_runtime_kind(value)


class RoleRuntimeConfigurationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.roles = load_script("native_activation_roles", "orchestra-roles.py")

    def document(self) -> dict[str, object]:
        with (ROOT / "config/roles.toml").open("rb") as stream:
            return tomllib.load(stream)

    def test_v01_role_profiles_without_runtime_selector_default_to_hermes(self) -> None:
        document = self.document()
        for role in document["roles"].values():  # type: ignore[union-attr]
            role.pop("runtime")
        with (
            mock.patch.object(self.roles, "load_document", return_value=document),
            mock.patch.object(self.roles, "PROFILE_TEMPLATES", ROOT / "profiles"),
        ):
            discovered = self.roles.discover_roles()
        self.assertTrue(discovered)
        self.assertEqual({role["runtime_kind"] for role in discovered}, {"hermes"})

    def test_invalid_role_runtime_selector_is_rejected(self) -> None:
        document = self.document()
        document["roles"]["orchestrator"]["runtime"] = {"kind": "automatic"}  # type: ignore[index]
        with (
            mock.patch.object(self.roles, "load_document", return_value=document),
            mock.patch.object(self.roles, "PROFILE_TEMPLATES", ROOT / "profiles"),
            self.assertRaises(self.roles.RoleError),
        ):
            self.roles.discover_roles()


class NativePlannerControlPlaneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.planner = load_script("native_activation_planner", "orchestra-planner.py")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "repo").symlink_to(ROOT, target_is_directory=True)
        self.database = self.root / "state/controller/orchestra.db"
        self.database.parent.mkdir(parents=True)
        with sqlite3.connect(self.database) as connection:
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
            now = "2026-09-04T00:00:00.000Z"
            connection.execute(
                """
                INSERT INTO roles VALUES (
                    'orchestrator', 'ops-orchestrator', 'orchestrator',
                    'planner', 'high', 30, '[]', '[]', 'none', 0, 0, 0,
                    2, 4096, 1, 'test', ?, ?, ?, 'native', 'fixed-model'
                )
                """,
                ("a" * 64, now, now),
            )
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, repo_path, data_path, policy_id,
                    enabled, config_source, config_hash, registered_at, updated_at
                ) VALUES (
                    'fixture', 'Fixture', '/tmp/fixture', '/tmp/fixture-data',
                    'default', 1, 'test', ?, ?, ?
                )
                """,
                ("b" * 64, now, now),
            )
            connection.commit()

        self.execution_root = self.root / "state/controller/orchestrator-executions"
        self.objective = self.root / "objective.txt"
        self.objective.write_text("Implement the bounded change", encoding="utf-8")
        self.arguments = SimpleNamespace(
            objective_file=str(self.objective),
            projects="fixture",
            marker="NATIVE_PLANNER_OK",
            timeout=30,
            expected_task_count=1,
            status="READY",
        )
        self.plan = {
            "schema_version": 1,
            "objective": "Implement the bounded change",
            "max_parallel_tasks": 1,
            "tasks": [{"project_id": "fixture", "key": "change"}],
        }
        self.output = (
            "ORCHESTRA_PLAN_JSON_BEGIN\n"
            + json.dumps(self.plan)
            + "\nORCHESTRA_PLAN_JSON_END\nNATIVE_PLANNER_OK\n"
        )

    def run_planner(self, provider: FakeModelProvider) -> None:
        orchestrator = mock.Mock()
        orchestrator.validate_plan.return_value = self.plan
        orchestrator.insert_plan.return_value = None
        orchestrator.payload_sha256.return_value = "c" * 64
        with (
            mock.patch("builtins.print"),
            mock.patch.object(self.planner, "ROOT", self.root),
            mock.patch.object(self.planner, "REPO", self.root / "repo"),
            mock.patch.object(self.planner, "DATABASE", self.database),
            mock.patch.object(self.planner, "EXECUTIONS_ROOT", self.execution_root),
            mock.patch.object(self.planner, "load_orchestrator", return_value=orchestrator),
        ):
            self.planner.command_generate(self.arguments, provider=provider)

    def execution_snapshot(self) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        execution = connection.execute(
            "SELECT * FROM orchestrator_executions"
        ).fetchone()
        events = connection.execute(
            "SELECT * FROM runtime_events ORDER BY runtime_event_id"
        ).fetchall()
        assert execution is not None
        return execution, events

    def test_native_success_is_runtime_neutral_and_durable_after_readback(self) -> None:
        provider = FakeModelProvider([FakeModelProviderOutcome.success(self.output)])
        self.run_planner(provider)

        self.assertEqual(len(provider.requests), 1)
        request = provider.requests[0]
        self.assertEqual(request.model, "fixed-model")
        self.assertEqual(request.timeout_seconds, 30)
        self.assertIn("Implement the bounded change", request.messages[0].content)

        execution, events = self.execution_snapshot()
        self.assertEqual(execution["runtime_kind"], "native")
        self.assertEqual(execution["exit_code"], 0)
        self.assertIsNotNone(execution["finished_at"])
        self.assertIsNone(execution["failure_reason"])
        self.assertEqual([event["event_kind"] for event in events], ["started"])
        self.assertEqual(events[0]["runtime_kind"], "native")

        with sqlite3.connect(self.database) as restarted:
            self.assertEqual(
                restarted.execute(
                    "SELECT runtime_kind, exit_code FROM orchestrator_executions"
                ).fetchone(),
                ("native", 0),
            )

    def test_native_provider_failure_is_durable(self) -> None:
        provider = FakeModelProvider(
            [
                FakeModelProviderOutcome.failure(
                    ModelProviderError(
                        ModelProviderErrorKind.UNAVAILABLE,
                        "unavailable test provider",
                    )
                )
            ]
        )
        with self.assertRaises(RuntimeError):
            self.run_planner(provider)

        execution, events = self.execution_snapshot()
        self.assertEqual(execution["runtime_kind"], "native")
        self.assertIsNone(execution["exit_code"])
        self.assertIsNotNone(execution["finished_at"])
        self.assertEqual(
            execution["failure_reason"],
            "runtime_error[runtime_unavailable]: Native model provider is unavailable",
        )
        self.assertEqual([event["event_kind"] for event in events], ["started"])


if __name__ == "__main__":
    unittest.main()
