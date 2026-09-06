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
CANDIDATE_SHA = "a" * 40
CANDIDATE_REF = "refs/heads/release/0.2.0"
ACCEPTANCE_RUN = "123456789"
ARTIFACT_ID = "987654321"
APPLICATION_DIGEST = "sha256:" + "b" * 64
RUNTIME_DIGEST = "sha256:" + "c" * 64


def image(repository: str, digest: str) -> dict[str, str]:
    return {"digest": digest, "image_reference": repository + "@" + digest, "repository": repository}


def accepted_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "publication_state": "accepted",
        "version": "candidate-" + CANDIDATE_SHA,
        "candidate_ref": CANDIDATE_REF,
        "source_revision": CANDIDATE_SHA,
        "platform": PROMOTION.PLATFORM,
        "application": image(PROMOTION.APPLICATION_REPOSITORY, APPLICATION_DIGEST),
        "runtime": image(PROMOTION.RUNTIME_REPOSITORY, RUNTIME_DIGEST),
        "worker": image(PROMOTION.WORKER_REPOSITORY, PROMOTION.WORKER_DIGEST),
        "workflow_run": ACCEPTANCE_RUN,
        "anonymous_verification": {"digest_pull": "PASS", "fresh_daemon": "YES", "image_set_complete": "YES"},
    }


def workflow_run_metadata() -> dict[str, object]:
    return {
        "id": int(ACCEPTANCE_RUN),
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "path": PROMOTION.ACCEPTANCE_WORKFLOW_PATH,
        "name": PROMOTION.ACCEPTANCE_WORKFLOW_NAME,
        "repository": {"full_name": "Bebet0o/Orchestra"},
    }


class PromotionContractTest(unittest.TestCase):
    def bytes_for(self, manifest: dict[str, object] | None = None) -> bytes:
        return PROMOTION.canonical_json(manifest or accepted_manifest())

    def validate(self, manifest: dict[str, object]) -> dict[str, object]:
        return PROMOTION.validate_accepted_manifest(
            self.bytes_for(manifest),
            candidate_ref=CANDIDATE_REF,
            candidate_sha=CANDIDATE_SHA,
            acceptance_run_id=ACCEPTANCE_RUN,
        )

    def test_accepted_input_promotes_only_version(self) -> None:
        accepted = self.validate(accepted_manifest())
        promoted = PROMOTION.promote_manifest(accepted)
        self.assertEqual(PROMOTION.semantic_diff(accepted, promoted), ["$.version"])
        self.assertEqual(promoted["version"], "v0.2.0")
        self.assertEqual(promoted["application"]["digest"], APPLICATION_DIGEST)
        self.assertEqual(promoted["runtime"]["digest"], RUNTIME_DIGEST)

    def test_wrong_authorities_fail_closed(self) -> None:
        mutations = (
            ("version", "candidate-" + "0" * 40),
            ("source_revision", "0" * 40),
            ("publication_state", "provisional"),
            ("platform", "linux/arm64"),
            ("workflow_run", "1"),
        )
        for key, value in mutations:
            manifest = accepted_manifest(); manifest[key] = value
            with self.subTest(key=key), self.assertRaises(PROMOTION.PromotionError):
                self.validate(manifest)
        manifest = accepted_manifest()
        manifest["worker"] = image(PROMOTION.WORKER_REPOSITORY, "sha256:" + "d" * 64)
        with self.assertRaises(PROMOTION.PromotionError):
            self.validate(manifest)

    def test_promoted_manifest_satisfies_certified_installer_contract(self) -> None:
        PROMOTION.validate_installer_contract(ROOT / "install.sh")

    def test_artifact_and_workflow_provenance_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "artifact.zip"
            metadata = root / "artifact.json"
            run_metadata = root / "run.json"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(PROMOTION.MANIFEST_MEMBER, self.bytes_for())
            digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata.write_text(json.dumps({
                "id": int(ARTIFACT_ID),
                "name": "orchestra-official-publication-accepted-" + CANDIDATE_SHA,
                "digest": digest,
                "expired": False,
                "workflow_run": {"id": int(ACCEPTANCE_RUN)},
            }), encoding="utf-8")
            run_metadata.write_text(json.dumps(workflow_run_metadata()), encoding="utf-8")
            PROMOTION.validate_acceptance_workflow_run(run_metadata, ACCEPTANCE_RUN)
            member, returned_digest = PROMOTION.validate_artifact(
                metadata, archive,
                candidate_sha=CANDIDATE_SHA,
                acceptance_run_id=ACCEPTANCE_RUN,
                accepted_artifact_id=ARTIFACT_ID,
            )
            self.assertEqual(member, self.bytes_for())
            self.assertEqual(returned_digest, digest)

            bad_run = workflow_run_metadata(); bad_run["path"] = ".github/workflows/other.yml"
            run_metadata.write_text(json.dumps(bad_run), encoding="utf-8")
            with self.assertRaises(PROMOTION.PromotionError):
                PROMOTION.validate_acceptance_workflow_run(run_metadata, ACCEPTANCE_RUN)

    def test_end_to_end_promotion_emits_manifest_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "artifact.zip"
            metadata = root / "artifact.json"
            run_metadata = root / "run.json"
            output = root / "promotion"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(PROMOTION.MANIFEST_MEMBER, self.bytes_for())
            digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata.write_text(json.dumps({
                "id": int(ARTIFACT_ID),
                "name": "orchestra-official-publication-accepted-" + CANDIDATE_SHA,
                "digest": digest,
                "expired": False,
                "workflow_run": {"id": int(ACCEPTANCE_RUN)},
            }), encoding="utf-8")
            run_metadata.write_text(json.dumps(workflow_run_metadata()), encoding="utf-8")
            manifest_path, evidence_path = PROMOTION.promote(
                workflow_run_metadata=run_metadata,
                artifact_metadata=metadata,
                artifact_archive=archive,
                certified_installer=ROOT / "install.sh",
                output_directory=output,
                candidate_ref=CANDIDATE_REF,
                candidate_sha=CANDIDATE_SHA,
                acceptance_run_id=ACCEPTANCE_RUN,
                accepted_artifact_id=ARTIFACT_ID,
            )
            promoted = json.loads(manifest_path.read_text(encoding="utf-8"))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(promoted["version"], "v0.2.0")
            self.assertTrue(evidence["acceptance_workflow_verified"])
            self.assertEqual(evidence["semantic_diff"], ["version"])


class WorkflowContractTest(unittest.TestCase):
    def test_workflow_is_manual_main_only_read_only_and_pinned(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"actions": "read", "contents": "read"})
        self.assertIn("github.ref == 'refs/heads/main'", workflow["jobs"]["promote"]["if"])
        self.assertIn("test \"$RELEASE_VERSION\" = 'v0.2.0'", source)
        self.assertIn("actions/runs/${ACCEPTANCE_RUN_ID}", source)
        self.assertIn("official_image_publication.py validate-request", source)
        self.assertNotIn("172071c71cf58076e85524b57cfa19ec8e9f5cb8", source)
        self.assertNotIn("packages: write", source)
        self.assertNotRegex(source, r"docker\s+(?:build|push)")
        for step in workflow["jobs"]["promote"]["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
