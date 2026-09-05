from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_runtime import NativeRuntime, RuntimeRole
from model_provider import FakeModelProvider, FakeModelProviderOutcome
from model_router import ModelRouteRule, ModelRoutingPolicy
from runtime_control import (
    prepare_model_route,
    runtime_from_prepared_route,
)


class ModelRouterDispatchTest(unittest.TestCase):
    def native_role(self, role_id: str, model: str = "base-model") -> dict[str, object]:
        return {
            "role_id": role_id,
            "runtime_kind": "native",
            "model_id": model,
        }

    def test_worker_task_kind_rule_selects_native_model(self) -> None:
        policy = ModelRoutingPolicy(
            version=4,
            rules=(
                ModelRouteRule(
                    "code-worker",
                    "local/qwen-code",
                    runtime_role="worker",
                    runtime_kind="native",
                    task_kind="CODE",
                ),
            ),
        )
        role = self.native_role("worker_code")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo/config").mkdir(parents=True)
            # prepare_model_route loads the canonical production file; patching
            # belongs at the loader boundary, not at router semantics.
            import runtime_control
            original = runtime_control.load_model_routing_policy
            runtime_control.load_model_routing_policy = lambda _path: policy
            try:
                prepared = prepare_model_route(
                    root,
                    role,
                    required_role=RuntimeRole.WORKER,
                    runtime_request_id="worker-route-1",
                    task_kind="CODE",
                )
            finally:
                runtime_control.load_model_routing_policy = original

        self.assertEqual(prepared.decision.selected_model_id, "local/qwen-code")
        self.assertEqual(prepared.decision.rule_id, "code-worker")
        self.assertEqual(prepared.request.task_kind, "CODE")

    def test_reviewer_role_rule_selects_native_model_and_reaches_provider(self) -> None:
        policy = ModelRoutingPolicy(
            version=5,
            rules=(
                ModelRouteRule(
                    "reviewer-strong",
                    "local/qwen-review",
                    runtime_role="reviewer",
                    runtime_kind="native",
                ),
            ),
        )
        role = self.native_role("reviewer")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo/config").mkdir(parents=True)
            import runtime_control
            original = runtime_control.load_model_routing_policy
            runtime_control.load_model_routing_policy = lambda _path: policy
            try:
                prepared = prepare_model_route(
                    root,
                    role,
                    required_role=RuntimeRole.REVIEWER,
                    runtime_request_id="review-route-1",
                    task_kind="CODE",
                )
            finally:
                runtime_control.load_model_routing_policy = original

            provider = FakeModelProvider([FakeModelProviderOutcome.success("PASS")])
            kind, runtime = runtime_from_prepared_route(
                root, role, prepared, required_role=RuntimeRole.REVIEWER, provider=provider
            )

        self.assertEqual(kind.value, "native")
        self.assertIsInstance(runtime, NativeRuntime)
        self.assertEqual(runtime._model, "local/qwen-review")

    def test_hermes_remains_runtime_managed_even_when_rule_matches(self) -> None:
        policy = ModelRoutingPolicy(
            version=6,
            rules=(
                ModelRouteRule(
                    "must-not-override-hermes",
                    "other-model",
                    runtime_role="worker",
                    runtime_kind="hermes",
                ),
            ),
        )
        role = {
            "role_id": "worker_code",
            "runtime_kind": "hermes",
            "model_id": "profile-model",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo/config").mkdir(parents=True)
            import runtime_control
            original = runtime_control.load_model_routing_policy
            runtime_control.load_model_routing_policy = lambda _path: policy
            try:
                prepared = prepare_model_route(
                    root,
                    role,
                    required_role=RuntimeRole.WORKER,
                    runtime_request_id="hermes-route-1",
                    task_kind="CODE",
                )
            finally:
                runtime_control.load_model_routing_policy = original

        self.assertEqual(prepared.decision.reason.value, "runtime_managed")
        self.assertEqual(prepared.decision.selected_model_id, "profile-model")
        self.assertEqual(prepared.decision.rule_id, "runtime-managed-model")


if __name__ == "__main__":
    unittest.main()
