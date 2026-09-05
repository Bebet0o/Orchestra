from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    __import__("sys").modules[name] = module
    spec.loader.exec_module(module)
    return module


service = load_module("orchestra_console_review_service", REPO / "scripts/orchestra-console.py")
REVIEW = "review-" + "b" * 32


class ReviewConsoleSourceTest(unittest.TestCase):
    def test_proxy_exposes_only_bounded_review_detail_reads(self) -> None:
        for path in (f"/api/v1/reviews/{REVIEW}", f"/api/v1/reviews/{REVIEW}/evidence"):
            with self.subTest(path=path):
                self.assertTrue(service._controller_route_exposed("GET", path))
        for method, path in (
            ("POST", f"/api/v1/reviews/{REVIEW}"),
            ("GET", f"/api/v1/reviews/{REVIEW}/unknown"),
            ("GET", f"/api/v1/reviews/{REVIEW}/evidence?limit=200"),
            ("GET", "/api/v1/reviews/not-a-review"),
            ("GET", f"/api/v1/reviews/{REVIEW}/evidence/extra"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(service._controller_route_exposed(method, path))

    def test_reviews_route_is_controller_backed(self) -> None:
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in (
            'id="review-panel"',
            'id="review-list"',
            'id="review-detail-card"',
            'id="review-evidence-list"',
            'id="review-assignment-list"',
            'id="review-recovery-list"',
        ):
            self.assertIn(marker, html)
        for marker in (
            "refreshReviews",
            "selectReview",
            "client.reviewEvidence",
            "client.reviewerAssignments",
            "client.recoveries",
            "reviewPanel.hidden",
            "textContent",
            "replaceChildren",
        ):
            self.assertIn(marker, app)
        for marker in (
            "async review(identifier)",
            "async reviewEvidence(identifier)",
            "REVIEW_ID_PATTERN",
        ):
            self.assertIn(marker, client)

    def test_browser_keeps_sensitive_review_payloads_out(self) -> None:
        source = "\n".join(
            (REPO / "console/src" / name).read_text(encoding="utf-8")
            for name in ("app.js", "controller-client.js", "index.html")
        )
        for forbidden in (
            "orchestra.db",
            "/run/orchestra-docker",
            "/var/run/docker.sock",
            "prompt_path",
            "output_path",
            "result_json",
            "failure_reason",
            "innerHTML",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "eval(",
            "new Function",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
