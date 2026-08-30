from __future__ import annotations

import builtins
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from agent_runtime import (  # noqa: E402
    NativeRuntime,
    RuntimeError,
    RuntimeErrorKind,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeRequest,
    RuntimeResult,
    RuntimeRole,
    RuntimeSandboxContext,
)
from model_provider import (  # noqa: E402
    FakeModelProvider,
    FakeModelProviderOutcome,
    ModelMessageRole,
    ModelProviderError,
    ModelProviderErrorKind,
    ModelResult,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from legacy_worker_environment import LegacyLocalEnvironment  # noqa: E402
from sandbox_backend import LegacyPreparedEnvironment  # noqa: E402


EVENT_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class CaptureProvider:
    def __init__(
        self,
        *,
        result: object = ModelResult("native-ok"),
        error: BaseException | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.requests: list[object] = []
        self.timeline = timeline

    def generate(self, request: object) -> object:
        self.requests.append(request)
        if self.timeline is not None:
            self.timeline.append("provider")
        if self.error is not None:
            raise self.error
        return self.result


class NativeRuntimeTest(unittest.TestCase):
    def request(self, **overrides: object) -> RuntimeRequest:
        values: dict[str, object] = {
            "role": RuntimeRole.PLANNER,
            "prompt": "Generate one bounded response",
            "runtime_config_id": "native-config",
            "request_id": "native-request",
            "timeout_seconds": 30,
            "completion_marker": "NATIVE_DONE",
        }
        values.update(overrides)
        return RuntimeRequest(**values)

    def sandbox(self) -> RuntimeSandboxContext:
        return RuntimeSandboxContext(
            workspace=Path("/tmp/native-runtime-workspace"),
            prepared_environment=LegacyPreparedEnvironment(
                LegacyLocalEnvironment(
                    environment_id="default-worker",
                    local_image_config_id="sha256:" + "a" * 64,
                    local_image_tag="hermesops-worker-sandbox:0.2",
                )
            ),
            cpu_limit=2,
            memory_mb=1024,
            read_only=False,
            network_enabled=False,
            sandbox_handle="b" * 64,
            task_id="task-native",
        )

    def runtime(self, provider: object, model: str = "fixed-model") -> NativeRuntime:
        return NativeRuntime(
            provider,
            model,
            event_clock=lambda: EVENT_TIME,
        )

    def assert_safe_error(
        self,
        error: RuntimeError,
        *,
        kind: RuntimeErrorKind,
        message: str,
        forbidden: str = "provider-token",
    ) -> None:
        self.assertEqual(error.kind, kind)
        self.assertEqual(str(error), message)
        self.assertIsNone(error.exit_status)
        self.assertEqual(error.output, "")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = str(error) + repr(error) + "".join(
            traceback.format_exception(error)
        )
        self.assertNotIn(forbidden, rendered)

    def assert_safe_configuration_error(
        self,
        error: BaseException,
        *,
        message: str,
        forbidden: str,
    ) -> None:
        self.assertIs(type(error), TypeError)
        self.assertEqual(str(error), message)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = str(error) + repr(error) + "".join(
            traceback.format_exception(error)
        )
        self.assertNotIn(forbidden, rendered)

    def test_success_maps_one_user_message_fixed_model_and_exact_timeout(self) -> None:
        provider = FakeModelProvider([FakeModelProviderOutcome.success("answer")])
        request = self.request(timeout_seconds=47)

        result = self.runtime(provider, "opaque-model-v2").execute(request)

        self.assertEqual(result, RuntimeResult("answer"))
        self.assertEqual(len(provider.requests), 1)
        model_request = provider.requests[0]
        self.assertEqual(model_request.model, "opaque-model-v2")
        self.assertEqual(model_request.timeout_seconds, 47)
        self.assertEqual(len(model_request.messages), 1)
        self.assertIs(model_request.messages[0].role, ModelMessageRole.USER)
        self.assertEqual(model_request.messages[0].content, request.prompt)
        rendered = repr(model_request)
        for forbidden in (
            request.runtime_config_id,
            request.request_id,
            request.completion_marker,
            request.role.value,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_empty_and_non_json_outputs_remain_successful_primary_data(self) -> None:
        for output in ("", "not-json", "Unicode ✓\n```code```\n"):
            with self.subTest(output=output):
                provider = FakeModelProvider(
                    [FakeModelProviderOutcome.success(output)]
                )
                result = self.runtime(provider).execute(self.request())
                self.assertEqual(result.output, output)

    def test_same_fixed_model_is_used_for_every_runtime_role(self) -> None:
        provider = FakeModelProvider(
            [FakeModelProviderOutcome.success(role.value) for role in RuntimeRole]
        )
        runtime = self.runtime(provider, "one-fixed-model")
        for role in RuntimeRole:
            runtime.execute(
                self.request(
                    role=role,
                    request_id=f"native-{role.value}",
                    sandbox=(None if role is RuntimeRole.PLANNER else self.sandbox()),
                )
            )
        self.assertEqual(
            [request.model for request in provider.requests],
            ["one-fixed-model"] * 3,
        )
        self.assertEqual(
            [request.messages[0].role for request in provider.requests],
            [ModelMessageRole.USER] * 3,
        )

    def test_started_is_bound_ordered_and_immediately_precedes_provider(self) -> None:
        timeline: list[str] = []
        events: list[RuntimeEvent] = []
        provider = CaptureProvider(timeline=timeline)

        def receive(event: RuntimeEvent) -> None:
            events.append(event)
            timeline.append(event.kind.value)

        request = self.request(on_event=receive)
        self.runtime(provider).execute(request)

        self.assertEqual(timeline, ["started", "provider"])
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].kind, RuntimeEventKind.STARTED)
        self.assertEqual(events[0].request_id, request.request_id)
        self.assertIs(events[0].role, request.role)
        self.assertEqual(events[0].timestamp, EVENT_TIME)

    def test_native_runtime_emits_no_synthetic_heartbeat(self) -> None:
        events: list[RuntimeEvent] = []
        provider = CaptureProvider()
        self.runtime(provider).execute(self.request(on_event=events.append))
        self.assertEqual(
            [event.kind for event in events],
            [RuntimeEventKind.STARTED],
        )

    def test_unrepresentable_timeout_fails_before_started_or_provider(self) -> None:
        events: list[RuntimeEvent] = []
        provider = CaptureProvider()
        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(
                self.request(timeout_seconds=601, on_event=events.append)
            )
        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.INVALID_RESULT,
            message=(
                "Native runtime request cannot be represented by the model provider"
            ),
        )
        self.assertEqual(events, [])
        self.assertEqual(provider.requests, [])

    def test_provider_error_kinds_map_to_stable_runtime_errors(self) -> None:
        cases = (
            (
                ModelProviderErrorKind.UNAVAILABLE,
                RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                "Native model provider is unavailable",
            ),
            (
                ModelProviderErrorKind.TIMEOUT,
                RuntimeErrorKind.TIMEOUT,
                "Native model generation timed out",
            ),
            (
                ModelProviderErrorKind.REQUEST_REJECTED,
                RuntimeErrorKind.EXECUTION_FAILED,
                "Native model provider rejected the request",
            ),
            (
                ModelProviderErrorKind.INVALID_RESPONSE,
                RuntimeErrorKind.INVALID_RESULT,
                "Native model provider returned an invalid response",
            ),
            (
                ModelProviderErrorKind.PROVIDER_FAILED,
                RuntimeErrorKind.EXECUTION_FAILED,
                "Native model provider failed",
            ),
        )
        for provider_kind, runtime_kind, message in cases:
            with self.subTest(provider_kind=provider_kind):
                source = ModelProviderError(
                    provider_kind,
                    "secret=provider-token\nprompt=private",
                )
                provider = CaptureProvider(error=source)
                with self.assertRaises(RuntimeError) as caught:
                    self.runtime(provider).execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=runtime_kind,
                    message=message,
                )
                self.assertEqual(len(provider.requests), 1)

    def test_unexpected_provider_exception_is_isolated_and_not_retried(self) -> None:
        hostile = type(
            "HostileProviderFailure\nsecret=provider-token",
            (Exception,),
            {},
        )("prompt=private")
        provider = CaptureProvider(error=hostile)
        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(self.request())
        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.EXECUTION_FAILED,
            message="Native model provider invocation failed",
        )
        self.assertEqual(len(provider.requests), 1)

    def test_hostile_model_provider_error_subclass_fails_closed(self) -> None:
        class HostileModelProviderError(ModelProviderError):
            def __repr__(self) -> str:
                return "secret=provider-token"

        provider = CaptureProvider(
            error=HostileModelProviderError(
                ModelProviderErrorKind.TIMEOUT,
                "secret=provider-token",
            )
        )
        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(self.request())
        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.EXECUTION_FAILED,
            message="Native model provider invocation failed",
        )

        missing = object()
        for kind in (missing, None, 1, "timeout", []):
            with self.subTest(kind=kind):
                malformed_kind = ModelProviderError(
                    ModelProviderErrorKind.TIMEOUT,
                    "secret=provider-token",
                )
                if kind is missing:
                    del malformed_kind.kind
                else:
                    malformed_kind.kind = kind
                with self.assertRaises(RuntimeError) as malformed:
                    self.runtime(CaptureProvider(error=malformed_kind)).execute(
                        self.request()
                    )
                self.assert_safe_error(
                    malformed.exception,
                    kind=RuntimeErrorKind.EXECUTION_FAILED,
                    message="Native model provider invocation failed",
                )

    def test_hostile_exact_model_provider_error_kind_access_is_isolated(self) -> None:
        class HostileKindFailure(Exception):
            pass

        for source in (
            ValueError("secret=provider-token"),
            builtins.RuntimeError("secret=provider-token"),
            HostileKindFailure("secret=provider-token"),
        ):
            with self.subTest(source=type(source).__name__):
                provider_error = ModelProviderError(
                    ModelProviderErrorKind.TIMEOUT,
                    "secret=provider-token",
                )

                def fail_kind(
                    _error: ModelProviderError,
                    failure: Exception = source,
                ) -> object:
                    raise failure

                provider = CaptureProvider(error=provider_error)
                with mock.patch.object(
                    ModelProviderError,
                    "kind",
                    property(fail_kind),
                    create=True,
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        self.runtime(provider).execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=RuntimeErrorKind.EXECUTION_FAILED,
                    message="Native model provider invocation failed",
                )
                self.assertEqual(len(provider.requests), 1)

    def test_model_provider_error_kind_access_base_exceptions_pass_through(
        self,
    ) -> None:
        for source in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(source=type(source).__name__):
                provider_error = ModelProviderError(
                    ModelProviderErrorKind.TIMEOUT,
                    "secret=provider-token",
                )

                def fail_kind(
                    _error: ModelProviderError,
                    failure: BaseException = source,
                ) -> object:
                    raise failure

                provider = CaptureProvider(error=provider_error)
                with mock.patch.object(
                    ModelProviderError,
                    "kind",
                    property(fail_kind),
                    create=True,
                ):
                    with self.assertRaises(type(source)):
                        self.runtime(provider).execute(self.request())
                self.assertEqual(len(provider.requests), 1)

    def test_model_provider_error_ignores_hostile_dict_methods(self) -> None:
        class HostileDict(dict[object, object]):
            def get(self, key: object, default: object = None) -> object:
                raise AssertionError("secret=provider-token")

        provider_error = ModelProviderError(
            ModelProviderErrorKind.TIMEOUT,
            "secret=provider-token",
        )
        provider_error.__dict__ = HostileDict(provider_error.__dict__)
        provider = CaptureProvider(error=provider_error)
        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(self.request())
        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.TIMEOUT,
            message="Native model generation timed out",
        )
        self.assertEqual(len(provider.requests), 1)

    def test_provider_base_exceptions_pass_through(self) -> None:
        for source in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(source=type(source).__name__):
                provider = CaptureProvider(error=source)
                with self.assertRaises(type(source)):
                    self.runtime(provider).execute(self.request())
                self.assertEqual(len(provider.requests), 1)

    def test_malformed_provider_results_never_become_runtime_success(self) -> None:
        class ModelResultSubclass(ModelResult):
            pass

        malformed = (
            None,
            "text",
            {},
            b"bytes",
            object(),
            ModelResultSubclass("subclass"),
        )
        for result in malformed:
            with self.subTest(result=type(result).__name__):
                provider = CaptureProvider(result=result)
                with self.assertRaises(RuntimeError) as caught:
                    self.runtime(provider).execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=RuntimeErrorKind.INVALID_RESULT,
                    message="Native model provider returned an invalid result",
                )
                self.assertEqual(len(provider.requests), 1)

    def test_mutated_exact_model_result_fails_closed(self) -> None:
        class StrSubclass(str):
            pass

        for output_text in (None, b"bytes", 1, StrSubclass("subclass")):
            with self.subTest(output_type=type(output_text).__name__):
                malformed = ModelResult("valid")
                object.__setattr__(malformed, "output_text", output_text)
                provider = CaptureProvider(result=malformed)
                with self.assertRaises(RuntimeError) as caught:
                    self.runtime(provider).execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=RuntimeErrorKind.INVALID_RESULT,
                    message="Native model provider returned an invalid result",
                )
                self.assertEqual(len(provider.requests), 1)

    def test_exact_model_result_with_missing_output_is_invalid_result(self) -> None:
        malformed = ModelResult("valid")
        object.__delattr__(malformed, "output_text")
        provider = CaptureProvider(result=malformed)

        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(self.request())

        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.INVALID_RESULT,
            message="Native model provider returned an invalid result",
        )
        self.assertEqual(len(provider.requests), 1)

    def test_hostile_exact_model_result_output_access_is_isolated(self) -> None:
        class HostileOutputFailure(Exception):
            pass

        for source in (
            ValueError("secret=result-token"),
            builtins.RuntimeError("secret=result-token"),
            HostileOutputFailure("secret=result-token"),
        ):
            with self.subTest(source=type(source).__name__):
                malformed = ModelResult("valid")

                def fail_output(
                    _result: ModelResult,
                    failure: Exception = source,
                ) -> object:
                    raise failure

                provider = CaptureProvider(result=malformed)
                with mock.patch.object(
                    ModelResult,
                    "output_text",
                    property(fail_output),
                    create=True,
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        self.runtime(provider).execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=RuntimeErrorKind.INVALID_RESULT,
                    message="Native model provider returned an invalid result",
                    forbidden="result-token",
                )
                self.assertEqual(len(provider.requests), 1)

    def test_model_result_output_access_base_exceptions_pass_through(self) -> None:
        for source in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(source=type(source).__name__):
                malformed = ModelResult("valid")

                def fail_output(
                    _result: ModelResult,
                    failure: BaseException = source,
                ) -> object:
                    raise failure

                provider = CaptureProvider(result=malformed)
                with mock.patch.object(
                    ModelResult,
                    "output_text",
                    property(fail_output),
                    create=True,
                ):
                    with self.assertRaises(type(source)):
                        self.runtime(provider).execute(self.request())
                self.assertEqual(len(provider.requests), 1)

    def test_event_sink_failure_is_isolated_and_prevents_provider_call(self) -> None:
        provider = CaptureProvider()

        def fail_sink(_event: RuntimeEvent) -> None:
            raise ValueError("secret=provider-token")

        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(self.request(on_event=fail_sink))
        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.EXECUTION_FAILED,
            message="Control-plane runtime event sink failed",
        )
        self.assertEqual(provider.requests, [])

    def test_event_sink_and_clock_base_exceptions_pass_through(self) -> None:
        for source in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(source=type(source).__name__):
                provider = CaptureProvider()

                def fail_sink(_event: RuntimeEvent, error: BaseException = source) -> None:
                    raise error

                with self.assertRaises(type(source)):
                    self.runtime(provider).execute(
                        self.request(on_event=fail_sink)
                    )
                self.assertEqual(provider.requests, [])

                clock_provider = CaptureProvider()

                def fail_clock(error: BaseException = source) -> datetime:
                    raise error

                with self.assertRaises(type(source)):
                    NativeRuntime(
                        clock_provider,
                        "fixed-model",
                        event_clock=fail_clock,
                    ).execute(self.request())
                self.assertEqual(clock_provider.requests, [])

    def test_invalid_event_clock_is_normalized_before_provider(self) -> None:
        provider = CaptureProvider()
        runtime = NativeRuntime(
            provider,
            "fixed-model",
            event_clock=lambda: datetime(2026, 1, 1),
        )
        with self.assertRaises(RuntimeError) as caught:
            runtime.execute(self.request())
        self.assert_safe_error(
            caught.exception,
            kind=RuntimeErrorKind.EXECUTION_FAILED,
            message="Native runtime event construction failed",
        )
        self.assertEqual(provider.requests, [])

    def test_callable_falsy_event_clock_is_preserved_and_used(self) -> None:
        class FalsyClock:
            def __bool__(self) -> bool:
                return False

            def __call__(self) -> datetime:
                return EVENT_TIME

        events: list[RuntimeEvent] = []
        provider = CaptureProvider()
        NativeRuntime(
            provider,
            "fixed-model",
            event_clock=FalsyClock(),
        ).execute(self.request(on_event=events.append))

        self.assertEqual(events[0].timestamp, EVENT_TIME)
        self.assertEqual(len(provider.requests), 1)

    def test_event_clock_truthiness_is_never_evaluated(self) -> None:
        class HostileTruthinessClock:
            def __bool__(self) -> bool:
                raise AssertionError("secret=clock-token")

            def __call__(self) -> datetime:
                return EVENT_TIME

        provider = CaptureProvider()
        result = NativeRuntime(
            provider,
            "fixed-model",
            event_clock=HostileTruthinessClock(),
        ).execute(self.request())

        self.assertEqual(result, RuntimeResult("native-ok"))
        self.assertEqual(len(provider.requests), 1)

    def test_event_clock_failures_are_isolated_before_provider(self) -> None:
        class HostileClockFailure(Exception):
            pass

        for source in (
            ValueError("secret=clock-token"),
            builtins.RuntimeError("secret=clock-token"),
            HostileClockFailure("secret=clock-token"),
        ):
            with self.subTest(source=type(source).__name__):
                provider = CaptureProvider()

                def fail_clock(error: Exception = source) -> datetime:
                    raise error

                runtime = NativeRuntime(
                    provider,
                    "fixed-model",
                    event_clock=fail_clock,
                )
                with self.assertRaises(RuntimeError) as caught:
                    runtime.execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=RuntimeErrorKind.EXECUTION_FAILED,
                    message="Native runtime event construction failed",
                    forbidden="clock-token",
                )
                self.assertEqual(provider.requests, [])

    def test_invalid_event_clock_values_are_safely_normalized(self) -> None:
        invalid_values = (None, "timestamp", True, object(), datetime(2026, 1, 1))
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                provider = CaptureProvider()
                runtime = NativeRuntime(
                    provider,
                    "fixed-model",
                    event_clock=lambda result=value: result,
                )
                with self.assertRaises(RuntimeError) as caught:
                    runtime.execute(self.request())
                self.assert_safe_error(
                    caught.exception,
                    kind=RuntimeErrorKind.EXECUTION_FAILED,
                    message="Native runtime event construction failed",
                )
                self.assertEqual(provider.requests, [])

    def test_constructor_rejects_every_non_callable_event_clock(self) -> None:
        for event_clock in ("clock", 1, True, object()):
            with self.subTest(value_type=type(event_clock).__name__):
                with self.assertRaises(TypeError) as caught:
                    NativeRuntime(
                        CaptureProvider(),
                        "fixed-model",
                        event_clock=event_clock,
                    )
                self.assertEqual(
                    str(caught.exception),
                    "Native runtime event clock must be callable",
                )

    def test_constructor_rejects_non_callable_provider_generate(self) -> None:
        for generate in (42, "not-callable", None):
            with self.subTest(generate=generate):
                provider = type("InvalidProvider", (), {"generate": generate})()
                with self.assertRaises(TypeError) as caught:
                    NativeRuntime(provider, "fixed-model")
                self.assert_safe_configuration_error(
                    caught.exception,
                    message="Native runtime provider configuration is invalid",
                    forbidden="provider-token",
                )

    def test_hostile_provider_lookup_is_safely_rejected(self) -> None:
        class HostileLookupProvider:
            @property
            def generate(self) -> object:
                raise ValueError("secret=provider-token")

        with self.assertRaises(TypeError) as caught:
            NativeRuntime(HostileLookupProvider(), "fixed-model")
        self.assert_safe_configuration_error(
            caught.exception,
            message="Native runtime provider configuration is invalid",
            forbidden="provider-token",
        )

    def test_provider_lookup_base_exceptions_pass_through(self) -> None:
        for source in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(source=type(source).__name__):
                class BaseExceptionLookupProvider:
                    @property
                    def generate(self, error: BaseException = source) -> object:
                        raise error

                with self.assertRaises(type(source)):
                    NativeRuntime(BaseExceptionLookupProvider(), "fixed-model")

    def test_provider_generate_is_resolved_only_once(self) -> None:
        class OneShotLookupProvider:
            def __init__(self) -> None:
                self.lookups = 0
                self.requests: list[object] = []

            @property
            def generate(self) -> object:
                self.lookups += 1
                if self.lookups > 1:
                    raise AssertionError("generate resolved more than once")
                return self.invoke

            def invoke(self, request: object) -> ModelResult:
                self.requests.append(request)
                return ModelResult("resolved-once")

        provider = OneShotLookupProvider()
        runtime = self.runtime(provider)
        result = runtime.execute(self.request())

        self.assertEqual(result, RuntimeResult("resolved-once"))
        self.assertEqual(provider.lookups, 1)
        self.assertEqual(len(provider.requests), 1)

    def test_constructor_is_strict_and_repr_never_touches_provider(self) -> None:
        with self.assertRaises(TypeError):
            NativeRuntime(object(), "fixed-model")
        for model in (None, "", "bad\nmodel"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                NativeRuntime(CaptureProvider(), model)
        with self.assertRaises(TypeError):
            NativeRuntime(CaptureProvider(), "fixed-model", event_clock="clock")

        class HostileReprProvider(CaptureProvider):
            def __repr__(self) -> str:
                raise AssertionError("secret=provider-token")

        rendered = repr(self.runtime(HostileReprProvider(), "secret-model"))
        self.assertEqual(rendered, "NativeRuntime()")
        self.assertNotIn("secret", rendered)

    def test_invalid_runtime_request_is_normalized_without_provider_call(self) -> None:
        provider = CaptureProvider()
        with self.assertRaises(RuntimeError) as caught:
            self.runtime(provider).execute(object())
        self.assertEqual(caught.exception.kind, RuntimeErrorKind.INVALID_RESULT)
        self.assertEqual(provider.requests, [])


class NativeRuntimeCompositionTest(unittest.TestCase):
    def request(self, events: list[RuntimeEvent]) -> RuntimeRequest:
        return RuntimeRequest(
            role=RuntimeRole.PLANNER,
            prompt="Loopback native prompt",
            runtime_config_id="native-loopback",
            request_id="native-loopback-request",
            timeout_seconds=10,
            completion_marker="IGNORED_BY_NATIVE_RUNTIME",
            on_event=events.append,
        )

    def test_real_openai_compatible_provider_composes_end_to_end(self) -> None:
        captured: list[dict[str, object]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                captured.append(json.loads(body.decode("utf-8")))
                response = b'{"choices":[{"message":{"content":"real-native"}}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                f"http://{host}:{port}/v1/chat/completions"
            )
        )
        events: list[RuntimeEvent] = []

        result = NativeRuntime(
            provider,
            "loopback-model",
            event_clock=lambda: EVENT_TIME,
        ).execute(self.request(events))

        self.assertEqual(result.output, "real-native")
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0],
            {
                "model": "loopback-model",
                "messages": [
                    {"role": "user", "content": "Loopback native prompt"}
                ],
            },
        )
        self.assertEqual(
            [event.kind for event in events],
            [RuntimeEventKind.STARTED],
        )

    def test_import_orders_and_installed_copy_are_cycle_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installed_scripts = Path(temporary) / "installed" / "scripts"
            installed_scripts.mkdir(parents=True)
            shutil.copytree(
                SCRIPTS / "agent_runtime",
                installed_scripts / "agent_runtime",
            )
            shutil.copytree(
                SCRIPTS / "model_provider",
                installed_scripts / "model_provider",
            )
            shutil.copy2(
                SCRIPTS / "oci_reference.py",
                installed_scripts / "oci_reference.py",
            )
            programs = (
                "import agent_runtime; import model_provider; "
                "assert agent_runtime.NativeRuntime is not None",
                "import model_provider; import agent_runtime; "
                "assert agent_runtime.NativeRuntime is not None",
            )
            for program in programs:
                with self.subTest(program=program):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]); "
                            + program,
                            str(installed_scripts),
                        ],
                        cwd=temporary,
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_source_has_no_policy_threads_discovery_or_logging(self) -> None:
        source = (SCRIPTS / "agent_runtime/native.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "ModelRouter",
            "fallback",
            "retry",
            "Thread",
            "asyncio",
            "subprocess",
            "OPENAI_API_KEY",
            "OpenAICompatibleProvider(",
            "print(",
            "logging",
            "logger",
            "str(error)",
            "repr(error)",
            ".__dict__.get(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

        model_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((SCRIPTS / "model_provider").glob("*.py"))
        )
        self.assertNotIn("agent_runtime", model_source)


if __name__ == "__main__":
    unittest.main()
