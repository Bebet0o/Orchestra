from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/release_manifest_promotion.py"
WORKFLOW = ROOT / ".github/workflows/promote-release-manifest.yml"


def load() -> object:
    spec = importlib.util.spec_from_file_location("release_manifest_promotion_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROMOTION = load()


def accepted_manifest() -> dict[str, object]:
    def image(repository: str, digest: str) -> dict[str, str]:
        return {"digest": digest, "image_reference": repository + "@" + digest, "repository": repository}
    return {
        "schema_version": 1,
        "publication_state": "accepted",
        "version": PROMOTION.CANDIDATE_VERSION,
        "candidate_ref": PROMOTION.CANDIDATE_REF,
        "source_revision": PROMOTION.CERTIFIED_SOURCE,
        "platform": PROMOTION.PLATFORM,
        "application": image("ghcr.io/bebet0o/orchestra", PROMOTION.APPLICATION_DIGEST),
        "runtime": image("ghcr.io/bebet0o/orchestra-runtime", PROMOTION.RUNTIME_DIGEST),
        "worker": image("ghcr.io/bebet0o/orchestra-worker", PROMOTION.WORKER_DIGEST),
        "workflow_run": PROMOTION.ACCEPTANCE_RUN,
        "anonymous_verification": {"digest_pull": "PASS", "fresh_daemon": "YES", "image_set_complete": "YES"},
    }


class PromotionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_hash = PROMOTION.ACCEPTED_MANIFEST_SHA256
        self.original_s3_hash = PROMOTION.S3_MANIFEST_SHA256

    def tearDown(self) -> None:
        PROMOTION.ACCEPTED_MANIFEST_SHA256 = self.original_hash
        PROMOTION.S3_MANIFEST_SHA256 = self.original_s3_hash

    def bytes_for(self, manifest: dict[str, object] | None = None) -> bytes:
        return PROMOTION.canonical_json(manifest or accepted_manifest())

    def validate(self, manifest: dict[str, object]) -> dict[str, object]:
        value = self.bytes_for(manifest)
        PROMOTION.ACCEPTED_MANIFEST_SHA256 = hashlib.sha256(value).hexdigest()
        return PROMOTION.validate_accepted_manifest(value)

    def test_exact_input_promotes_only_version_and_matches_s3_bytes(self) -> None:
        accepted = self.validate(accepted_manifest())
        promoted = PROMOTION.promote_manifest(accepted)
        self.assertEqual(PROMOTION.semantic_diff(accepted, promoted), ["$.version"])
        self.assertEqual(promoted["version"], "v0.1.0")
        PROMOTION.S3_MANIFEST_SHA256 = hashlib.sha256(PROMOTION.canonical_json(promoted)).hexdigest()
        self.assertEqual(PROMOTION.sha256_bytes(PROMOTION.canonical_json(promoted)), PROMOTION.S3_MANIFEST_SHA256)

    def test_wrong_accepted_manifest_hash_is_rejected(self) -> None:
        PROMOTION.ACCEPTED_MANIFEST_SHA256 = "0" * 64
        with self.assertRaises(PROMOTION.PromotionError):
            PROMOTION.validate_accepted_manifest(self.bytes_for())

    def test_wrong_authorities_fail_closed(self) -> None:
        mutations = (
            ("version", "candidate-" + "0" * 40),
            ("source_revision", "0" * 40),
            ("publication_state", "provisional"),
            ("platform", "linux/arm64"),
        )
        for key, value in mutations:
            manifest = accepted_manifest(); manifest[key] = value
            with self.subTest(key=key), self.assertRaises(PROMOTION.PromotionError):
                self.validate(manifest)
        for image, digest in (("application", "a"), ("runtime", "b"), ("worker", "c")):
            manifest = accepted_manifest()
            manifest[image]["digest"] = "sha256:" + digest * 64
            manifest[image]["image_reference"] = manifest[image]["repository"] + "@" + manifest[image]["digest"]
            with self.subTest(image=image), self.assertRaises(PROMOTION.PromotionError):
                self.validate(manifest)

    def test_promoted_manifest_satisfies_certified_installer_contract(self) -> None:
        accepted = self.validate(accepted_manifest())
        promoted = PROMOTION.promote_manifest(accepted)
        PROMOTION.validate_installer_contract(ROOT / "install.sh", promoted)

    def test_artifact_identity_archive_digest_and_file_set_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root / "artifact.zip"; metadata = root / "metadata.json"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(PROMOTION.MANIFEST_MEMBER, self.bytes_for())
            digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            old_digest = PROMOTION.ACCEPTED_ARTIFACT_DIGEST
            PROMOTION.ACCEPTED_ARTIFACT_DIGEST = digest
            try:
                metadata.write_text(json.dumps({
                    "id": PROMOTION.ACCEPTED_ARTIFACT_ID,
                    "name": PROMOTION.ACCEPTED_ARTIFACT_NAME,
                    "digest": digest,
                    "workflow_run": {"id": int(PROMOTION.ACCEPTANCE_RUN)},
                }))
                self.assertEqual(PROMOTION.validate_artifact(metadata, archive), self.bytes_for())
                metadata.write_text(json.dumps({"id": 0, "name": PROMOTION.ACCEPTED_ARTIFACT_NAME, "digest": digest, "workflow_run": {"id": int(PROMOTION.ACCEPTANCE_RUN)}}))
                with self.assertRaises(PROMOTION.PromotionError):
                    PROMOTION.validate_artifact(metadata, archive)
            finally:
                PROMOTION.ACCEPTED_ARTIFACT_DIGEST = old_digest


class WorkflowContractTest(unittest.TestCase):
    def test_workflow_is_manual_main_only_read_only_and_pinned(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"actions": "read", "contents": "read"})
        self.assertIn("github.ref == 'refs/heads/main'", workflow["jobs"]["promote"]["if"])
        self.assertNotIn("packages: write", source)
        self.assertNotRegex(source, r"docker\s+(?:build|push)")
        for step in workflow["jobs"]["promote"]["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
