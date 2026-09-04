"""Synchronous AgentRuntime backed by one explicitly injected model provider."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from model_provider import (
    ModelMessage,
    ModelMessageRole,
    ModelProvider,
    ModelProviderError,
    ModelProviderErrorKind,
    ModelRequest,
    ModelResult,
)

from .contract import (
    AgentRuntime,
    RuntimeEvent,
    RuntimeEventDispatcher,
    RuntimeEventKind,
    RuntimeError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimeResult,
)


_PROVIDER_ERROR_MAPPING = {
    ModelProviderErrorKind.UNAVAILABLE: (
        RuntimeErrorKind.RUNTIME_UNAVAILABLE,
        "Native model provider is unavailable",
    ),
    ModelProviderErrorKind.TIMEOUT: (
        RuntimeErrorKind.TIMEOUT,
        "Native model generation timed out",
    ),
    ModelProviderErrorKind.REQUEST_REJECTED: (
        RuntimeErrorKind.EXECUTION_FAILED,
        "Native model provider rejected the request",
    ),
    ModelProviderErrorKind.INVALID_RESPONSE: (
        RuntimeErrorKind.INVALID_RESULT,
        "Native model provider returned an invalid response",
    ),
    ModelProviderErrorKind.PROVIDER_FAILED: (
        RuntimeErrorKind.EXECUTION_FAILED,
        "Native model provider failed",
    ),
}


class NativeRuntime(AgentRuntime):
    """Translate one runtime request into one synchronous model generation."""

    runtime_kind = "native"

    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        *,
        event_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if event_clock is not None and not callable(event_clock):
            raise TypeError("Native runtime event clock must be callable")

        provider_generate: object = None
        provider_lookup_failed = False
        try:
            provider_generate = provider.generate
        except Exception:
            provider_lookup_failed = True
        if provider_lookup_failed or not callable(provider_generate):
            raise TypeError("Native runtime provider configuration is invalid")

        configuration_invalid = False
        try:
            ModelRequest(
                model=model,
                messages=(ModelMessage(ModelMessageRole.USER, ""),),
                timeout_seconds=1,
            )
        except (TypeError, ValueError):
            configuration_invalid = True
        if configuration_invalid:
            raise ValueError("Native runtime model configuration is invalid")

        self._generate = provider_generate
        self._model = model
        self._event_clock = (
            (lambda: datetime.now(timezone.utc))
            if event_clock is None
            else event_clock
        )

    def __repr__(self) -> str:
        return "NativeRuntime()"

    def _model_request(self, request: RuntimeRequest) -> ModelRequest:
        model_request: ModelRequest | None = None
        try:
            model_request = ModelRequest(
                model=self._model,
                messages=(
                    ModelMessage(
                        role=ModelMessageRole.USER,
                        content=request.prompt,
                    ),
                ),
                timeout_seconds=request.timeout_seconds,
            )
        except (TypeError, ValueError):
            pass
        if model_request is None:
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Native runtime request cannot be represented by the model provider",
            )
        return model_request

    def _started_event(self, request: RuntimeRequest) -> RuntimeEvent:
        event: RuntimeEvent | None = None
        try:
            event = RuntimeEvent(
                kind=RuntimeEventKind.STARTED,
                request_id=request.request_id,
                role=request.role,
                timestamp=self._event_clock(),
            )
        except Exception:
            pass
        if event is None:
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Native runtime event construction failed",
            )
        return event

    @staticmethod
    def _provider_error(error: ModelProviderError) -> RuntimeError:
        provider_kind: object = None
        if type(error) is ModelProviderError:
            try:
                provider_kind = error.kind
            except Exception:
                pass
        if type(provider_kind) is not ModelProviderErrorKind:
            provider_kind = None
        runtime_kind, message = _PROVIDER_ERROR_MAPPING.get(
            provider_kind,
            (
                RuntimeErrorKind.EXECUTION_FAILED,
                "Native model provider invocation failed",
            ),
        )
        return RuntimeError(runtime_kind, message)

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        if not isinstance(request, RuntimeRequest):
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Native runtime request does not satisfy the runtime contract",
            )

        model_request = self._model_request(request)
        dispatcher = RuntimeEventDispatcher(request)
        started_event = self._started_event(request)

        dispatch_error: RuntimeError | None = None
        try:
            dispatcher.emit(started_event)
        except RuntimeError:
            dispatch_error = RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Control-plane runtime event sink failed",
            )
        if dispatch_error is not None:
            raise dispatch_error

        provider_result: object = None
        invocation_error: RuntimeError | None = None
        try:
            provider_result = self._generate(model_request)
        except ModelProviderError as error:
            invocation_error = self._provider_error(error)
        except Exception:
            invocation_error = RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Native model provider invocation failed",
            )
        if invocation_error is not None:
            raise invocation_error

        output_text: object = None
        result_invalid = type(provider_result) is not ModelResult
        if not result_invalid:
            try:
                output_text = provider_result.output_text
            except Exception:
                result_invalid = True
        if result_invalid or type(output_text) is not str:
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Native model provider returned an invalid result",
            )
        return RuntimeResult(output=output_text)
