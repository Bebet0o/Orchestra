from __future__ import annotations

import ast
import builtins
import copy
import inspect
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from agent_runtime import (  # noqa: E402
    FakeRuntime,
    FakeRuntimeOutcome,
    HermesRuntime,
    RuntimeEvent,
    RuntimeEventDispatcher,
    RuntimeEventKind,
    RuntimeError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimePreparedEnvironmentData,
    RuntimeResult,
    RuntimeRole,
    RuntimeSandboxContext,
    record_runtime_failure,
)
from environment_resolution import ResolvedEnvironment  # noqa: E402
from sandbox_backend import PreparedEnvironment  # noqa: E402


def oci_preparation(hex_character: str) -> PreparedEnvironment:
    digest_character = "d" if hex_character == "e" else "e"
    digest = "sha256:" + digest_character * 64
    return PreparedEnvironment(
        ResolvedEnvironment(
            schema_version=1,
            environment_id="default-worker",
            image_reference="registry.example.com/orchestra/worker@" + digest,
            oci_digest=digest,
            platform="linux/amd64",
            provenance="runtime-test",
        ),
        "sha256:" + hex_character * 64,
    )

RECOVERY_SPEC = importlib.util.spec_from_file_location(
    "orchestra_recovery_runtime_tests",
    SCRIPTS / "orchestra-recovery.py",
)
if RECOVERY_SPEC is None or RECOVERY_SPEC.loader is None:
    raise RuntimeError("Cannot load recovery module for runtime tests")
RECOVERY = importlib.util.module_from_spec(RECOVERY_SPEC)
RECOVERY_SPEC.loader.exec_module(RECOVERY)
host_container_ownership = RECOVERY.host_container_ownership
nested_container_ownership = RECOVERY.nested_container_ownership


class FalsyFakeRuntime(FakeRuntime):
    def __bool__(self) -> bool:
        return False


class AgentRuntimeContractTest(unittest.TestCase):
    def request(self, **overrides: object) -> RuntimeRequest:
        values: dict[str, object] = {
            "role": RuntimeRole.PLANNER,
            "prompt": "Perform one bounded operation",
            "runtime_config_id": "ops-orchestrator",
            "request_id": "runtime-test",
            "timeout_seconds": 30,
            "completion_marker": "RUNTIME_TEST_OK",
        }
        values.update(overrides)
        return RuntimeRequest(**values)

    def test_contract_types_are_structured_and_runtime_neutral(self) -> None:
        self.assertEqual(
            {role.value for role in RuntimeRole},
            {"planner", "worker", "reviewer"},
        )
        self.assertEqual(
            {kind.value for kind in RuntimeErrorKind},
            {
                "runtime_unavailable",
                "execution_failed",
                "timeout",
                "invalid_result",
                "cancelled",
            },
        )
        request_fields = {field.name.lower() for field in fields(RuntimeRequest)}
        self.assertFalse(any("hermes" in name for name in request_fields))
        self.assertNotIn("command", request_fields)
        self.assertNotIn("provider", request_fields)
        self.assertNotIn("transcript_path", request_fields)

    def test_request_rejects_wrong_runtime_types_at_construction(self) -> None:
        with self.assertRaises(TypeError):
            self.request(role="planner")
        with self.assertRaises(TypeError):
            self.request(timeout_seconds=True)
        with self.assertRaises(TypeError):
            self.request(on_event="not-callable")

    def test_prompt_is_omitted_from_request_repr(self) -> None:
        secret = "prompt-secret-that-must-not-appear"
        self.assertNotIn(secret, repr(self.request(prompt=secret)))

    def test_contract_identifiers_reject_structural_injection(self) -> None:
        for candidate in (
            "../escape",
            "/absolute",
            "back\\slash",
            "line\nbreak",
            "unicode\u2028separator",
            "$(shell)",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    self.request(request_id=candidate)

        for marker in (" spaced ", "two\nlines", "unicode\u2028line", "nul\x00"):
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    self.request(completion_marker=marker)

        with self.assertRaises(ValueError):
            self.request(prompt="unsafe\x00prompt")

    def test_sandbox_contract_is_generic_and_strict(self) -> None:
        sandbox = RuntimeSandboxContext(
            workspace=Path("/tmp/workspace"),
            prepared_environment=oci_preparation("a"),
            cpu_limit=1,
            memory_mb=512,
            read_only=True,
            network_enabled=False,
            sandbox_handle="a" * 64,
            task_id="task-contract",
            runtime_user="2001:3001",
        )
        field_names = {item.name for item in fields(sandbox)}
        self.assertEqual(
            field_names,
            {
                "workspace",
                "prepared_environment",
                "cpu_limit",
                "memory_mb",
                "read_only",
                "network_enabled",
                "sandbox_handle",
                "task_id",
                "runtime_user",
            },
        )
        self.assertFalse(any("hermes" in name for name in field_names))
        with self.assertRaises(TypeError):
            RuntimeSandboxContext(
                workspace=Path("/tmp/workspace"),
                prepared_environment="image",  # type: ignore[arg-type]
                cpu_limit=1,
                memory_mb=512,
                read_only=False,
                network_enabled=False,
                sandbox_handle="../container",
                task_id="task-contract",
                runtime_user="2001:3001",
            )

    def test_runtime_preparation_boundary_normalizes_valid_concrete_types(self) -> None:
        oci_prepared = oci_preparation("a")
        oci_context_from_helper = RuntimeSandboxContext(
            workspace=Path("/tmp/workspace"),
            prepared_environment=oci_prepared,
            cpu_limit=1,
            memory_mb=512,
            read_only=False,
            network_enabled=False,
            sandbox_handle="a" * 64,
            task_id="task-contract",
            runtime_user="2001:3001",
        )
        self.assertIsInstance(
            oci_context_from_helper.prepared_environment,
            RuntimePreparedEnvironmentData,
        )
        self.assertEqual(
            oci_context_from_helper.prepared_environment.image_reference,
            oci_prepared.image_reference,
        )

        digest = "sha256:" + "b" * 64
        reference = "registry.example.com/team/worker@" + digest
        resolved = ResolvedEnvironment(
            1,
            "default-worker",
            reference,
            digest,
            "linux/amd64",
            "runtime-test",
        )
        prepared = PreparedEnvironment(resolved, "sha256:" + "c" * 64)
        oci_context = RuntimeSandboxContext(
            workspace=Path("/tmp/workspace"),
            prepared_environment=prepared,
            cpu_limit=1,
            memory_mb=512,
            read_only=False,
            network_enabled=False,
            sandbox_handle="b" * 64,
            task_id="task-contract",
            runtime_user="2001:3001",
        )
        self.assertEqual(
            oci_context.prepared_environment.image_reference,
            reference,
        )
        with self.assertRaises(FrozenInstanceError):
            oci_context.prepared_environment.image_reference = None  # type: ignore[misc]

    def test_runtime_preparation_boundary_rejects_incoherent_impostors(self) -> None:
        digest = "sha256:" + "b" * 64
        other_digest = "sha256:" + "c" * 64
        local_id = "sha256:" + "d" * 64
        reference = "registry.example.com/team/worker@" + digest
        base = {
            "executable_image_selector": reference,
            "local_image_config_id": local_id,
            "oci_digest": digest,
            "image_reference": reference,
        }
        mutations = (
            {**base, "image_reference": None, "oci_digest": None,
             "executable_image_selector": "worker:latest"},
            {**base, "executable_image_selector": local_id},
            {**base, "oci_digest": other_digest},
            {**base, "image_reference": None},
            {**base, "oci_digest": None},
            {**base, "local_image_config_id": "sha256:short"},
            {**base, "local_image_config_id": digest},
            {**base, "image_reference": "worker:latest",
             "executable_image_selector": "worker:latest"},
        )
        for values in mutations:
            impostor = SimpleNamespace(**values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                RuntimeSandboxContext(
                    workspace=Path("/tmp/workspace"),
                    prepared_environment=impostor,
                    cpu_limit=1,
                    memory_mb=512,
                    read_only=False,
                    network_enabled=False,
                    sandbox_handle="c" * 64,
                    task_id="task-contract",
                    runtime_user="2001:3001",
                )

    def test_fake_runtime_can_replace_the_adapter_deterministically(self) -> None:
        request = self.request()
        runtime = FakeRuntime(
            [
                FakeRuntimeOutcome.success(
                    output="bounded result\nRUNTIME_TEST_OK\n",
                )
            ]
        )

        result = runtime.execute(request)

        self.assertIsInstance(result, RuntimeResult)
        self.assertEqual(result.output, "bounded result\nRUNTIME_TEST_OK\n")
        self.assertEqual(runtime.requests, [request])

    def test_success_result_cannot_represent_a_nonzero_exit(self) -> None:
        self.assertEqual(
            {field.name for field in fields(RuntimeResult)},
            {"output"},
        )
        runtime = FakeRuntime(
            [FakeRuntimeOutcome(output="result\nRUNTIME_TEST_OK\n", exit_status=7)]
        )
        with self.assertRaises(RuntimeError) as caught:
            runtime.execute(self.request())
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)
        self.assertEqual(caught.exception.exit_status, 7)

    def test_contract_omits_speculative_cancellation_and_transport_mode(self) -> None:
        request_fields = {field.name for field in fields(RuntimeRequest)}
        self.assertNotIn("cancel_requested", request_fields)
        self.assertNotIn("completion_mode", request_fields)
        self.assertNotIn("source_profile", request_fields)
        self.assertNotIn("profile", request_fields)
        self.assertNotIn("execution_name", request_fields)
        self.assertNotIn("output_path", request_fields)

    def test_fake_runtime_normalizes_failure_timeout_and_invalid_result(self) -> None:
        cases = (
            (FakeRuntimeOutcome.failure("failed"), RuntimeErrorKind.EXECUTION_FAILED),
            (FakeRuntimeOutcome.timeout(), RuntimeErrorKind.TIMEOUT),
            (FakeRuntimeOutcome.invalid_result(), RuntimeErrorKind.INVALID_RESULT),
        )
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                runtime = FakeRuntime([outcome])
                with self.assertRaises(RuntimeError) as caught:
                    runtime.execute(self.request())
                self.assertEqual(caught.exception.kind, expected)

    def test_fake_runtime_never_turns_cancellation_into_success(self) -> None:
        runtime = FakeRuntime([FakeRuntimeOutcome.cancelled()])
        with self.assertRaises(RuntimeError) as caught:
            runtime.execute(self.request())
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.CANCELLED)

    def test_fake_runtime_rejects_malformed_outcomes_explicitly(self) -> None:
        with self.assertRaises(TypeError):
            FakeRuntimeOutcome(error_kind="not-a-kind")
        with self.assertRaises(TypeError):
            FakeRuntime([object()])

    def test_fake_runtime_exhaustion_is_normalized(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            FakeRuntime([]).execute(self.request())
        self.assertEqual(
            caught.exception.kind,
            RuntimeErrorKind.RUNTIME_UNAVAILABLE,
        )

    def test_runtime_error_contract_is_strict(self) -> None:
        with self.assertRaises(TypeError):
            RuntimeError("execution_failed", "bad kind")
        with self.assertRaises(TypeError):
            RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "bad status",
                exit_status=True,
            )

    def event(self, **overrides: object) -> RuntimeEvent:
        values: dict[str, object] = {
            "kind": RuntimeEventKind.STARTED,
            "request_id": "runtime-test",
            "role": RuntimeRole.PLANNER,
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return RuntimeEvent(**values)

    def test_runtime_event_contract_is_strict_bounded_and_secret_free(self) -> None:
        event = self.event()
        self.assertEqual(
            {item.name for item in fields(event)},
            {"kind", "request_id", "role", "timestamp"},
        )
        self.assertNotIn("prompt", repr(event).lower())
        with self.assertRaises(TypeError):
            self.event(kind="started")
        with self.assertRaises(TypeError):
            self.event(role="planner")
        with self.assertRaises(ValueError):
            self.event(timestamp=datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            self.event(
                timestamp=datetime(
                    2026,
                    1,
                    1,
                    tzinfo=timezone(timedelta(hours=1)),
                )
            )

    def test_runtime_event_binding_and_order_fail_closed(self) -> None:
        dispatcher = RuntimeEventDispatcher(self.request())
        with self.assertRaises(RuntimeError) as caught:
            dispatcher.emit(self.event(request_id="other-request"))
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.INVALID_RESULT)
        with self.assertRaises(RuntimeError):
            dispatcher.emit(self.event(role=RuntimeRole.WORKER))
        with self.assertRaises(RuntimeError):
            dispatcher.emit(self.event(kind=RuntimeEventKind.HEARTBEAT))
        dispatcher.emit(self.event())
        with self.assertRaises(RuntimeError):
            dispatcher.emit(self.event())

    def test_runtime_event_sink_failure_is_normalized(self) -> None:
        request = self.request(
            on_event=mock.Mock(side_effect=ValueError("sink failed"))
        )
        with self.assertRaises(RuntimeError) as caught:
            RuntimeEventDispatcher(request).emit(self.event())
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)
        self.assertEqual(
            str(caught.exception),
            "Control-plane runtime event sink failed",
        )

    def test_runtime_event_sink_failure_exposes_no_secondary_data(self) -> None:
        control_injection = type(
            "EventSinkFailure\nsecret=event-token",
            (Exception,),
            {},
        )("hostile-message\x00secret=message-token")
        identifier_injection = type(
            "runtime_event_secret",
            (Exception,),
            {},
        )("secondary-message")

        class HostileRepresentation(Exception):
            def __repr__(self) -> str:
                return "hostile-repr\nsecret=repr-token"

        failures = (
            control_injection,
            identifier_injection,
            HostileRepresentation("hostile-message\r\nsecret=message-token"),
        )
        for sink_error in failures:
            with self.subTest(error=type(sink_error).__name__):
                request = self.request(
                    on_event=mock.Mock(side_effect=sink_error)
                )
                with self.assertRaises(RuntimeError) as caught:
                    RuntimeEventDispatcher(request).emit(self.event())

                error = caught.exception
                self.assertEqual(
                    error.kind,
                    RuntimeErrorKind.EXECUTION_FAILED,
                )
                self.assertEqual(
                    str(error),
                    "Control-plane runtime event sink failed",
                )
                record = record_runtime_failure(error, lambda _output: None)
                self.assertEqual(
                    record.failure_reason,
                    "runtime_error[execution_failed]: "
                    "Control-plane runtime event sink failed",
                )

                def fail_persistence(_output: str) -> None:
                    raise type(
                        "TranscriptFailure\nsecret=transcript-token",
                        (Exception,),
                        {},
                    )("hostile transcript message")

                double_failure = record_runtime_failure(
                    error,
                    fail_persistence,
                )
                self.assertEqual(
                    double_failure.failure_reason,
                    "runtime_error[execution_failed]: "
                    "Control-plane runtime event sink failed; "
                    "transcript_persistence_failed",
                )

    def test_runtime_event_sink_does_not_swallow_base_exceptions(self) -> None:
        for base_error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(base_error).__name__):
                request = self.request(
                    on_event=mock.Mock(side_effect=base_error)
                )
                with self.assertRaises(type(base_error)):
                    RuntimeEventDispatcher(request).emit(self.event())

    def test_fake_runtime_scripted_events_are_ordered_and_deterministic(self) -> None:
        delivered: list[RuntimeEvent] = []
        request = self.request(on_event=delivered.append)
        runtime = FakeRuntime(
            [
                FakeRuntimeOutcome.success(
                    output="result\nRUNTIME_TEST_OK\n",
                    events=(
                        RuntimeEventKind.STARTED,
                        RuntimeEventKind.HEARTBEAT,
                    ),
                )
            ]
        )
        runtime.execute(request)
        self.assertEqual(
            [event.kind for event in delivered],
            [RuntimeEventKind.STARTED, RuntimeEventKind.HEARTBEAT],
        )
        self.assertEqual(delivered[0].request_id, request.request_id)
        self.assertEqual(delivered[0].role, request.role)
        self.assertEqual(
            delivered[0].timestamp,
            datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        self.assertLess(delivered[0].timestamp, delivered[1].timestamp)

    def test_fake_runtime_rejects_invalid_or_impossible_event_scripts(self) -> None:
        with self.assertRaises(TypeError):
            FakeRuntimeOutcome(events=("heartbeat",))
        runtime = FakeRuntime(
            [
                FakeRuntimeOutcome.success(
                    output="result\nRUNTIME_TEST_OK\n",
                    events=(RuntimeEventKind.HEARTBEAT,),
                )
            ]
        )
        with self.assertRaises(RuntimeError) as caught:
            runtime.execute(self.request())
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.INVALID_RESULT)

    def test_runtime_failure_projection_is_uniform_and_preserves_output(self) -> None:
        persisted: list[str] = []
        error = RuntimeError(
            RuntimeErrorKind.TIMEOUT,
            "bounded timeout",
            output="partial diagnostics",
        )
        record = record_runtime_failure(error, persisted.append)
        self.assertIsNone(record.exit_code)
        self.assertEqual(
            record.failure_reason,
            "runtime_error[timeout]: bounded timeout",
        )
        self.assertEqual(record.output, "partial diagnostics")
        self.assertEqual(persisted, ["partial diagnostics"])
        self.assertNotIn("partial diagnostics", repr(record))

    def test_runtime_failure_projection_bounds_persistence_failure(self) -> None:
        def fail_persistence(_output: str) -> None:
            raise OSError("private path detail")

        record = record_runtime_failure(
            RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "failed",
                exit_status=7,
            ),
            fail_persistence,
        )
        self.assertEqual(record.exit_code, 7)
        self.assertEqual(
            record.failure_reason,
            "runtime_error[execution_failed]: failed; "
            "transcript_persistence_failed",
        )

    def test_runtime_failure_projection_preserves_all_ordinary_sink_errors(
        self,
    ) -> None:
        class PersistenceFailure(Exception):
            pass

        long_persistence_failure = type(
            "PersistenceFailure" * 16,
            (Exception,),
            {},
        )("bounded persistence failure")
        failures = (
            UnicodeEncodeError("utf-8", "x", 0, 1, "not encodable"),
            ValueError("bad persistence"),
            PersistenceFailure("generic persistence failure"),
            long_persistence_failure,
        )
        for persistence_error in failures:
            with self.subTest(error=type(persistence_error).__name__):
                def fail_persistence(_output: str) -> None:
                    raise persistence_error

                output = "partial-\ud800"
                record = record_runtime_failure(
                    RuntimeError(
                        RuntimeErrorKind.TIMEOUT,
                        "provider stalled",
                        output=output,
                    ),
                    fail_persistence,
                )
                self.assertIsNone(record.exit_code)
                self.assertEqual(record.output, output)
                self.assertEqual(
                    record.failure_reason,
                    "runtime_error[timeout]: provider stalled; "
                    "transcript_persistence_failed",
                )

    def test_runtime_failure_projection_rejects_secondary_data_injection(
        self,
    ) -> None:
        control_injection = type(
            "PersistenceFailure\nsecret=runtime-token",
            (Exception,),
            {},
        )("secondary-message\x00secret=abc")
        valid_identifier_injection = type(
            "runtime_token_secret",
            (Exception,),
            {},
        )("secondary-message")

        class HostileRepresentation(Exception):
            def __repr__(self) -> str:
                return "hostile-repr\nsecret=repr-token"

        failures = (
            control_injection,
            valid_identifier_injection,
            HostileRepresentation("hostile-message\nsecret=message-token"),
        )
        for persistence_error in failures:
            with self.subTest(error=type(persistence_error).__name__):
                def fail_persistence(_output: str) -> None:
                    raise persistence_error

                record = record_runtime_failure(
                    RuntimeError(
                        RuntimeErrorKind.TIMEOUT,
                        "provider stalled",
                        output="partial",
                    ),
                    fail_persistence,
                )
                self.assertEqual(
                    record.failure_reason,
                    "runtime_error[timeout]: provider stalled; "
                    "transcript_persistence_failed",
                )
                self.assertNotIn("secret", record.failure_reason)
                self.assertNotIn("runtime-token", record.failure_reason)
                self.assertNotIn("runtime_token_secret", record.failure_reason)
                self.assertNotIn("\n", record.failure_reason)

    def test_runtime_failure_projection_keeps_primary_kinds_with_fixed_suffix(
        self,
    ) -> None:
        cases = (
            (RuntimeErrorKind.TIMEOUT, None),
            (RuntimeErrorKind.EXECUTION_FAILED, 7),
            (RuntimeErrorKind.INVALID_RESULT, None),
        )
        for kind, exit_status in cases:
            with self.subTest(kind=kind):
                def fail_persistence(_output: str) -> None:
                    raise RuntimeError("secondary sink failure")

                record = record_runtime_failure(
                    RuntimeError(
                        kind,
                        "primary message",
                        exit_status=exit_status,
                        output="primary output",
                    ),
                    fail_persistence,
                )
                self.assertEqual(record.exit_code, exit_status)
                self.assertEqual(record.output, "primary output")
                self.assertEqual(
                    record.failure_reason,
                    f"runtime_error[{kind.value}]: primary message; "
                    "transcript_persistence_failed",
                )

    def test_runtime_failure_projection_does_not_swallow_base_exceptions(
        self,
    ) -> None:
        for base_error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(base_error).__name__):
                def interrupt_persistence(_output: str) -> None:
                    raise base_error

                with self.assertRaises(type(base_error)):
                    record_runtime_failure(
                        RuntimeError(RuntimeErrorKind.TIMEOUT, "primary"),
                        interrupt_persistence,
                    )

    def test_runtime_events_have_no_business_policy_kinds(self) -> None:
        self.assertEqual(
            {kind.value for kind in RuntimeEventKind},
            {"started", "heartbeat"},
        )


class HermesRuntimeMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "repo/compose").mkdir(parents=True)
        (self.root / "repo/scripts").mkdir(parents=True)
        (self.root / "repo/scripts/orchestra-planner-entry.py").touch()
        (self.root / "repo/scripts/orchestra-worker-entry.py").touch()
        (self.root / "repo/compose/images.lock.env").write_text(
            (ROOT / "compose/images.lock.env").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (self.root / "state/hermes-home/profiles").mkdir(parents=True)
        self.runtime = HermesRuntime(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_planner_mapping_preserves_profile_prompt_and_limits(self) -> None:
        request = RuntimeRequest(
            role=RuntimeRole.PLANNER,
            prompt="plan prompt",
            runtime_config_id="ops-orchestrator",
            request_id="test",
            timeout_seconds=45,
            completion_marker="PLAN_OK",
        )

        command = self.runtime.build_command(request)

        self.assertIn(self.runtime.hermes_agent_image, command)
        self.assertIn("orchestra-runtime-container=1", command)
        self.assertIn("orchestra-runtime-request-id=test", command)
        self.assertIn("HERMES_MAX_ITERATIONS=30", command)
        self.assertIn("ops-orchestrator", command)
        self.assertEqual(command[-2:], ["-z", "plan prompt"])

    def test_worker_mapping_preserves_sandbox_and_runtime_profile(self) -> None:
        sandbox = RuntimeSandboxContext(
            workspace=self.root / "worker-clone",
            prepared_environment=oci_preparation("a"),
            cpu_limit=2,
            memory_mb=2048,
            read_only=False,
            network_enabled=False,
            sandbox_handle="a" * 64,
            task_id="task-worker",
            runtime_user="2001:3001",
        )
        request = RuntimeRequest(
            role=RuntimeRole.WORKER,
            prompt="worker prompt",
            runtime_config_id="ops-worker-code",
            request_id="test",
            timeout_seconds=60,
            completion_marker="WORKER_OK",
            sandbox=sandbox,
        )

        command = self.runtime.build_command(request)

        self.assertIn("HERMES_MAX_ITERATIONS=40", command)
        self.assertIn("TERMINAL_DOCKER_NETWORK=false", command)
        self.assertIn(
            f'TERMINAL_DOCKER_VOLUMES=["{sandbox.workspace}:/workspace:rw"]',
            command,
        )
        self.assertIn("ORCHESTRA_SANDBOX_HANDLE=" + "a" * 64, command)
        self.assertIn("ORCHESTRA_SANDBOX_TASK_ID=task-worker", command)
        self.assertIn(
            "ORCHESTRA_SANDBOX_EXECUTABLE_IMAGE="
            + sandbox.prepared_environment.image_reference,
            command,
        )
        self.assertIn(
            "ORCHESTRA_SANDBOX_LOCAL_IMAGE_CONFIG_ID=sha256:"
            + "a" * 64,
            command,
        )
        self.assertFalse(
            any("ORCHESTRA_SANDBOX_IMAGE_ID=" in value for value in command)
        )
        self.assertIn("orchestra-runtime-container=1", command)
        self.assertEqual(command[-4:], ["-p", "test", "-z", request.prompt])

    def test_reviewer_mapping_is_read_only_and_keeps_policy_outside(self) -> None:
        sandbox = RuntimeSandboxContext(
            workspace=self.root / "review-clone",
            prepared_environment=oci_preparation("b"),
            cpu_limit=1,
            memory_mb=1024,
            read_only=True,
            network_enabled=False,
            sandbox_handle="b" * 64,
            task_id="task-reviewer",
            runtime_user="2001:3001",
        )
        request = RuntimeRequest(
            role=RuntimeRole.REVIEWER,
            prompt="review prompt",
            runtime_config_id="ops-reviewer",
            request_id="test",
            timeout_seconds=60,
            completion_marker="REVIEW_OK",
            sandbox=sandbox,
        )

        command = self.runtime.build_command(request)

        self.assertIn("HERMES_MAX_ITERATIONS=50", command)
        self.assertIn(
            f'TERMINAL_DOCKER_VOLUMES=["{sandbox.workspace}:/workspace:ro"]',
            command,
        )
        self.assertIn(
            'TERMINAL_DOCKER_ENV={"GIT_OPTIONAL_LOCKS":"0"}',
            command,
        )
        adapter_source = inspect.getsource(type(self.runtime))
        for policy in ("PASS", "FIX", "BLOCK_HUMAN", "RESUME_SAFE"):
            self.assertNotIn(policy, adapter_source)


class HermesRuntimeExecutionTest(unittest.TestCase):
    class Process:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode
            self.pid = 12345

        def poll(self) -> int | None:
            return self.returncode

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "repo/compose").mkdir(parents=True)
        (self.root / "repo/scripts").mkdir(parents=True)
        (self.root / "repo/scripts/orchestra-planner-entry.py").touch()
        (self.root / "repo/scripts/orchestra-worker-entry.py").touch()
        (self.root / "repo/compose/images.lock.env").write_text(
            (ROOT / "compose/images.lock.env").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (self.root / "state/hermes-home/profiles").mkdir(parents=True)
        self.runtime = HermesRuntime(self.root, poll_interval_seconds=0)
        self.request = RuntimeRequest(
            role=RuntimeRole.PLANNER,
            prompt="plan prompt",
            runtime_config_id="ops-orchestrator",
            request_id="process-test",
            timeout_seconds=30,
            completion_marker="PLAN_OK",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_process_success_returns_normalized_result(self) -> None:
        def launch(*_args: object, **kwargs: object) -> object:
            stream = kwargs["stdout"]
            stream.write("result\nPLAN_OK\n")
            stream.flush()
            return self.Process(0)

        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", side_effect=launch),
            mock.patch.object(self.runtime, "_remove_outer_container"),
        ):
            result = self.runtime.execute(self.request)

        self.assertEqual(result.output, "result\nPLAN_OK\n")

    def test_hermes_runtime_emits_started_before_heartbeat(self) -> None:
        delivered: list[RuntimeEvent] = []
        request = RuntimeRequest(
            role=self.request.role,
            prompt=self.request.prompt,
            runtime_config_id=self.request.runtime_config_id,
            request_id=self.request.request_id,
            timeout_seconds=self.request.timeout_seconds,
            completion_marker=self.request.completion_marker,
            on_event=delivered.append,
        )

        def launch(*_args: object, **kwargs: object) -> object:
            stream = kwargs["stdout"]
            stream.write("result\nPLAN_OK\n")
            stream.flush()
            return self.Process(0)

        timestamps = (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        )
        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", side_effect=launch),
            mock.patch(
                "agent_runtime.hermes.time.monotonic",
                side_effect=(10.0, 16.0),
            ),
            mock.patch.object(
                self.runtime,
                "event_clock",
                side_effect=timestamps,
            ),
        ):
            result = self.runtime.execute(request)

        self.assertEqual(result.output, "result\nPLAN_OK\n")
        self.assertEqual(
            [event.kind for event in delivered],
            [RuntimeEventKind.STARTED, RuntimeEventKind.HEARTBEAT],
        )
        self.assertEqual(
            [event.timestamp for event in delivered],
            list(timestamps),
        )

    def test_private_transcript_cannot_follow_a_caller_symlink(self) -> None:
        victim = self.root / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        (self.root / "planner.log").symlink_to(victim)

        def launch(*_args: object, **kwargs: object) -> object:
            stream = kwargs["stdout"]
            stream.write("result\nPLAN_OK\n")
            stream.flush()
            return self.Process(0)

        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", side_effect=launch),
            mock.patch.object(self.runtime, "_remove_outer_container"),
        ):
            self.runtime.execute(self.request)

        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP")

    def test_process_failure_preserves_partial_output(self) -> None:
        def launch(*_args: object, **kwargs: object) -> object:
            stream = kwargs["stdout"]
            stream.write("partial diagnostics\n")
            stream.flush()
            return self.Process(7)

        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", side_effect=launch),
            mock.patch(
                "agent_runtime.hermes.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(self.request)

        self.assertEqual(caught.exception.exit_status, 7)
        self.assertEqual(caught.exception.output, "partial diagnostics\n")

    def test_malformed_execute_request_is_normalized(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.runtime.execute(object())
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)

    def test_process_failure_and_invalid_output_are_normalized(self) -> None:
        for returncode, expected in (
            (7, RuntimeErrorKind.EXECUTION_FAILED),
            (0, RuntimeErrorKind.INVALID_RESULT),
        ):
            with self.subTest(expected=expected):
                with (
                    mock.patch(
                        "agent_runtime.hermes.subprocess.Popen",
                        return_value=self.Process(returncode),
                    ),
                    mock.patch(
                        "agent_runtime.hermes.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, "", ""),
                    ),
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        self.runtime.execute(self.request)
                    self.assertEqual(caught.exception.kind, expected)

    def test_missing_runtime_dependency_is_normalized_and_cleanup_is_safe(self) -> None:
        with (
            mock.patch(
                "agent_runtime.hermes.subprocess.Popen",
                side_effect=FileNotFoundError(2, "missing", "docker"),
            ),
            mock.patch(
                "agent_runtime.hermes.subprocess.run",
                side_effect=FileNotFoundError(2, "missing", "docker"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(self.request)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.RUNTIME_UNAVAILABLE)

    def test_timeout_is_normalized_and_terminates_the_process(self) -> None:
        process = self.Process(None)
        with (
            mock.patch(
                "agent_runtime.hermes.subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "agent_runtime.hermes.time.monotonic",
                side_effect=(10.0, 41.0),
            ),
            mock.patch.object(
                self.runtime,
                "_capture_outer_container_id",
                return_value="a" * 64,
            ),
            mock.patch.object(self.runtime, "_terminate") as terminate,
            mock.patch.object(self.runtime, "_remove_outer_container"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(self.request)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.TIMEOUT)
        terminate.assert_called_once_with(process)

    def test_timeout_preserves_partial_output(self) -> None:
        process = self.Process(None)

        def launch(*_args: object, **kwargs: object) -> object:
            stream = kwargs["stdout"]
            stream.write("partial timeout diagnostics\n")
            stream.flush()
            return process

        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", side_effect=launch),
            mock.patch(
                "agent_runtime.hermes.time.monotonic",
                side_effect=(10.0, 41.0),
            ),
            mock.patch.object(
                self.runtime,
                "_capture_outer_container_id",
                return_value="a" * 64,
            ),
            mock.patch.object(self.runtime, "_terminate"),
            mock.patch.object(self.runtime, "_remove_outer_container"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(self.request)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.TIMEOUT)
        self.assertEqual(
            caught.exception.output,
            "partial timeout diagnostics\n",
        )

    def test_terminate_reaps_after_sigkill(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired("runtime", 10),
            None,
        )
        with mock.patch("agent_runtime.hermes.os.killpg") as killpg:
            self.runtime._terminate(process)

        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(12345, signal.SIGTERM),
                mock.call(12345, signal.SIGKILL),
            ],
        )

    def test_terminate_still_kills_and_reaps_after_unexpected_wait_error(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = (ChildProcessError("interrupted"), None)
        with mock.patch("agent_runtime.hermes.os.killpg") as killpg:
            with self.assertRaises(ChildProcessError):
                self.runtime._terminate(process)

        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(12345, signal.SIGTERM),
                mock.call(12345, signal.SIGKILL),
            ],
        )

    def test_terminate_reaps_after_sigkill_delivery_error(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired("runtime", 10),
            None,
        )
        with mock.patch(
            "agent_runtime.hermes.os.killpg",
            side_effect=(None, PermissionError("denied")),
        ) as killpg:
            with self.assertRaises(PermissionError):
                self.runtime._terminate(process)

        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(process.wait.call_count, 2)

    def test_terminate_reports_second_wait_error_after_reap_attempt(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired("runtime", 10),
            ChildProcessError("reap failed"),
        )
        with mock.patch("agent_runtime.hermes.os.killpg") as killpg:
            with self.assertRaises(ChildProcessError):
                self.runtime._terminate(process)

        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(process.wait.call_count, 2)

    def test_cleanup_attempts_every_phase_after_terminate_failure(self) -> None:
        process = self.Process(None)
        profile = self.root / "runtime-profile"
        profile.mkdir()
        with (
            mock.patch.object(
                self.runtime,
                "_terminate",
                side_effect=ChildProcessError("not a child"),
            ) as terminate,
            mock.patch.object(self.runtime, "_remove_outer_container") as container,
            mock.patch.object(self.runtime, "_remove_profile") as remove_profile,
        ):
            errors = self.runtime._cleanup(
                process=process,
                execution_name="runtime-process-test",
                execution_container_id="a" * 64,
                profile_directory=profile,
            )

        terminate.assert_called_once_with(process)
        container.assert_called_once_with(
            "a" * 64,
            "runtime-process-test",
        )
        remove_profile.assert_called_once_with(profile)
        self.assertEqual([type(error) for error in errors], [ChildProcessError])

    def test_profile_cleanup_is_attempted_after_container_failure(self) -> None:
        profile = self.root / "runtime-profile"
        profile.mkdir()
        with (
            mock.patch.object(
                self.runtime,
                "_remove_outer_container",
                side_effect=OSError("docker cleanup failed"),
            ),
            mock.patch.object(self.runtime, "_remove_profile") as remove_profile,
        ):
            errors = self.runtime._cleanup(
                process=None,
                execution_name="runtime-process-test",
                execution_container_id="a" * 64,
                profile_directory=profile,
            )
        remove_profile.assert_called_once_with(profile)
        self.assertEqual([type(error) for error in errors], [OSError])

    def test_container_cleanup_command_failure_is_normalized(self) -> None:
        failed = subprocess.CompletedProcess([], 125, "", "permission denied")
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            return_value=failed,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime._remove_outer_container(
                    "a" * 64,
                    "runtime-process-test",
                )
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)

    def test_container_cleanup_revalidates_owner_before_removal(self) -> None:
        owned = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "Id": "a" * 64,
                        "Name": "/process-test",
                        "Config": {
                            "Labels": {
                                "orchestra-runtime-container": "1",
                                "orchestra-runtime-request-id": "process-test",
                            }
                        },
                    }
                ]
            ),
            "",
        )
        removed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            side_effect=(owned, removed),
        ) as run:
            self.runtime._remove_outer_container("a" * 64, "process-test")
        self.assertEqual(run.call_count, 2)

        unowned = copy.deepcopy(json.loads(owned.stdout))
        unowned[0]["Config"]["Labels"] = {}
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(unowned), ""
            ),
        ) as run:
            with self.assertRaises(RuntimeError):
                self.runtime._remove_outer_container(
                    "a" * 64,
                    "process-test",
                )
        run.assert_called_once()

    def test_outer_stop_uses_reinspected_full_id(self) -> None:
        container_id = "a" * 64
        owned = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "Id": container_id,
                        "Name": "/process-test",
                        "Config": {
                            "Labels": {
                                "orchestra-runtime-container": "1",
                                "orchestra-runtime-request-id": "process-test",
                            }
                        },
                    }
                ]
            ),
            "",
        )
        stopped = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            side_effect=(owned, stopped),
        ) as run:
            self.runtime._stop_outer_container(container_id, "process-test")

        self.assertEqual(
            run.call_args_list[1].args[0],
            ["docker", "stop", "--time", "10", container_id],
        )

    def test_outer_stop_never_falls_back_to_reused_name(self) -> None:
        missing = subprocess.CompletedProcess(
            [],
            1,
            "",
            "Error: No such container",
        )
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            return_value=missing,
        ) as run:
            self.runtime._stop_outer_container("a" * 64, "process-test")

        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["docker", "container", "inspect", "a" * 64],
        )

    def test_outer_stop_disappearance_after_inspect_has_no_name_fallback(self) -> None:
        container_id = "a" * 64
        owned = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "Id": container_id,
                        "Name": "/process-test",
                        "Config": {
                            "Labels": {
                                "orchestra-runtime-container": "1",
                                "orchestra-runtime-request-id": "process-test",
                            }
                        },
                    }
                ]
            ),
            "",
        )
        gone = subprocess.CompletedProcess(
            [],
            1,
            "",
            "Error: No such container",
        )
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            side_effect=(owned, gone),
        ) as run:
            self.runtime._stop_outer_container(container_id, "process-test")

        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["docker", "stop", "--time", "10", container_id],
        )

    def test_outer_stop_rejects_wrong_owner_or_request(self) -> None:
        for labels in (
            {},
            {
                "orchestra-runtime-container": "1",
                "orchestra-runtime-request-id": "other-request",
            },
        ):
            with self.subTest(labels=labels):
                inspected = subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        [
                            {
                                "Id": "a" * 64,
                                "Name": "/process-test",
                                "Config": {"Labels": labels},
                            }
                        ]
                    ),
                    "",
                )
                with mock.patch(
                    "agent_runtime.hermes.subprocess.run",
                    return_value=inspected,
                ) as run:
                    with self.assertRaises(RuntimeError):
                        self.runtime._stop_outer_container(
                            "a" * 64,
                            "process-test",
                        )
                run.assert_called_once()

    def test_outer_id_capture_fails_closed_on_foreign_name_collision(self) -> None:
        foreign = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "Id": "b" * 64,
                        "Name": "/process-test",
                        "Config": {"Labels": {}},
                    }
                ]
            ),
            "",
        )
        process = self.Process(None)
        with mock.patch(
            "agent_runtime.hermes.subprocess.run",
            return_value=foreign,
        ) as run:
            with self.assertRaises(RuntimeError):
                self.runtime._capture_outer_container_id(
                    "process-test",
                    process,
                )
        run.assert_called_once()

    def test_primary_error_survives_cleanup_failure(self) -> None:
        process = self.Process(None)
        with (
            mock.patch(
                "agent_runtime.hermes.subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "agent_runtime.hermes.time.monotonic",
                side_effect=(10.0, 41.0),
            ),
            mock.patch.object(
                self.runtime,
                "_capture_outer_container_id",
                return_value="a" * 64,
            ),
            mock.patch.object(self.runtime, "_terminate"),
            mock.patch.object(
                self.runtime,
                "_remove_outer_container",
                side_effect=OSError("cleanup failed"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(self.request)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.TIMEOUT)
        self.assertEqual(caught.exception.secondary_errors, ("OSError",))

    def test_success_with_significant_cleanup_failure_is_not_success(self) -> None:
        def launch(*_args: object, **kwargs: object) -> object:
            stream = kwargs["stdout"]
            stream.write("result\nPLAN_OK\n")
            stream.flush()
            return self.Process(0)

        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", side_effect=launch),
            mock.patch.object(
                self.runtime,
                "_capture_outer_container_id",
                return_value="a" * 64,
            ),
            mock.patch.object(
                self.runtime,
                "_remove_outer_container",
                side_effect=OSError("cleanup failed"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(self.request)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)
        self.assertIn("cleanup failed", str(caught.exception).lower())

    def test_event_sink_exception_is_normalized_and_cleanup_continues(self) -> None:
        request = RuntimeRequest(
            role=self.request.role,
            prompt=self.request.prompt,
            runtime_config_id=self.request.runtime_config_id,
            request_id=self.request.request_id,
            timeout_seconds=self.request.timeout_seconds,
            completion_marker=self.request.completion_marker,
            on_event=mock.Mock(side_effect=ValueError("heartbeat failed")),
        )
        process = self.Process(None)
        with (
            mock.patch("agent_runtime.hermes.subprocess.Popen", return_value=process),
            mock.patch.object(
                self.runtime,
                "_capture_outer_container_id",
                return_value="a" * 64,
            ),
            mock.patch.object(self.runtime, "_terminate") as terminate,
            mock.patch.object(
                self.runtime,
                "_remove_outer_container",
            ) as remove_container,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.execute(request)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)
        self.assertIn("event sink", str(caught.exception))
        terminate.assert_called_once_with(process)
        remove_container.assert_called_once_with("a" * 64, "process-test")

    def test_yaml_sequence_profile_is_invalid_result(self) -> None:
        profile = self.root / "state/hermes-home/profiles/ops-worker-code"
        profile.mkdir()
        (profile / "config.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
        sandbox = RuntimeSandboxContext(
            workspace=self.root / "clone",
            prepared_environment=oci_preparation("c"),
            cpu_limit=1,
            memory_mb=512,
            read_only=False,
            network_enabled=False,
            sandbox_handle="c" * 64,
            task_id="task-profile",
            runtime_user="2001:3001",
        )
        request = RuntimeRequest(
            role=RuntimeRole.WORKER,
            prompt="worker prompt",
            runtime_config_id="ops-worker-code",
            request_id="bad-yaml",
            timeout_seconds=30,
            completion_marker="WORKER_OK",
            sandbox=sandbox,
        )
        with self.assertRaises(RuntimeError) as caught:
            self.runtime._prepare_profile(request)
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.INVALID_RESULT)

    def test_required_role_preflight_fails_before_execution(self) -> None:
        (self.root / "repo/scripts/orchestra-worker-entry.py").unlink()
        with self.assertRaises(RuntimeError) as caught:
            HermesRuntime(self.root, required_role=RuntimeRole.WORKER)
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.RUNTIME_UNAVAILABLE)


class PlannerRuntimeInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        specification = importlib.util.spec_from_file_location(
            "orchestra_planner_runtime_test",
            SCRIPTS / "orchestra-planner.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load planner module")
        cls.planner = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.planner)

    def exercise(self, runtime: FakeRuntime) -> tuple[object, mock.Mock, mock.Mock]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        objective_path = directory / "objective.txt"
        objective_path.write_text("Implement the bounded change", encoding="utf-8")
        payload = {
            "schema_version": 1,
            "objective": "Implement the bounded change",
            "max_parallel_tasks": 1,
            "tasks": [{"project_id": "fixture", "key": "change"}],
        }
        orchestrator = mock.Mock()
        orchestrator.validate_plan.return_value = payload
        orchestrator.insert_plan.return_value = "plan-runtime-test"
        orchestrator.payload_sha256.return_value = "plan-sha256"
        finish = mock.Mock()
        self.last_orchestrator = orchestrator
        self.last_finish = finish
        arguments = SimpleNamespace(
            objective_file=str(objective_path),
            projects="fixture",
            marker="PLANNER_RUNTIME_OK",
            timeout=30,
            expected_task_count=1,
            status="READY",
        )
        with (
            mock.patch("builtins.print"),
            mock.patch.object(self.planner, "EXECUTIONS_ROOT", directory / "runs"),
            mock.patch.object(
                self.planner,
                "project_context",
                return_value=[{"project_id": "fixture"}],
            ),
            mock.patch.object(
                self.planner,
                "load_orchestrator",
                return_value=orchestrator,
            ),
            mock.patch.object(self.planner, "reserve_execution"),
            mock.patch.object(self.planner, "finish_execution", finish),
        ):
            self.planner.command_generate(arguments, runtime=runtime)
        return orchestrator, finish, payload

    def test_planner_success_uses_fake_and_preserves_domain_validation(self) -> None:
        payload = {
            "schema_version": 1,
            "objective": "Implement the bounded change",
            "max_parallel_tasks": 1,
            "tasks": [{"project_id": "fixture", "key": "change"}],
        }
        output = (
            "ORCHESTRA_PLAN_JSON_BEGIN\n"
            + json.dumps(payload)
            + "\nORCHESTRA_PLAN_JSON_END\nPLANNER_RUNTIME_OK\n"
        )
        runtime = FalsyFakeRuntime([FakeRuntimeOutcome.success(output=output)])

        orchestrator, finish, _payload = self.exercise(runtime)

        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.requests[0].role, RuntimeRole.PLANNER)
        orchestrator.validate_plan.assert_called_once()
        orchestrator.insert_plan.assert_called_once()
        self.assertIsNone(finish.call_args.kwargs["failure_reason"])
        self.assertEqual(finish.call_args.kwargs["exit_code"], 0)

    def test_planner_timeout_propagates_without_false_success(self) -> None:
        runtime = FakeRuntime([FakeRuntimeOutcome.timeout()])

        with self.assertRaises(RuntimeError) as caught:
            self.exercise(runtime)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.TIMEOUT)
        self.last_orchestrator.insert_plan.assert_not_called()
        self.assertTrue(
            self.last_finish.call_args.kwargs["failure_reason"].startswith(
                "runtime_error[timeout]:"
            )
        )
        self.assertIsNone(self.last_finish.call_args.kwargs["exit_code"])

    def test_planner_persistence_failure_preserves_runtime_error(self) -> None:
        runtime = FakeRuntime([FakeRuntimeOutcome.timeout()])
        injected = type(
            "planner_secret_token",
            (Exception,),
            {},
        )("secret planner persistence")
        with mock.patch.object(
            self.planner,
            "persist_transcript",
            side_effect=injected,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.exercise(runtime)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.TIMEOUT)
        self.last_orchestrator.insert_plan.assert_not_called()
        self.assertIsNone(self.last_finish.call_args.kwargs["exit_code"])
        self.assertIn(
            "runtime_error[timeout]:",
            self.last_finish.call_args.kwargs["failure_reason"],
        )
        self.assertIn(
            "transcript_persistence_failed",
            self.last_finish.call_args.kwargs["failure_reason"],
        )
        self.assertNotIn(
            "planner_secret_token",
            self.last_finish.call_args.kwargs["failure_reason"],
        )

    def test_planner_invalid_result_kind_is_durable_without_exit_code(self) -> None:
        runtime = FakeRuntime([FakeRuntimeOutcome.invalid_result()])
        with self.assertRaises(RuntimeError):
            self.exercise(runtime)
        self.assertEqual(
            self.last_finish.call_args.kwargs["failure_reason"],
            "runtime_error[invalid_result]: Runtime result is invalid",
        )
        self.assertIsNone(self.last_finish.call_args.kwargs["exit_code"])

    def test_control_plane_transcript_refuses_symlinks(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        victim = directory / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        link = directory / "planner.log"
        link.symlink_to(victim)

        with self.assertRaises(OSError):
            self.planner.persist_transcript(link, "replace")

        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP")

    def test_planner_rejects_nonzero_runtime_with_valid_plan(self) -> None:
        payload = {
            "schema_version": 1,
            "objective": "Implement the bounded change",
            "max_parallel_tasks": 1,
            "tasks": [{"project_id": "fixture", "key": "change"}],
        }
        output = (
            "ORCHESTRA_PLAN_JSON_BEGIN\n"
            + json.dumps(payload)
            + "\nORCHESTRA_PLAN_JSON_END\nPLANNER_RUNTIME_OK\n"
        )
        runtime = FakeRuntime([FakeRuntimeOutcome(output=output, exit_status=7)])

        with self.assertRaises(RuntimeError) as caught:
            self.exercise(runtime)

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)
        self.last_orchestrator.insert_plan.assert_not_called()
        self.assertEqual(self.last_finish.call_args.kwargs["exit_code"], 7)

    def test_planner_keeps_invalid_plan_in_the_domain_boundary(self) -> None:
        runtime = FakeRuntime(
            [
                FakeRuntimeOutcome.success(
                    output=(
                        "ORCHESTRA_PLAN_JSON_BEGIN\n{invalid json\n"
                        "ORCHESTRA_PLAN_JSON_END\nPLANNER_RUNTIME_OK\n"
                    )
                )
            ]
        )
        with self.assertRaises(self.planner.PlannerError):
            self.exercise(runtime)
        self.last_orchestrator.insert_plan.assert_not_called()
        self.assertEqual(self.last_finish.call_args.kwargs["exit_code"], 0)


class WorkerRuntimeInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        specification = importlib.util.spec_from_file_location(
            "orchestra_worker_runtime_test",
            SCRIPTS / "orchestra-worker.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load worker module")
        cls.worker = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.worker)

    def exercise(
        self,
        outcome: FakeRuntimeOutcome,
        *,
        result_commit: str = "b" * 40,
        runtime_type: type[FakeRuntime] = FakeRuntime,
    ) -> tuple[FakeRuntime, mock.Mock]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        instruction = directory / "instruction.txt"
        instruction.write_text("Make the bounded commit", encoding="utf-8")
        repository = directory / "repository"
        worktree = directory / "worktree"
        clone = directory / "clones/run/clone"
        for path in (repository, worktree, clone):
            path.mkdir(parents=True)
        base_commit = "a" * 40
        branch = "orchestra/run-test"
        transaction_ref = "refs/heads/" + branch
        run = {
            "run_id": "run-test",
            "project_id": "fixture",
            "branch_name": branch,
            "base_commit": base_commit,
        }
        role = {
            "role_id": "worker",
            "profile_name": "ops-worker-code",
            "cpu_limit": 2,
            "memory_mb": 2048,
        }
        before = {transaction_ref: base_commit}
        after = {transaction_ref: result_commit}
        references = mock.Mock(side_effect=(before, before, after))

        def git_result(path: Path, *arguments: str) -> str:
            if arguments[:1] == ("status",):
                return ""
            if arguments == ("rev-parse", "HEAD"):
                return result_commit
            if arguments == ("branch", "--show-current"):
                return branch
            if arguments == ("remote",):
                return ""
            raise AssertionError(f"Unexpected Git query: {path} {arguments}")

        completed = subprocess.CompletedProcess([], 0, "", "")
        runtime = runtime_type([outcome])
        finish = mock.Mock()
        precreate = mock.Mock(
            return_value=("a" * 64, {"verified": True}, completed)
        )
        self.last_finish = finish
        self.last_worker_precreate = precreate
        self.last_worker_heartbeat = mock.Mock()
        connection = mock.MagicMock()
        arguments = SimpleNamespace(
            run="run-test",
            role="worker",
            instruction_file=str(instruction),
            marker="WORKER_RUNTIME_OK",
            timeout=30,
        )
        with (
            mock.patch("builtins.print"),
            mock.patch.object(self.worker, "EXECUTIONS_ROOT", directory / "runs"),
            mock.patch.object(self.worker, "CLONES_ROOT", directory / "clones"),
            mock.patch.object(
                self.worker,
                "prepare_worker_environment",
                return_value=oci_preparation("d"),
            ),
            mock.patch.object(self.worker, "connect", return_value=connection),
            mock.patch.object(self.worker, "load_role", return_value=role),
            mock.patch.object(self.worker, "load_run", return_value=run),
            mock.patch.object(self.worker, "verify_worktree", return_value=(repository, worktree)),
            mock.patch.object(self.worker, "git_references", references),
            mock.patch.object(self.worker, "git", side_effect=git_result),
            mock.patch.object(self.worker, "reserve_execution"),
            mock.patch.object(
                self.worker,
                "heartbeat",
                new=self.last_worker_heartbeat,
            ),
            mock.patch.object(self.worker, "prepare_worker_clone", return_value=clone),
            mock.patch.object(
                self.worker,
                "precreate_worker_sandbox",
                new=precreate,
            ),
            mock.patch.object(self.worker, "nested_docker", return_value=completed),
            mock.patch.object(self.worker, "run_command", return_value=completed),
            mock.patch.object(self.worker, "cleanup_created_sandboxes"),
            mock.patch.object(self.worker, "finish_execution", finish),
            mock.patch.object(
                self.worker,
                "create_runtime",
                side_effect=AssertionError("default runtime must not be constructed"),
            ),
        ):
            self.worker.command_launch(arguments, runtime=runtime)
        return runtime, finish

    def test_worker_fake_accepts_valid_commit_through_git_boundary(self) -> None:
        runtime, finish = self.exercise(
            FakeRuntimeOutcome.success(output="WORKER_RUNTIME_OK\n")
        )
        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.requests[0].role, RuntimeRole.WORKER)
        self.assertEqual(runtime.requests[0].sandbox.sandbox_handle, "a" * 64)
        self.assertFalse(runtime.requests[0].sandbox.network_enabled)
        self.assertNotIn(
            "runtime_profile",
            self.last_worker_precreate.call_args.kwargs,
        )
        self.assertTrue(finish.call_args.kwargs["success"])
        self.assertEqual(finish.call_args.kwargs["exit_code"], 0)
        self.assertEqual(
            finish.call_args.kwargs["result"]["result_commit"],
            "b" * 40,
        )
        output_path = Path(finish.call_args.kwargs["result"]["output_path"])
        self.assertEqual(output_path.read_text(encoding="utf-8"), "WORKER_RUNTIME_OK\n")

    def test_worker_uses_injected_falsy_runtime(self) -> None:
        runtime, finish = self.exercise(
            FakeRuntimeOutcome.success(output="WORKER_RUNTIME_OK\n"),
            runtime_type=FalsyFakeRuntime,
        )
        self.assertFalse(bool(runtime))
        self.assertEqual(len(runtime.requests), 1)
        self.assertTrue(finish.call_args.kwargs["success"])

    def test_worker_heartbeat_is_driven_by_runtime_event(self) -> None:
        runtime, _finish = self.exercise(
            FakeRuntimeOutcome.success(
                output="WORKER_RUNTIME_OK\n",
                events=(
                    RuntimeEventKind.STARTED,
                    RuntimeEventKind.HEARTBEAT,
                ),
            )
        )
        self.last_worker_heartbeat.assert_called_once_with(
            "run-test",
            runtime.requests[0].sandbox.task_id,
        )

    def test_worker_precreates_a_runtime_neutral_sandbox(self) -> None:
        sandbox_id = "e" * 64
        completed = subprocess.CompletedProcess([], 0, "", "")

        def nested(*arguments: str, **_kwargs: object) -> object:
            if arguments[:1] == ("run",):
                return subprocess.CompletedProcess([], 0, sandbox_id + "\n", "")
            return completed

        with (
            mock.patch.object(self.worker, "nested_docker", side_effect=nested) as docker,
            mock.patch.object(
                self.worker,
                "audit_sandbox",
                return_value={"verified": True},
            ),
        ):
            handle, _audit, _preflight = self.worker.precreate_worker_sandbox(
                container_name="orchestra-sandbox-test",
                task_id="task-" + "1" * 32,
                runtime_request_id="agent-runtime-123456789abc",
                clone=Path("/tmp/worker-clone"),
                prepared_environment=oci_preparation("f"),
                cpu_limit=2,
                memory_mb=2048,
                branch_name="orchestra/test",
                base_commit="a" * 40,
            )

        run_arguments = next(
            call.args for call in docker.call_args_list if call.args[:1] == ("run",)
        )
        rendered = " ".join(run_arguments)
        self.assertEqual(handle, sandbox_id)
        self.assertIn("orchestra-sandbox=1", rendered)
        self.assertIn("orchestra-runtime-request-id=agent-runtime-", rendered)
        for forbidden in ("hermes-agent", "hermes-task-id", "hermes-profile"):
            self.assertNotIn(forbidden, rendered)
        self.assertFalse(
            any(call.args[:2] == ("rm", "-f") for call in docker.call_args_list)
        )

    def test_worker_name_collision_never_triggers_predelete(self) -> None:
        def collision(*arguments: str, **_kwargs: object) -> object:
            if arguments[:1] == ("run",):
                raise self.worker.WorkerError("name already in use")
            raise AssertionError(f"Unexpected command: {arguments}")

        with mock.patch.object(
            self.worker,
            "nested_docker",
            side_effect=collision,
        ) as docker:
            with self.assertRaises(self.worker.WorkerError):
                self.worker.precreate_worker_sandbox(
                    container_name="orchestra-sandbox-collision",
                    task_id="task-" + "1" * 32,
                    runtime_request_id="agent-runtime-123456789abc",
                    clone=Path("/tmp/worker-clone"),
                    prepared_environment=oci_preparation("f"),
                    cpu_limit=2,
                    memory_mb=2048,
                    branch_name="orchestra/test",
                    base_commit="a" * 40,
                )

        self.assertEqual(len(docker.call_args_list), 1)
        self.assertEqual(docker.call_args.args[:1], ("run",))

    def owned_sandbox_document(self) -> dict[str, object]:
        preparation = oci_preparation("f")
        return {
            "Id": "e" * 64,
            "Image": "sha256:" + "f" * 64,
            "State": {"Status": "running"},
            "Config": {
                "Image": preparation.image_reference,
                "User": self.worker.RUNTIME_USER,
                "Labels": {
                    "orchestra-sandbox": "1",
                    "orchestra-task-id": "task-" + "1" * 32,
                    "orchestra-runtime-request-id": (
                        "agent-runtime-123456789abc"
                    ),
                }
            },
            "Mounts": [
                {
                    "Source": "/tmp/worker-clone",
                    "Destination": "/workspace",
                    "RW": True,
                }
            ],
        }

    def test_worker_sweep_ignores_unowned_same_image_and_mount(self) -> None:
        container = self.owned_sandbox_document()
        container["Config"]["Labels"] = {}
        completed = subprocess.CompletedProcess([], 0, "e" * 64 + "\n", "")
        with (
            mock.patch.object(
                self.worker,
                "nested_docker",
                return_value=completed,
            ) as docker,
            mock.patch.object(
                self.worker,
                "inspect_nested_container",
                return_value=container,
            ),
        ):
            self.worker.cleanup_created_sandboxes(
                baseline_ids=set(),
                clone=Path("/tmp/worker-clone"),
                prepared_environment=oci_preparation("f"),
                task_id="task-" + "1" * 32,
                runtime_request_id="agent-runtime-123456789abc",
            )
        self.assertFalse(
            any(call.args[:2] == ("rm", "-f") for call in docker.call_args_list)
        )

    def test_worker_sweep_rejects_wrong_binding_or_policy(self) -> None:
        mutations = (
            lambda data: data["Config"]["Labels"].__setitem__(
                "orchestra-task-id", "task-" + "2" * 32
            ),
            lambda data: data["Config"]["Labels"].__setitem__(
                "orchestra-runtime-request-id", "agent-runtime-abcdef123456"
            ),
            lambda data: data.__setitem__("Image", "sha256:" + "0" * 64),
            lambda data: data["Mounts"][0].__setitem__(
                "Source", "/tmp/foreign-clone"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                container = copy.deepcopy(self.owned_sandbox_document())
                mutate(container)
                self.assertIsNone(
                    self.worker.authorized_sandbox_container_id(
                        container,
                        candidate_id="e" * 64,
                        task_id="task-" + "1" * 32,
                        runtime_request_id="agent-runtime-123456789abc",
                        clone=Path("/tmp/worker-clone"),
                        prepared_environment=oci_preparation("f"),
                        read_only=False,
                    )
                )

    def test_worker_sweep_removes_exact_owned_sandbox_by_full_id(self) -> None:
        container = self.owned_sandbox_document()
        commands: list[tuple[str, ...]] = []

        def nested(*arguments: str, **_kwargs: object) -> object:
            commands.append(arguments)
            if arguments[:2] == ("ps", "-aq"):
                return subprocess.CompletedProcess(
                    arguments, 0, "e" * 64 + "\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with (
            mock.patch.object(self.worker, "nested_docker", side_effect=nested),
            mock.patch.object(
                self.worker,
                "inspect_nested_container",
                return_value=container,
            ),
        ):
            self.worker.cleanup_created_sandboxes(
                baseline_ids=set(),
                clone=Path("/tmp/worker-clone"),
                prepared_environment=oci_preparation("f"),
                task_id="task-" + "1" * 32,
                runtime_request_id="agent-runtime-123456789abc",
            )
        self.assertIn(("rm", "-f", "e" * 64), commands)

    def test_worker_sweep_never_inspects_a_baseline_container(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "e" * 64 + "\n", "")
        with (
            mock.patch.object(
                self.worker,
                "nested_docker",
                return_value=completed,
            ),
            mock.patch.object(
                self.worker,
                "inspect_nested_container",
            ) as inspect_container,
        ):
            self.worker.cleanup_created_sandboxes(
                baseline_ids={"e" * 64},
                clone=Path("/tmp/worker-clone"),
                prepared_environment=oci_preparation("f"),
                task_id="task-" + "1" * 32,
                runtime_request_id="agent-runtime-123456789abc",
            )
        inspect_container.assert_not_called()

    def test_worker_fake_effect_completes_a_real_git_transaction(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        repository = directory / "repository"
        worktree = directory / "worktree"
        clone = directory / "clones/clone"
        instruction = directory / "instruction.txt"
        instruction.write_text("Commit the bounded change", encoding="utf-8")

        def run_git(path: Path, *arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(path), *arguments],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        subprocess.run(
            ["git", "init", "-b", "main", str(repository)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run_git(repository, "config", "user.name", "Runtime Test")
        run_git(repository, "config", "user.email", "runtime@example.invalid")
        (repository / "fixture.txt").write_text("base\n", encoding="utf-8")
        run_git(repository, "add", "fixture.txt")
        run_git(repository, "commit", "-m", "base")
        base_commit = run_git(repository, "rev-parse", "HEAD")
        branch = "orchestra/run-real"
        run_git(repository, "branch", branch)
        run_git(repository, "worktree", "add", str(worktree), branch)
        subprocess.run(
            ["git", "clone", "--quiet", str(repository), str(clone)],
            check=True,
        )
        run_git(clone, "checkout", branch)
        run_git(clone, "remote", "remove", "origin")
        run_git(clone, "config", "user.name", "Runtime Test")
        run_git(clone, "config", "user.email", "runtime@example.invalid")

        def commit_effect(request: RuntimeRequest) -> None:
            self.assertEqual(request.sandbox.workspace, clone)
            (clone / "fixture.txt").write_text("changed\n", encoding="utf-8")
            run_git(clone, "add", "fixture.txt")
            run_git(clone, "commit", "-m", "bounded change")

        runtime = FakeRuntime(
            [
                FakeRuntimeOutcome.success(
                    output="WORKER_RUNTIME_OK\n",
                    effect=commit_effect,
                )
            ]
        )
        run = {
            "run_id": "run-real",
            "project_id": "fixture",
            "branch_name": branch,
            "base_commit": base_commit,
        }
        role = {
            "role_id": "worker",
            "profile_name": "ops-worker-code",
            "cpu_limit": 2,
            "memory_mb": 2048,
        }
        completed = subprocess.CompletedProcess([], 0, "", "")
        finish = mock.Mock()
        arguments = SimpleNamespace(
            run="run-real",
            role="worker",
            instruction_file=str(instruction),
            marker="WORKER_RUNTIME_OK",
            timeout=30,
        )
        with (
            mock.patch("builtins.print"),
            mock.patch.object(self.worker, "EXECUTIONS_ROOT", directory / "runs"),
            mock.patch.object(self.worker, "CLONES_ROOT", directory / "clones"),
            mock.patch.object(
                self.worker,
                "prepare_worker_environment",
                return_value=oci_preparation("f"),
            ),
            mock.patch.object(self.worker, "connect", return_value=mock.MagicMock()),
            mock.patch.object(self.worker, "load_role", return_value=role),
            mock.patch.object(self.worker, "load_run", return_value=run),
            mock.patch.object(self.worker, "verify_worktree", return_value=(repository, worktree)),
            mock.patch.object(self.worker, "reserve_execution"),
            mock.patch.object(self.worker, "prepare_worker_clone", return_value=clone),
            mock.patch.object(
                self.worker,
                "precreate_worker_sandbox",
                return_value=("b" * 64, {"verified": True}, completed),
            ),
            mock.patch.object(self.worker, "nested_docker", return_value=completed),
            mock.patch.object(self.worker, "cleanup_created_sandboxes"),
            mock.patch.object(self.worker, "finish_execution", finish),
            mock.patch.object(
                self.worker,
                "create_runtime",
                side_effect=AssertionError("default runtime must not be constructed"),
            ),
        ):
            self.worker.command_launch(arguments, runtime=runtime)

        result_commit = finish.call_args.kwargs["result"]["result_commit"]
        self.assertNotEqual(result_commit, base_commit)
        self.assertEqual(run_git(worktree, "rev-parse", "HEAD"), result_commit)
        self.assertEqual(run_git(worktree, "status", "--porcelain=v1"), "")

    def test_worker_fake_cannot_hide_missing_commit(self) -> None:
        with self.assertRaises(self.worker.WorkerError):
            self.exercise(
                FakeRuntimeOutcome.success(output="WORKER_RUNTIME_OK\n"),
                result_commit="a" * 40,
            )

    def test_worker_runtime_error_and_nonzero_never_succeed(self) -> None:
        for outcome in (
            FakeRuntimeOutcome.failure("runtime failed"),
            FakeRuntimeOutcome(output="WORKER_RUNTIME_OK\n", exit_status=7),
        ):
            with self.subTest(outcome=outcome):
                with self.assertRaises(RuntimeError) as caught:
                    self.exercise(outcome)
                self.assertEqual(
                    caught.exception.kind,
                    RuntimeErrorKind.EXECUTION_FAILED,
                )
                self.assertEqual(
                    self.last_finish.call_args.kwargs["exit_code"],
                    outcome.exit_status,
                )
                self.assertTrue(
                    self.last_finish.call_args.kwargs[
                        "failure_reason"
                    ].startswith("runtime_error[execution_failed]:")
                )

    def test_worker_timeout_kind_is_durable_without_exit_code(self) -> None:
        with self.assertRaises(RuntimeError):
            self.exercise(FakeRuntimeOutcome.timeout())
        self.assertTrue(
            self.last_finish.call_args.kwargs["failure_reason"].startswith(
                "runtime_error[timeout]:"
            )
        )
        self.assertIsNone(self.last_finish.call_args.kwargs["exit_code"])

    def test_worker_persistence_failure_preserves_runtime_error(self) -> None:
        injected = type(
            "worker_secret_token",
            (Exception,),
            {},
        )("secret worker persistence")
        with mock.patch.object(
            self.worker,
            "persist_transcript",
            side_effect=injected,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.exercise(
                    FakeRuntimeOutcome(
                        output="partial",
                        exit_status=7,
                        error_kind=RuntimeErrorKind.EXECUTION_FAILED,
                        message="runtime failed",
                    )
                )

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.EXECUTION_FAILED)
        self.assertEqual(self.last_finish.call_args.kwargs["exit_code"], 7)
        self.assertIn(
            "runtime_error[execution_failed]: runtime failed",
            self.last_finish.call_args.kwargs["failure_reason"],
        )
        self.assertIn(
            "transcript_persistence_failed",
            self.last_finish.call_args.kwargs["failure_reason"],
        )
        self.assertNotIn(
            "worker_secret_token",
            self.last_finish.call_args.kwargs["failure_reason"],
        )

    def test_default_worker_preflight_precedes_control_plane_side_effects(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "repo/scripts").mkdir(parents=True)
        prepare_environment = mock.Mock()
        with (
            mock.patch.object(self.worker, "ROOT", root),
            mock.patch.object(
                self.worker,
                "prepare_worker_environment",
                prepare_environment,
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.worker.command_launch(mock.Mock(), runtime=None)
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.RUNTIME_UNAVAILABLE)
        prepare_environment.assert_not_called()


class ReviewerRuntimeInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installed = tempfile.TemporaryDirectory()
        installed_root = Path(cls.installed.name)
        (installed_root / "repo").symlink_to(ROOT, target_is_directory=True)
        specification = importlib.util.spec_from_file_location(
            "orchestra_reviewer_runtime_test",
            SCRIPTS / "orchestra-reviewer.py",
        )
        if specification is None or specification.loader is None:
            raise AssertionError("Unable to load reviewer module")
        cls.reviewer = importlib.util.module_from_spec(specification)
        with mock.patch.dict(os.environ, {"ORCHESTRA_ROOT": str(installed_root)}):
            specification.loader.exec_module(cls.reviewer)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.installed.cleanup()

    def valid_output(self) -> str:
        payload = {
            "decision": "APPROVE",
            "verdict": "PASS",
            "summary": "The isolated change is valid",
            "findings": [],
            "checks": ["read-only"],
        }
        return (
            "ORCHESTRA_REVIEW_JSON_BEGIN\n"
            + json.dumps(payload)
            + "\nORCHESTRA_REVIEW_JSON_END\nREVIEW_RUNTIME_OK\n"
        )

    def exercise(
        self,
        outcome: FakeRuntimeOutcome,
        *,
        mutate_after: bool = False,
        runtime_type: type[FakeRuntime] = FakeRuntime,
    ) -> tuple[FakeRuntime, mock.Mock]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        instruction = directory / "review.txt"
        instruction.write_text("Review the bounded commit", encoding="utf-8")
        repository = directory / "repository"
        worktree = directory / "worktree"
        clone = directory / "clones/run/clone"
        for path in (repository, worktree, clone):
            path.mkdir(parents=True)
        base_commit = "a" * 40
        result_commit = "b" * 40
        branch = "orchestra/run-test"
        run = {
            "run_id": "run-test",
            "project_id": "fixture",
            "branch_name": branch,
            "base_commit": base_commit,
        }
        role = {
            "role_id": "reviewer",
            "profile_name": "ops-reviewer",
            "cpu_limit": 1,
            "memory_mb": 1024,
        }
        stable_refs = {"refs/heads/" + branch: result_commit}
        changed_refs = dict(stable_refs, **{"refs/heads/forbidden": "c" * 40})
        reference_results = [stable_refs, stable_refs, stable_refs]
        reference_results.append(changed_refs if mutate_after else stable_refs)

        def git_result(path: Path, *arguments: str) -> str:
            if arguments == ("rev-parse", "HEAD"):
                return result_commit
            if arguments[:1] == ("status",):
                return ""
            raise AssertionError(f"Unexpected Git query: {path} {arguments}")

        completed = subprocess.CompletedProcess([], 0, "", "")
        runtime = runtime_type([outcome])
        finish = mock.Mock()
        precreate = mock.Mock(
            return_value=("c" * 64, {"verified": True}, completed)
        )
        self.last_reviewer_finish = finish
        self.last_reviewer_precreate = precreate
        self.last_reviewer_heartbeat = mock.Mock()
        connection = mock.MagicMock()
        arguments = SimpleNamespace(
            run="run-test",
            role="reviewer",
            assignment="assignment-test",
            instruction_file=str(instruction),
            marker="REVIEW_RUNTIME_OK",
            timeout=30,
        )
        with (
            mock.patch("builtins.print"),
            mock.patch.object(self.reviewer, "EXECUTIONS_ROOT", directory / "runs"),
            mock.patch.object(self.reviewer, "CLONES_ROOT", directory / "clones"),
            mock.patch.object(self.reviewer, "validate_controller_schema"),
            mock.patch.object(
                self.reviewer.ASSIGNMENTS,
                "validate_assignment_id",
                return_value="assignment-test",
            ),
            mock.patch.object(
                self.reviewer.WORKER,
                "prepare_worker_environment",
                return_value=oci_preparation("e"),
            ),
            mock.patch.object(self.reviewer, "connect", return_value=connection),
            mock.patch.object(self.reviewer, "load_role", return_value=role),
            mock.patch.object(self.reviewer, "load_run", return_value=run),
            mock.patch.object(
                self.reviewer,
                "verify_transaction",
                return_value=(repository, worktree, result_commit),
            ),
            mock.patch.object(
                self.reviewer,
                "git_references",
                side_effect=reference_results,
            ),
            mock.patch.object(self.reviewer, "git", side_effect=git_result),
            mock.patch.object(self.reviewer, "reserve_review"),
            mock.patch.object(
                self.reviewer,
                "heartbeat",
                new=self.last_reviewer_heartbeat,
            ),
            mock.patch.object(self.reviewer, "prepare_review_clone", return_value=clone),
            mock.patch.object(
                self.reviewer,
                "precreate_reviewer_sandbox",
                new=precreate,
            ),
            mock.patch.object(
                self.reviewer,
                "audit_reviewer_sandbox",
                return_value={"read_only": True},
            ),
            mock.patch.object(self.reviewer, "nested_docker", return_value=completed),
            mock.patch.object(self.reviewer, "make_clone_writable"),
            mock.patch.object(self.reviewer, "finish_review", finish),
            mock.patch.object(
                self.reviewer,
                "create_runtime",
                side_effect=AssertionError("default runtime must not be constructed"),
            ),
        ):
            self.reviewer.command_launch(arguments, runtime=runtime)
        return runtime, finish

    def test_reviewer_fake_processes_valid_payload_and_read_only_audit(self) -> None:
        runtime, finish = self.exercise(
            FakeRuntimeOutcome.success(output=self.valid_output())
        )
        self.assertEqual(len(runtime.requests), 1)
        self.assertTrue(runtime.requests[0].sandbox.read_only)
        self.assertEqual(runtime.requests[0].sandbox.sandbox_handle, "c" * 64)
        self.assertFalse(runtime.requests[0].sandbox.network_enabled)
        self.assertNotIn(
            "runtime_profile",
            self.last_reviewer_precreate.call_args.kwargs,
        )
        self.assertTrue(finish.call_args.kwargs["success"])
        self.assertTrue(finish.call_args.kwargs["repository_unchanged"])
        self.assertEqual(finish.call_args.kwargs["review"]["verdict"], "PASS")
        output_path = Path(finish.call_args.kwargs["result"]["output_path"])
        self.assertEqual(output_path.read_text(encoding="utf-8"), self.valid_output())

    def test_reviewer_uses_injected_falsy_runtime(self) -> None:
        runtime, finish = self.exercise(
            FakeRuntimeOutcome.success(output=self.valid_output()),
            runtime_type=FalsyFakeRuntime,
        )
        self.assertFalse(bool(runtime))
        self.assertEqual(len(runtime.requests), 1)
        self.assertTrue(finish.call_args.kwargs["success"])

    def test_reviewer_heartbeat_is_driven_by_runtime_event(self) -> None:
        runtime, _finish = self.exercise(
            FakeRuntimeOutcome.success(
                output=self.valid_output(),
                events=(
                    RuntimeEventKind.STARTED,
                    RuntimeEventKind.HEARTBEAT,
                ),
            )
        )
        self.last_reviewer_heartbeat.assert_called_once_with(
            "run-test",
            runtime.requests[0].sandbox.task_id,
        )

    def test_reviewer_precreates_a_runtime_neutral_sandbox(self) -> None:
        sandbox_id = "f" * 64
        completed = subprocess.CompletedProcess([], 0, "", "")

        def nested(*arguments: str, **_kwargs: object) -> object:
            if arguments[:1] == ("run",):
                return subprocess.CompletedProcess([], 0, sandbox_id + "\n", "")
            return completed

        with (
            mock.patch.object(self.reviewer, "nested_docker", side_effect=nested) as docker,
            mock.patch.object(
                self.reviewer,
                "audit_reviewer_sandbox",
                return_value={"verified": True},
            ),
        ):
            handle, _audit, _preflight = self.reviewer.precreate_reviewer_sandbox(
                container_name="orchestra-sandbox-test",
                task_id="task-" + "2" * 32,
                runtime_request_id="agent-runtime-abcdef123456",
                clone=Path("/tmp/reviewer-clone"),
                prepared_environment=oci_preparation("1"),
                cpu_limit=1,
                memory_mb=1024,
                branch_name="orchestra/test",
                result_commit="b" * 40,
            )

        run_arguments = next(
            call.args for call in docker.call_args_list if call.args[:1] == ("run",)
        )
        rendered = " ".join(run_arguments)
        self.assertEqual(handle, sandbox_id)
        self.assertIn("orchestra-sandbox=1", rendered)
        self.assertIn("orchestra-runtime-request-id=agent-runtime-", rendered)
        for forbidden in ("hermes-agent", "hermes-task-id", "hermes-profile"):
            self.assertNotIn(forbidden, rendered)
        self.assertFalse(
            any(call.args[:2] == ("rm", "-f") for call in docker.call_args_list)
        )

    def test_reviewer_name_collision_never_triggers_predelete(self) -> None:
        def collision(*arguments: str, **_kwargs: object) -> object:
            if arguments[:1] == ("run",):
                raise self.reviewer.ReviewerError("name already in use")
            raise AssertionError(f"Unexpected command: {arguments}")

        with mock.patch.object(
            self.reviewer,
            "nested_docker",
            side_effect=collision,
        ) as docker:
            with self.assertRaises(self.reviewer.ReviewerError):
                self.reviewer.precreate_reviewer_sandbox(
                    container_name="orchestra-sandbox-collision",
                    task_id="task-" + "2" * 32,
                    runtime_request_id="agent-runtime-abcdef123456",
                    clone=Path("/tmp/reviewer-clone"),
                    prepared_environment=oci_preparation("1"),
                    cpu_limit=1,
                    memory_mb=1024,
                    branch_name="orchestra/test",
                    result_commit="b" * 40,
                )
        self.assertEqual(len(docker.call_args_list), 1)
        self.assertEqual(docker.call_args.args[:1], ("run",))

    def reviewer_cleanup_document(self) -> dict[str, object]:
        preparation = oci_preparation("1")
        return {
            "Id": "f" * 64,
            "Image": "sha256:" + "1" * 64,
            "State": {"Status": "running"},
            "Config": {
                "Image": preparation.image_reference,
                "User": self.reviewer.RUNTIME_USER,
                "Labels": {
                    "orchestra-sandbox": "1",
                    "orchestra-task-id": "task-" + "2" * 32,
                    "orchestra-runtime-request-id": (
                        "agent-runtime-abcdef123456"
                    ),
                }
            },
            "Mounts": [
                {
                    "Source": "/tmp/reviewer-clone",
                    "Destination": "/workspace",
                    "RW": False,
                }
            ],
        }

    def reviewer_audit_document(self) -> dict[str, object]:
        document = self.reviewer_cleanup_document()
        document["HostConfig"] = {
            "NetworkMode": "none",
            "NanoCpus": 1_000_000_000,
            "Memory": 1024 * 1024 * 1024,
            "PidsLimit": 256,
            "Privileged": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "CapDrop": ["ALL"],
        }
        document["NetworkSettings"] = {"Networks": {}}
        document["Config"]["Env"] = ["PATH=/usr/bin"]
        return document

    def audit_reviewer_document(self, document: dict[str, object]) -> dict[str, object]:
        with mock.patch.object(
            self.reviewer,
            "inspect_nested_container",
            return_value=document,
        ):
            return self.reviewer.audit_reviewer_sandbox(
                container_id="f" * 64,
                task_id="task-" + "2" * 32,
                runtime_request_id="agent-runtime-abcdef123456",
                clone=Path("/tmp/reviewer-clone"),
                prepared_environment=oci_preparation("1"),
                cpu_limit=1,
                memory_mb=1024,
            )

    def test_reviewer_real_authority_audit_accepts_canonical_container(self) -> None:
        audit = self.audit_reviewer_document(self.reviewer_audit_document())
        self.assertTrue(audit["read_only_verified"])
        self.assertFalse(audit["workspace_rw"])
        self.assertEqual(
            audit["local_image_config_id"],
            "sha256:" + "1" * 64,
        )

    def test_reviewer_real_authority_audit_rejects_all_identity_drift(self) -> None:
        def config_image(data: dict[str, object]) -> None:
            data["Config"]["Image"] = "sha256:" + "2" * 64

        def local_image(data: dict[str, object]) -> None:
            data["Image"] = "sha256:" + "2" * 64

        def task(data: dict[str, object]) -> None:
            data["Config"]["Labels"]["orchestra-task-id"] = "wrong"

        def request(data: dict[str, object]) -> None:
            data["Config"]["Labels"]["orchestra-runtime-request-id"] = "wrong"

        def owner(data: dict[str, object]) -> None:
            data["Config"]["Labels"]["orchestra-sandbox"] = "0"

        def full_id(data: dict[str, object]) -> None:
            data["Id"] = "e" * 64

        def workspace(data: dict[str, object]) -> None:
            data["Mounts"][0]["Source"] = "/tmp/foreign-clone"

        def writable(data: dict[str, object]) -> None:
            data["Mounts"][0]["RW"] = True

        def docker_socket(data: dict[str, object]) -> None:
            data["Mounts"].append(
                {
                    "Source": "/var/run/docker.sock",
                    "Destination": "/var/run/docker.sock",
                    "RW": True,
                }
            )

        def missing_workspace(data: dict[str, object]) -> None:
            data["Mounts"] = []

        mutations = (
            config_image,
            local_image,
            task,
            request,
            owner,
            full_id,
            workspace,
            writable,
            docker_socket,
            missing_workspace,
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate.__name__):
                document = copy.deepcopy(self.reviewer_audit_document())
                mutate(document)
                with self.assertRaises(self.reviewer.ReviewerError):
                    self.audit_reviewer_document(document)

    def test_reviewer_cleanup_removes_only_verified_full_id(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(
                self.reviewer,
                "inspect_nested_container",
                return_value=self.reviewer_cleanup_document(),
            ),
            mock.patch.object(
                self.reviewer,
                "nested_docker",
                return_value=completed,
            ) as docker,
        ):
            removed = self.reviewer.remove_owned_reviewer_sandbox(
                "f" * 64,
                task_id="task-" + "2" * 32,
                runtime_request_id="agent-runtime-abcdef123456",
                clone=Path("/tmp/reviewer-clone"),
                prepared_environment=oci_preparation("1"),
            )
        self.assertTrue(removed)
        docker.assert_called_once_with("rm", "-f", "f" * 64, check=False)

    def test_reviewer_cleanup_ignores_unowned_container(self) -> None:
        container = self.reviewer_cleanup_document()
        container["Config"]["Labels"] = {}
        with (
            mock.patch.object(
                self.reviewer,
                "inspect_nested_container",
                return_value=container,
            ),
            mock.patch.object(self.reviewer, "nested_docker") as docker,
        ):
            removed = self.reviewer.remove_owned_reviewer_sandbox(
                "f" * 64,
                task_id="task-" + "2" * 32,
                runtime_request_id="agent-runtime-abcdef123456",
                clone=Path("/tmp/reviewer-clone"),
                prepared_environment=oci_preparation("1"),
            )
        self.assertFalse(removed)
        docker.assert_not_called()

    def test_reviewer_cleanup_revalidates_image_and_requires_full_id(self) -> None:
        for mutation, candidate in (
            (lambda data: data["Config"].__setitem__(
                "Image", "sha256:" + "2" * 64
            ), "f" * 64),
            (lambda data: data.__setitem__(
                "Image", "sha256:" + "2" * 64
            ), "f" * 64),
            (lambda _data: None, "orchestra-sandbox-name"),
        ):
            with self.subTest(candidate=candidate, mutation=mutation):
                document = self.reviewer_cleanup_document()
                mutation(document)
                with (
                    mock.patch.object(
                        self.reviewer,
                        "inspect_nested_container",
                        return_value=document,
                    ),
                    mock.patch.object(self.reviewer, "nested_docker") as docker,
                ):
                    removed = self.reviewer.remove_owned_reviewer_sandbox(
                        candidate,
                        task_id="task-" + "2" * 32,
                        runtime_request_id="agent-runtime-abcdef123456",
                        clone=Path("/tmp/reviewer-clone"),
                        prepared_environment=oci_preparation("1"),
                    )
                self.assertFalse(removed)
                docker.assert_not_called()

    def test_reviewer_nonzero_runtime_journals_exit_status(self) -> None:
        with self.assertRaises(RuntimeError):
            self.exercise(
                FakeRuntimeOutcome(output=self.valid_output(), exit_status=7)
            )
        self.assertEqual(
            self.last_reviewer_finish.call_args.kwargs["exit_code"],
            7,
        )
        self.assertTrue(
            self.last_reviewer_finish.call_args.kwargs[
                "failure_reason"
            ].startswith("runtime_error[execution_failed]:")
        )

    def test_reviewer_invalid_payload_and_runtime_error_never_pass(self) -> None:
        outcomes = (
            FakeRuntimeOutcome.success(
                output="{invalid review}\nREVIEW_RUNTIME_OK\n"
            ),
            FakeRuntimeOutcome.failure("runtime failed"),
            FakeRuntimeOutcome(output=self.valid_output(), exit_status=7),
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                with self.assertRaises((self.reviewer.ReviewerError, RuntimeError)):
                    self.exercise(outcome)

    def test_reviewer_invalid_result_kind_is_durable(self) -> None:
        with self.assertRaises(RuntimeError):
            self.exercise(FakeRuntimeOutcome.invalid_result())
        self.assertEqual(
            self.last_reviewer_finish.call_args.kwargs["failure_reason"],
            "runtime_error[invalid_result]: Runtime result is invalid",
        )

    def test_reviewer_persistence_failure_preserves_runtime_error(self) -> None:
        injected = type(
            "reviewer_secret_token",
            (Exception,),
            {},
        )("secret reviewer persistence")
        with mock.patch.object(
            self.reviewer,
            "persist_transcript",
            side_effect=injected,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.exercise(FakeRuntimeOutcome.timeout())

        self.assertEqual(caught.exception.kind, RuntimeErrorKind.TIMEOUT)
        self.assertIsNone(
            self.last_reviewer_finish.call_args.kwargs["exit_code"]
        )
        self.assertIn(
            "runtime_error[timeout]:",
            self.last_reviewer_finish.call_args.kwargs["failure_reason"],
        )
        self.assertIn(
            "transcript_persistence_failed",
            self.last_reviewer_finish.call_args.kwargs["failure_reason"],
        )
        self.assertNotIn(
            "reviewer_secret_token",
            self.last_reviewer_finish.call_args.kwargs["failure_reason"],
        )

    def test_reviewer_fake_pass_cannot_bypass_immutability_checks(self) -> None:
        with self.assertRaises(self.reviewer.ReviewerError) as caught:
            self.exercise(
                FakeRuntimeOutcome.success(output=self.valid_output()),
                mutate_after=True,
            )
        self.assertIn("changed Git state", str(caught.exception))

    def test_reviewer_fake_effect_is_caught_by_real_git_checks(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        repository = directory / "repository"
        worktree = directory / "worktree"
        clone = directory / "clones/clone"
        instruction = directory / "review.txt"
        instruction.write_text("Review without mutation", encoding="utf-8")

        def run_git(path: Path, *arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(path), *arguments],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()

        subprocess.run(
            ["git", "init", "-b", "main", str(repository)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run_git(repository, "config", "user.name", "Runtime Test")
        run_git(repository, "config", "user.email", "runtime@example.invalid")
        (repository / "fixture.txt").write_text("base\n", encoding="utf-8")
        run_git(repository, "add", "fixture.txt")
        run_git(repository, "commit", "-m", "base")
        base_commit = run_git(repository, "rev-parse", "HEAD")
        (repository / "fixture.txt").write_text("result\n", encoding="utf-8")
        run_git(repository, "add", "fixture.txt")
        run_git(repository, "commit", "-m", "result")
        result_commit = run_git(repository, "rev-parse", "HEAD")
        branch = "orchestra/review-real"
        run_git(repository, "branch", branch)
        run_git(repository, "worktree", "add", str(worktree), branch)
        subprocess.run(
            ["git", "clone", "--quiet", str(repository), str(clone)],
            check=True,
        )
        run_git(clone, "checkout", branch)
        run_git(clone, "remote", "remove", "origin")

        def mutate_effect(request: RuntimeRequest) -> None:
            self.assertTrue(request.sandbox.read_only)
            (clone / "tampered.txt").write_text("mutation\n", encoding="utf-8")

        runtime = FakeRuntime(
            [
                FakeRuntimeOutcome.success(
                    output=self.valid_output(),
                    effect=mutate_effect,
                )
            ]
        )
        run = {
            "run_id": "review-real",
            "project_id": "fixture",
            "branch_name": branch,
            "base_commit": base_commit,
        }
        role = {
            "role_id": "reviewer",
            "profile_name": "ops-reviewer",
            "cpu_limit": 1,
            "memory_mb": 1024,
        }
        completed = subprocess.CompletedProcess([], 0, "", "")
        finish = mock.Mock()
        arguments = SimpleNamespace(
            run="review-real",
            role="reviewer",
            assignment="assignment-real",
            instruction_file=str(instruction),
            marker="REVIEW_RUNTIME_OK",
            timeout=30,
        )
        with (
            mock.patch("builtins.print"),
            mock.patch.object(self.reviewer, "EXECUTIONS_ROOT", directory / "runs"),
            mock.patch.object(self.reviewer, "CLONES_ROOT", directory / "clones"),
            mock.patch.object(self.reviewer, "validate_controller_schema"),
            mock.patch.object(
                self.reviewer.ASSIGNMENTS,
                "validate_assignment_id",
                return_value="assignment-real",
            ),
            mock.patch.object(
                self.reviewer.WORKER,
                "prepare_worker_environment",
                return_value=oci_preparation("1"),
            ),
            mock.patch.object(self.reviewer, "connect", return_value=mock.MagicMock()),
            mock.patch.object(self.reviewer, "load_role", return_value=role),
            mock.patch.object(self.reviewer, "load_run", return_value=run),
            mock.patch.object(
                self.reviewer,
                "verify_transaction",
                return_value=(repository, worktree, result_commit),
            ),
            mock.patch.object(self.reviewer, "reserve_review"),
            mock.patch.object(self.reviewer, "prepare_review_clone", return_value=clone),
            mock.patch.object(
                self.reviewer,
                "precreate_reviewer_sandbox",
                return_value=("d" * 64, {"verified": True}, completed),
            ),
            mock.patch.object(
                self.reviewer,
                "audit_reviewer_sandbox",
                return_value={"read_only": True},
            ),
            mock.patch.object(self.reviewer, "nested_docker", return_value=completed),
            mock.patch.object(self.reviewer, "make_clone_writable"),
            mock.patch.object(self.reviewer, "finish_review", finish),
            mock.patch.object(
                self.reviewer,
                "create_runtime",
                side_effect=AssertionError("default runtime must not be constructed"),
            ),
        ):
            with self.assertRaises(self.reviewer.ReviewerError) as caught:
                self.reviewer.command_launch(arguments, runtime=runtime)

        self.assertIn("changed Git state", str(caught.exception))
        self.assertFalse(finish.call_args.kwargs["success"])
        self.assertFalse(finish.call_args.kwargs["repository_unchanged"])

    def test_default_reviewer_preflight_precedes_control_plane_side_effects(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "repo/scripts").mkdir(parents=True)
        validate_schema = mock.Mock()
        with (
            mock.patch.object(self.reviewer, "ROOT", root),
            mock.patch.object(
                self.reviewer,
                "validate_controller_schema",
                validate_schema,
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.reviewer.command_launch(mock.Mock(), runtime=None)
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.RUNTIME_UNAVAILABLE)
        validate_schema.assert_not_called()


class PrivateSandboxAdoptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = (SCRIPTS / "orchestra-worker-entry.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        validator = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_validate_authorized_sandbox"
        )
        namespace: dict[str, object] = {
            "Any": object,
            "Path": Path,
        }
        module = ast.fix_missing_locations(ast.Module(body=[validator]))
        exec(compile(module, "orchestra-worker-entry.py", "exec"), namespace)
        cls.validate = staticmethod(namespace[validator.name])

    def expected(self) -> dict[str, object]:
        return {
            "expected_handle": "a" * 64,
            "expected_task_id": "task-" + "1" * 32,
            "expected_request_id": "agent-runtime-123456789abc",
            "expected_workspace": "/srv/worker-clone",
            "expected_executable_image": "sha256:" + "b" * 64,
            "expected_local_image_config_id": "sha256:" + "b" * 64,
            "expected_read_only": False,
            "expected_network_enabled": False,
            "expected_cpu_limit": 2,
            "expected_memory_mb": 2048,
            "expected_user": "2001:3001",
        }

    def container(self) -> dict[str, object]:
        expected = self.expected()
        return {
            "Id": expected["expected_handle"],
            "Image": expected["expected_local_image_config_id"],
            "State": {"Status": "running"},
            "Config": {
                "Image": expected["expected_executable_image"],
                "Labels": {
                    "orchestra-sandbox": "1",
                    "orchestra-task-id": expected["expected_task_id"],
                    "orchestra-runtime-request-id": expected[
                        "expected_request_id"
                    ],
                },
                "User": expected["expected_user"],
            },
            "HostConfig": {
                "NetworkMode": "none",
                "NanoCpus": 2_000_000_000,
                "Memory": 2048 * 1024 * 1024,
                "PidsLimit": 256,
                "Privileged": False,
                "SecurityOpt": ["no-new-privileges:true"],
                "CapDrop": ["ALL"],
            },
            "NetworkSettings": {"Networks": {}},
            "Mounts": [
                {
                    "Source": "/srv/worker-clone",
                    "Destination": "/workspace",
                    "RW": True,
                }
            ],
        }

    def test_exact_authorized_sandbox_is_adopted(self) -> None:
        self.assertEqual(
            self.validate(self.container(), **self.expected()),
            "running",
        )
        container = self.container()
        container["NetworkSettings"]["Networks"] = {"none": {}}
        self.assertEqual(
            self.validate(container, **self.expected()),
            "running",
        )

    def test_any_identity_or_policy_drift_blocks_adoption(self) -> None:
        mutations = {
            "id": lambda data: data.__setitem__("Id", "c" * 64),
            "owner": lambda data: data["Config"]["Labels"].pop(
                "orchestra-sandbox"
            ),
            "task": lambda data: data["Config"]["Labels"].__setitem__(
                "orchestra-task-id", "task-" + "2" * 32
            ),
            "request": lambda data: data["Config"]["Labels"].__setitem__(
                "orchestra-runtime-request-id",
                "agent-runtime-abcdef123456",
            ),
            "state": lambda data: data["State"].__setitem__(
                "Status", "exited"
            ),
            "image": lambda data: data.__setitem__(
                "Image", "sha256:" + "d" * 64
            ),
            "workspace": lambda data: data["Mounts"][0].__setitem__(
                "Source", "/srv/other"
            ),
            "mode": lambda data: data["Mounts"][0].__setitem__(
                "RW", False
            ),
            "extra_bind": lambda data: data["Mounts"].append(
                {
                    "Type": "bind",
                    "Source": "/srv/extra",
                    "Destination": "/extra",
                    "RW": True,
                }
            ),
            "network_mode": lambda data: data["HostConfig"].__setitem__(
                "NetworkMode", "bridge"
            ),
            "attached_network": lambda data: data["NetworkSettings"].__setitem__(
                "Networks", {"bridge": {}}
            ),
            "cpu": lambda data: data["HostConfig"].__setitem__(
                "NanoCpus", 1_000_000_000
            ),
            "memory": lambda data: data["HostConfig"].__setitem__(
                "Memory", 1024 * 1024 * 1024
            ),
            "pids": lambda data: data["HostConfig"].__setitem__(
                "PidsLimit", 0
            ),
            "privileged": lambda data: data["HostConfig"].__setitem__(
                "Privileged", True
            ),
            "security": lambda data: data["HostConfig"].__setitem__(
                "SecurityOpt", []
            ),
            "capabilities": lambda data: data["HostConfig"].__setitem__(
                "CapDrop", []
            ),
            "user": lambda data: data["Config"].__setitem__(
                "User", "0"
            ),
            "sensitive_environment": lambda data: data["Config"].__setitem__(
                "Env", ["OPENAI_API_KEY=forbidden"]
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                container = copy.deepcopy(self.container())
                mutate(container)
                with self.assertRaises(builtins.RuntimeError):
                    self.validate(container, **self.expected())


class RecoveryOwnershipTest(unittest.TestCase):
    task_id = "task-" + "1" * 32
    request_id = "agent-runtime-123456789abc"

    def generic_sandbox(self) -> dict[str, object]:
        return {
            "Name": "/orchestra-sandbox-123456789abc",
            "Config": {
                "Labels": {
                    "orchestra-sandbox": "1",
                    "orchestra-task-id": self.task_id,
                    "orchestra-runtime-request-id": self.request_id,
                }
            },
        }

    def test_nested_cleanup_ownership_matrix_is_fail_closed(self) -> None:
        generic = self.generic_sandbox()
        binding = {(self.task_id, self.request_id)}
        self.assertEqual(
            nested_container_ownership(generic, known_bindings=binding),
            "NEW_GENERIC",
        )
        unowned = copy.deepcopy(generic)
        unowned["Config"]["Labels"] = {}
        self.assertIsNone(
            nested_container_ownership(unowned, known_bindings=binding)
        )
        spoofed = copy.deepcopy(generic)
        spoofed["Config"]["Labels"]["orchestra-task-id"] = (
            "task-" + "2" * 32
        )
        self.assertIsNone(
            nested_container_ownership(spoofed, known_bindings=binding)
        )
        old_labels = copy.deepcopy(generic)
        old_labels["Config"]["Labels"] = {"hermes-agent": "1"}
        self.assertIsNone(
            nested_container_ownership(old_labels, known_bindings=binding)
        )

    def test_outer_cleanup_requires_labels_identity_and_durable_binding(self) -> None:
        generic = {
            "Name": "/" + self.request_id,
            "Config": {
                "Labels": {
                    "orchestra-runtime-container": "1",
                    "orchestra-runtime-request-id": self.request_id,
                }
            },
        }
        self.assertEqual(
            host_container_ownership(
                generic,
                expected_name=self.request_id,
                known_names={self.request_id},
            ),
            "NEW_GENERIC",
        )
        self.assertIsNone(
            host_container_ownership(
                generic,
                expected_name=self.request_id,
                known_names=set(),
            )
        )
        unowned = copy.deepcopy(generic)
        unowned["Config"]["Labels"] = {}
        self.assertIsNone(
            host_container_ownership(
                unowned,
                expected_name=self.request_id,
                known_names={self.request_id},
            )
        )
        spoofed = copy.deepcopy(generic)
        spoofed["Config"]["Labels"][
            "orchestra-runtime-request-id"
        ] = "agent-runtime-abcdef123456"
        self.assertIsNone(
            host_container_ownership(
                spoofed,
                expected_name=self.request_id,
                known_names={self.request_id},
            )
        )
        old_oneoff = {
            "Name": "/retired-oneoff",
            "Config": {"Labels": {"com.docker.compose.oneoff": "True"}},
        }
        self.assertIsNone(
            host_container_ownership(
                old_oneoff,
                expected_name="retired-oneoff",
                known_names={"retired-oneoff"},
            )
        )

    def test_recovery_discovery_is_current_label_based_only(self) -> None:
        source = (SCRIPTS / "orchestra-recovery.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"name=^/hermesops-"', source)
        for ownership_filter in (
            "label=orchestra-runtime-container=1",
            "label=orchestra-sandbox=1",
        ):
            self.assertIn(ownership_filter, source)
        self.assertNotIn("LEGACY_HERMES", source)

    def test_orphan_cleanup_selects_owned_stale_and_protects_active(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE worker_executions (
                task_id TEXT,
                run_id TEXT,
                outer_container_name TEXT,
                sandbox_container_id TEXT,
                runtime_profile TEXT
            );
            CREATE TABLE reviewer_executions (
                task_id TEXT,
                run_id TEXT,
                outer_container_name TEXT,
                sandbox_container_id TEXT,
                runtime_profile TEXT
            );
            CREATE TABLE orchestrator_executions (
                outer_container_name TEXT,
                finished_at TEXT
            );
            """
        )
        sandbox_id = "a" * 64
        connection.execute(
            "INSERT INTO runs VALUES ('run-test', 'project-test', 'COMPLETED')"
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, 'run-test', 'COMPLETED')",
            (self.task_id,),
        )
        connection.execute(
            "INSERT INTO worker_executions VALUES (?, 'run-test', ?, ?, ?)",
            (self.task_id, self.request_id, sandbox_id, self.request_id),
        )
        connection.commit()
        unowned_outer = "agent-runtime-abcdef123456"
        unowned_sandbox = "b" * 64

        def command(arguments: list[str], **_kwargs: object) -> object:
            rendered = " ".join(arguments)
            if "docker ps -a" in rendered and "orchestra-sandbox=1" in rendered:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    f"{sandbox_id} orchestra-sandbox-123456789abc\n"
                    f"{unowned_sandbox} hermesops-unowned\n",
                    "",
                )
            if "docker ps -a" in rendered and "runtime-container" in rendered:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    self.request_id + "\n" + unowned_outer + "\n",
                    "",
                )
            if "docker ps -a" in rendered:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def inspect_outer(name: str) -> dict[str, object]:
            if name == self.request_id:
                return {
                    "Name": "/" + name,
                    "Config": {
                        "Labels": {
                            "orchestra-runtime-container": "1",
                            "orchestra-runtime-request-id": name,
                        }
                    },
                }
            return {"Name": "/" + name, "Config": {"Labels": {}}}

        def inspect_sandbox(container_id: str) -> dict[str, object]:
            if container_id == sandbox_id:
                return self.generic_sandbox()
            return {
                "Name": "/hermesops-unowned",
                "Config": {"Labels": {}},
            }

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with (
            mock.patch.object(RECOVERY, "connect", return_value=connection),
            mock.patch.object(RECOVERY, "docker_exists", return_value=True),
            mock.patch.object(RECOVERY, "run_command", side_effect=command),
            mock.patch.object(
                RECOVERY,
                "inspect_host_container",
                side_effect=inspect_outer,
            ),
            mock.patch.object(
                RECOVERY,
                "inspect_nested_container",
                side_effect=inspect_sandbox,
            ),
            mock.patch.object(
                RECOVERY, "remove_host_container", return_value=True
            ),
            mock.patch.object(
                RECOVERY, "remove_nested_container", return_value=True
            ),
            mock.patch.object(
                RECOVERY,
                "HERMES_HOME",
                Path(temporary.name) / "hermes-home",
            ),
            mock.patch.object(
                RECOVERY,
                "WORKSPACES",
                Path(temporary.name) / "workspaces",
            ),
        ):
            stale = RECOVERY.cleanup_orphans(dry_run=False)
            connection.execute(
                "UPDATE runs SET status = 'RUNNING' WHERE run_id = 'run-test'"
            )
            connection.execute(
                "UPDATE tasks SET status = 'RUNNING' WHERE task_id = ?",
                (self.task_id,),
            )
            connection.commit()
            active = RECOVERY.cleanup_orphans(dry_run=False)

        stale_names = {
            action.get("name") for action in stale["actions"]
        }
        self.assertIn(self.request_id, stale_names)
        self.assertIn("orchestra-sandbox-123456789abc", stale_names)
        self.assertNotIn(unowned_outer, stale_names)
        self.assertNotIn("hermesops-unowned", stale_names)
        self.assertFalse(
            any(
                action["resource"].endswith("container")
                for action in active["actions"]
            )
        )


class RuntimeBoundarySourceTest(unittest.TestCase):
    def test_primary_call_sites_use_runtime_injection_without_direct_bypass(self) -> None:
        expected = {
            "orchestra-planner.py": "def command_generate",
            "orchestra-worker.py": "def command_launch",
            "orchestra-reviewer.py": "def command_launch",
        }
        for filename, function in expected.items():
            with self.subTest(filename=filename):
                source = (SCRIPTS / filename).read_text(encoding="utf-8")
                self.assertIn("AgentRuntime", source)
                self.assertIn("create_runtime", source)
                self.assertIn("runtime.execute(", source)
                self.assertIn(function, source)
                self.assertNotIn("subprocess.Popen(", source)
                self.assertNotIn('"hermes-agent"', source)
                self.assertNotIn("HermesRuntime", source)

    def test_launchers_do_not_encode_hermes_sandbox_discovery(self) -> None:
        for filename in ("orchestra-worker.py", "orchestra-reviewer.py"):
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                for forbidden in (
                    "hermes-agent=1",
                    "hermes-task-id=",
                    "hermes-profile=",
                    "reused_by_hermes",
                    "runtime-worker-",
                    "runtime-reviewer-",
                    'f"hermesops-worker-',
                    'f"hermesops-reviewer-',
                    "isinstance(runtime",
                ):
                    self.assertNotIn(forbidden, source)

        private_entry = (SCRIPTS / "orchestra-worker-entry.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ORCHESTRA_SANDBOX_HANDLE", private_entry)
        self.assertIn("_find_authorized_sandbox", private_entry)

    def test_adapter_does_not_own_lifecycle_git_or_review_policy(self) -> None:
        source = (SCRIPTS / "agent_runtime/hermes.py").read_text(encoding="utf-8")
        for forbidden in (
            "objective_queue",
            "orchestration_tasks",
            "approvals",
            "integration_executions",
            '"update-ref"',
            '"merge-base"',
            "BLOCK_HUMAN",
            "RESUME_SAFE",
            "ROLLBACK_SAFE",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
