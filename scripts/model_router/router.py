"""Bounded deterministic static model router."""

from __future__ import annotations

from .contract import (
    ModelRouteDecision,
    ModelRouteReason,
    ModelRouteRequest,
    ModelRoutingPolicy,
)


class ModelRouter:
    """Resolve one request through one immutable ordered routing policy."""

    def __init__(self, policy: ModelRoutingPolicy) -> None:
        if type(policy) is not ModelRoutingPolicy:
            raise TypeError("Model router policy is invalid")
        self._policy = policy

    @property
    def policy(self) -> ModelRoutingPolicy:
        return self._policy

    def route(self, request: ModelRouteRequest) -> ModelRouteDecision:
        if type(request) is not ModelRouteRequest:
            raise TypeError("Model route request is invalid")
        # Hermes Agent currently owns its model through the synchronized
        # profile configuration. Do not pretend a per-execution override was
        # applied when the adapter has no such control surface.
        if request.runtime_kind == "hermes":
            return ModelRouteDecision(
                selected_model_id=request.configured_model_id,
                policy_version=self._policy.version,
                policy_sha256=self._policy.sha256,
                rule_id="runtime-managed-model",
                reason=ModelRouteReason.RUNTIME_MANAGED,
            )
        for rule in self._policy.rules:
            if rule.matches(request):
                return ModelRouteDecision(
                    selected_model_id=rule.model_id,
                    policy_version=self._policy.version,
                    policy_sha256=self._policy.sha256,
                    rule_id=rule.rule_id,
                    reason=ModelRouteReason.RULE_MATCH,
                )
        return ModelRouteDecision(
            selected_model_id=request.configured_model_id,
            policy_version=self._policy.version,
            policy_sha256=self._policy.sha256,
            rule_id="configured-role-model",
            reason=ModelRouteReason.CONFIGURED_DEFAULT,
        )
