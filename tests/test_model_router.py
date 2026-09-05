from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from model_router import (  # noqa: E402
    ModelRouteReason,
    ModelRouteRequest,
    ModelRouteRule,
    ModelRouter,
    ModelRoutingPolicy,
)


class ModelRouterTest(unittest.TestCase):
    def request(self, **overrides: object) -> ModelRouteRequest:
        values: dict[str, object] = {
            "runtime_request_id": "runtime-request-1",
            "role_id": "worker-code",
            "runtime_role": "worker",
            "runtime_kind": "native",
            "configured_model_id": "configured-model",
            "task_kind": "PIPELINE",
        }
        values.update(overrides)
        return ModelRouteRequest(**values)  # type: ignore[arg-type]

    def test_first_matching_rule_is_deterministic_and_explainable(self) -> None:
        policy = ModelRoutingPolicy(
            version=1,
            rules=(
                ModelRouteRule(
                    rule_id="native-code",
                    model_id="coding-model",
                    runtime_kind="native",
                    task_kind="PIPELINE",
                ),
                ModelRouteRule(
                    rule_id="all-workers",
                    model_id="worker-model",
                    runtime_role="worker",
                ),
            ),
        )
        router = ModelRouter(policy)
        first = router.route(self.request())
        second = router.route(self.request(runtime_request_id="runtime-request-2"))
        self.assertEqual(first.selected_model_id, "coding-model")
        self.assertEqual(first.rule_id, "native-code")
        self.assertEqual(first.reason, ModelRouteReason.RULE_MATCH)
        self.assertEqual(first.policy_sha256, policy.sha256)
        self.assertEqual(first.policy_sha256, second.policy_sha256)
        self.assertEqual(first.selected_model_id, second.selected_model_id)

    def test_no_match_falls_back_only_to_explicit_configured_model(self) -> None:
        policy = ModelRoutingPolicy(
            version=3,
            rules=(
                ModelRouteRule(
                    rule_id="reviewers",
                    model_id="review-model",
                    runtime_role="reviewer",
                ),
            ),
        )
        decision = ModelRouter(policy).route(self.request())
        self.assertEqual(decision.selected_model_id, "configured-model")
        self.assertEqual(decision.rule_id, "configured-role-model")
        self.assertEqual(decision.reason, ModelRouteReason.CONFIGURED_DEFAULT)
        self.assertEqual(decision.policy_version, 3)

    def test_hermes_does_not_claim_an_unapplied_per_execution_override(self) -> None:
        policy = ModelRoutingPolicy(
            version=1,
            rules=(
                ModelRouteRule("worker", "override-model", runtime_role="worker"),
            ),
        )
        decision = ModelRouter(policy).route(self.request(runtime_kind="hermes"))
        self.assertEqual(decision.selected_model_id, "configured-model")
        self.assertEqual(decision.rule_id, "runtime-managed-model")
        self.assertEqual(decision.reason, ModelRouteReason.RUNTIME_MANAGED)

    def test_policy_hash_covers_order_and_all_selectors(self) -> None:
        left = ModelRoutingPolicy(
            version=1,
            rules=(
                ModelRouteRule("one", "model-a", role_id="worker-code"),
                ModelRouteRule("two", "model-b", runtime_role="worker"),
            ),
        )
        same = ModelRoutingPolicy(
            version=1,
            rules=(
                ModelRouteRule("one", "model-a", role_id="worker-code"),
                ModelRouteRule("two", "model-b", runtime_role="worker"),
            ),
        )
        reversed_policy = ModelRoutingPolicy(version=1, rules=tuple(reversed(left.rules)))
        changed_selector = ModelRoutingPolicy(
            version=1,
            rules=(
                ModelRouteRule("one", "model-a", role_id="worker-tests"),
                ModelRouteRule("two", "model-b", runtime_role="worker"),
            ),
        )
        self.assertEqual(left.sha256, same.sha256)
        self.assertNotEqual(left.sha256, reversed_policy.sha256)
        self.assertNotEqual(left.sha256, changed_selector.sha256)
        self.assertEqual(len(left.sha256), 64)

    def test_rules_can_match_role_runtime_and_task_together(self) -> None:
        rule = ModelRouteRule(
            "specific",
            "model-x",
            role_id="worker-code",
            runtime_role="worker",
            runtime_kind="native",
            task_kind="PIPELINE",
        )
        self.assertTrue(rule.matches(self.request()))
        self.assertFalse(rule.matches(self.request(task_kind="NOOP")))
        self.assertFalse(rule.matches(self.request(runtime_kind="hermes")))

    def test_opaque_model_ids_remain_provider_compatible(self) -> None:
        request = self.request(configured_model_id="Qwen/Qwen3.8-27B")
        decision = ModelRouter(ModelRoutingPolicy(version=1)).route(request)
        self.assertEqual(decision.selected_model_id, "Qwen/Qwen3.8-27B")
        routed = ModelRouter(
            ModelRoutingPolicy(
                version=1,
                rules=(
                    ModelRouteRule(
                        "native",
                        "local/Qwen3.8:Q4_K_M",
                        runtime_kind="native",
                    ),
                ),
            )
        ).route(request)
        self.assertEqual(routed.selected_model_id, "local/Qwen3.8:Q4_K_M")

    def test_contract_rejects_ambiguous_or_unbounded_inputs(self) -> None:
        with self.assertRaises(ValueError):
            ModelRouteRule("catch-all", "model")
        with self.assertRaises(ValueError):
            ModelRouteRule("bad id", "model", runtime_role="worker")
        with self.assertRaises(ValueError):
            self.request(runtime_role="recovery")
        with self.assertRaises(ValueError):
            self.request(runtime_kind="other")
        with self.assertRaises(ValueError):
            self.request(configured_model_id="x" * 257)
        with self.assertRaises(ValueError):
            ModelRoutingPolicy(
                version=1,
                rules=(
                    ModelRouteRule("same", "a", runtime_role="worker"),
                    ModelRouteRule("same", "b", runtime_role="reviewer"),
                ),
            )
        with self.assertRaises(ValueError):
            ModelRoutingPolicy(
                version=1,
                rules=tuple(
                    ModelRouteRule(f"rule-{index}", "model", runtime_role="worker")
                    for index in range(65)
                ),
            )

    def test_contract_values_are_immutable(self) -> None:
        request = self.request()
        rule = ModelRouteRule("worker", "model", runtime_role="worker")
        policy = ModelRoutingPolicy(1, (rule,))
        decision = ModelRouter(policy).route(request)
        for value in (request, rule, policy, decision):
            field_name = dataclasses.fields(value)[0].name
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, field_name, "changed")


if __name__ == "__main__":
    unittest.main()
