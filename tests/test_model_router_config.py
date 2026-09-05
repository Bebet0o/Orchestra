from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from model_router import ModelRouterError, load_model_routing_policy


class ModelRouterConfigTest(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.toml"
            path.write_text(text, encoding="utf-8")
            return load_model_routing_policy(path)

    def test_repository_default_policy_is_explicit_and_backward_compatible(self) -> None:
        policy = load_model_routing_policy(ROOT / "config/orchestrator.toml")
        self.assertEqual(policy.version, 1)
        self.assertEqual(policy.rules, ())

    def test_ordered_local_native_rules_are_loaded_without_model_family_assumptions(self) -> None:
        policy = self.load('''
[model_router]
version = 3
[[model_router.rules]]
id = "local-code"
model = "Qwen/Qwen3.8-27B"
role_id = "worker_code"
runtime_kind = "native"
[[model_router.rules]]
id = "review"
model = "local/reviewer:model"
runtime_role = "reviewer"
''')
        self.assertEqual(policy.version, 3)
        self.assertEqual([rule.rule_id for rule in policy.rules], ["local-code", "review"])
        self.assertEqual(policy.rules[0].model_id, "Qwen/Qwen3.8-27B")

    def test_unknown_fields_and_malformed_rules_fail_closed(self) -> None:
        cases = [
            "[model_router]\nversion=1\nunknown=true\n",
            "[model_router]\nversion=1\nrules={}\n",
            "[model_router]\nversion=true\nrules=[]\n",
            "[model_router]\nversion=1\n[[model_router.rules]]\nid='x'\nmodel='m'\nextra='y'\n",
            "[model_router]\nversion=1\n[[model_router.rules]]\nid='x'\nmodel='m'\n",
        ]
        # Last case is intentionally invalid because a rule must constrain at
        # least one selector; ModelRouteRule remains the authority for that rule.
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ModelRouterError):
                    self.load(text)

    def test_absent_file_fails_closed(self) -> None:
        with self.assertRaises(ModelRouterError):
            load_model_routing_policy(Path('/definitely/missing/orchestrator.toml'))


if __name__ == "__main__":
    unittest.main()
