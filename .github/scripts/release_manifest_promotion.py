#!/usr/bin/env python3
"""Promote one trusted accepted image-set manifest to the v0.2.0 release contract."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, SCRIPT_DIRECTORY)

from worker_publication import (  # noqa: E402
    PublicationContractError,
    validate_candidate_ref,
    validate_candidate_sha,
    validate_canonical_digest,
)

RELEASE_VERSION = "v0.2.0"
PLATFORM = "linux/amd64"
APPLICATION_REPOSITORY = "ghcr.io/bebet0o/orchestra"
RUNTIME_REPOSITORY = "ghcr.io/bebet0o/orchestra-runtime"
WORKER_REPOSITORY = "ghcr.io/bebet0o/orchestra-worker"
WORKER_DIGEST = "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
ACCEPTANCE_WORKFLOW_PATH = ".github/workflows/accept-official-images.yml"
ACCEPTANCE_WORKFLOW_NAME = "Accept Orchestra application/runtime publication"
MANIFEST_MEMBER = "orchestra-release-manifest-accepted.json"
MAX_INPUT_BYTES = 1_048_576


class PromotionError(ValueError):
    """The accepted input cannot be promoted under the trusted release contract."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bounded(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise PromotionError(f"unsafe promotion input: {path}")
    return path.read_bytes()


def _load_json_bytes(value: bytes, *, description: str) -> dict[str, object]:
    try:
        document = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError(f"{description} is not valid JSON") from error
    if not isinstance(document, dict):
        raise PromotionError(f"{description} is not a JSON object")
    return document


def _decimal_identity(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise PromotionError(f"{field} is invalid")
    return value


def validate_acceptance_workflow_run(metadata_path: Path, acceptance_run_id: str) -> None:
    run_id = _decimal_identity(acceptance_run_id, field="acceptance workflow run")
    metadata = _load_json_bytes(_read_bounded(metadata_path), description="workflow run metadata")
    repository = metadata.get("repository")
    if not isinstance(repository, dict) or str(repository.get("full_name", "")).lower() != "bebet0o/orchestra":
        raise PromotionError("acceptance workflow repository is not exact")
    if metadata.get("id") != int(run_id):
        raise PromotionError("acceptance workflow run ID is not exact")
    if metadata.get("event") != "workflow_dispatch":
        raise PromotionError("acceptance workflow event is not trusted")
    if metadata.get("status") != "completed" or metadata.get("conclusion") != "success":
        raise PromotionError("acceptance workflow did not complete successfully")
    if metadata.get("head_branch") != "main":
        raise PromotionError("acceptance workflow did not run from main")
    if metadata.get("path") != ACCEPTANCE_WORKFLOW_PATH:
        raise PromotionError("acceptance workflow path is not exact")
    if metadata.get("name") != ACCEPTANCE_WORKFLOW_NAME:
        raise PromotionError("acceptance workflow name is not exact")


def validate_artifact(
    metadata_path: Path,
    archive_path: Path,
    *,
    candidate_sha: str,
    acceptance_run_id: str,
    accepted_artifact_id: str,
) -> tuple[bytes, str]:
    candidate = validate_candidate_sha(candidate_sha)
    run_id = _decimal_identity(acceptance_run_id, field="acceptance workflow run")
    artifact_id = _decimal_identity(accepted_artifact_id, field="accepted artifact ID")
    metadata = _load_json_bytes(_read_bounded(metadata_path), description="artifact metadata")
    expected_name = "orchestra-official-publication-accepted-" + candidate
    if metadata.get("id") != int(artifact_id):
        raise PromotionError("accepted artifact ID is not exact")
    if metadata.get("name") != expected_name:
        raise PromotionError("accepted artifact name is not exact")
    workflow_run = metadata.get("workflow_run")
    if not isinstance(workflow_run, dict) or workflow_run.get("id") != int(run_id):
        raise PromotionError("accepted artifact workflow run is not exact")
    if metadata.get("expired") is True:
        raise PromotionError("accepted artifact is expired")
    digest = metadata.get("digest")
    try:
        digest = validate_canonical_digest(digest, field="accepted artifact digest")
    except PublicationContractError as error:
        raise PromotionError(str(error)) from error
    archive = _read_bounded(archive_path)
    if "sha256:" + sha256_bytes(archive) != digest:
        raise PromotionError("accepted artifact archive digest is not exact")
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            members = bundle.infolist()
            if len(members) != 1 or members[0].filename != MANIFEST_MEMBER:
                raise PromotionError("accepted artifact file set is not exact")
            member = members[0]
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                raise PromotionError("accepted manifest member is unsafe")
            return bundle.read(member), digest
    except zipfile.BadZipFile as error:
        raise PromotionError("accepted artifact archive is invalid") from error


def _validate_image(value: object, *, repository: str, exact_digest: str | None = None) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"repository", "digest", "image_reference"}:
        raise PromotionError(f"{repository} image authority is malformed")
    if value.get("repository") != repository:
        raise PromotionError(f"{repository} repository authority mismatched")
    try:
        digest = validate_canonical_digest(value.get("digest"), field=f"{repository} digest")
    except PublicationContractError as error:
        raise PromotionError(str(error)) from error
    if exact_digest is not None and digest != exact_digest:
        raise PromotionError(f"{repository} digest authority mismatched")
    reference = repository + "@" + digest
    if value.get("image_reference") != reference:
        raise PromotionError(f"{repository} image reference mismatched")
    return {"repository": repository, "digest": digest, "image_reference": reference}


def validate_accepted_manifest(
    value: bytes,
    *,
    candidate_ref: str,
    candidate_sha: str,
    acceptance_run_id: str,
) -> dict[str, object]:
    try:
        reference = validate_candidate_ref(candidate_ref)
        candidate = validate_candidate_sha(candidate_sha)
    except PublicationContractError as error:
        raise PromotionError(str(error)) from error
    run_id = _decimal_identity(acceptance_run_id, field="acceptance workflow run")
    manifest = _load_json_bytes(value, description="accepted manifest")
    expected_keys = {
        "schema_version", "publication_state", "version", "candidate_ref",
        "source_revision", "platform", "application", "runtime", "worker",
        "workflow_run", "anonymous_verification",
    }
    if set(manifest) != expected_keys:
        raise PromotionError("accepted manifest field set is not exact")
    if manifest.get("schema_version") != 1 or manifest.get("publication_state") != "accepted":
        raise PromotionError("accepted manifest state is invalid")
    if manifest.get("version") != "candidate-" + candidate:
        raise PromotionError("accepted manifest candidate version is invalid")
    if manifest.get("candidate_ref") != reference or manifest.get("source_revision") != candidate:
        raise PromotionError("accepted manifest source identity is invalid")
    if manifest.get("platform") != PLATFORM:
        raise PromotionError("accepted manifest platform is invalid")
    if str(manifest.get("workflow_run")) != run_id:
        raise PromotionError("accepted manifest workflow run is invalid")
    if manifest.get("anonymous_verification") != {
        "digest_pull": "PASS", "fresh_daemon": "YES", "image_set_complete": "YES"
    }:
        raise PromotionError("accepted manifest lacks exact anonymous verification")
    manifest["application"] = _validate_image(manifest.get("application"), repository=APPLICATION_REPOSITORY)
    manifest["runtime"] = _validate_image(manifest.get("runtime"), repository=RUNTIME_REPOSITORY)
    manifest["worker"] = _validate_image(
        manifest.get("worker"), repository=WORKER_REPOSITORY, exact_digest=WORKER_DIGEST
    )
    return manifest


def semantic_diff(left: object, right: object, path: str = "$") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(left.keys() | right.keys()):
            child = f"{path}.{key}"
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(semantic_diff(left[key], right[key], child))
        return paths
    return [] if left == right else [path]


def promote_manifest(accepted: dict[str, object]) -> dict[str, object]:
    promoted = copy.deepcopy(accepted)
    promoted["version"] = RELEASE_VERSION
    if semantic_diff(accepted, promoted) != ["$.version"]:
        raise PromotionError("promotion changed fields other than version")
    return promoted


def canonical_json(document: dict[str, object]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def validate_installer_contract(installer_path: Path) -> None:
    installer = _read_bounded(installer_path).decode("utf-8")
    required_predicates = (
        '.publication_state == "accepted"',
        '.version == "v0.2.0"',
        '.platform == "linux/amd64"',
        '.application.repository == "ghcr.io/bebet0o/orchestra"',
        '.runtime.repository == "ghcr.io/bebet0o/orchestra-runtime"',
        '.worker.repository == "ghcr.io/bebet0o/orchestra-worker"',
    )
    if any(predicate not in installer for predicate in required_predicates):
        raise PromotionError("certified installer predicate is not recognized")


def promote(
    *,
    workflow_run_metadata: Path,
    artifact_metadata: Path,
    artifact_archive: Path,
    certified_installer: Path,
    output_directory: Path,
    candidate_ref: str,
    candidate_sha: str,
    acceptance_run_id: str,
    accepted_artifact_id: str,
) -> tuple[Path, Path]:
    validate_acceptance_workflow_run(workflow_run_metadata, acceptance_run_id)
    accepted_bytes, artifact_digest = validate_artifact(
        artifact_metadata,
        artifact_archive,
        candidate_sha=candidate_sha,
        acceptance_run_id=acceptance_run_id,
        accepted_artifact_id=accepted_artifact_id,
    )
    accepted = validate_accepted_manifest(
        accepted_bytes,
        candidate_ref=candidate_ref,
        candidate_sha=candidate_sha,
        acceptance_run_id=acceptance_run_id,
    )
    promoted = promote_manifest(accepted)
    validate_installer_contract(certified_installer)
    promoted_bytes = canonical_json(promoted)
    output_directory.mkdir(parents=True, exist_ok=False)
    manifest_path = output_directory / "orchestra-release-manifest.json"
    evidence_path = output_directory / "orchestra-release-promotion-evidence.json"
    manifest_path.write_bytes(promoted_bytes)
    evidence = {
        "schema_version": 1,
        "release_version": RELEASE_VERSION,
        "candidate_ref": candidate_ref,
        "certified_source_revision": candidate_sha,
        "acceptance_workflow_run": acceptance_run_id,
        "accepted_artifact_id": int(accepted_artifact_id),
        "accepted_artifact_digest": artifact_digest,
        "accepted_manifest_sha256": sha256_bytes(accepted_bytes),
        "promoted_manifest_sha256": sha256_bytes(promoted_bytes),
        "application_digest": promoted["application"]["digest"],
        "runtime_digest": promoted["runtime"]["digest"],
        "worker_digest": promoted["worker"]["digest"],
        "acceptance_workflow_verified": True,
        "semantic_diff": ["version"],
    }
    evidence_path.write_bytes(canonical_json(evidence))
    return manifest_path, evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-run-metadata", required=True, type=Path)
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--artifact-archive", required=True, type=Path)
    parser.add_argument("--certified-installer", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--acceptance-run-id", required=True)
    parser.add_argument("--accepted-artifact-id", required=True)
    arguments = parser.parse_args()
    promote(
        workflow_run_metadata=arguments.workflow_run_metadata,
        artifact_metadata=arguments.artifact_metadata,
        artifact_archive=arguments.artifact_archive,
        certified_installer=arguments.certified_installer,
        output_directory=arguments.output_directory,
        candidate_ref=arguments.candidate_ref,
        candidate_sha=arguments.candidate_sha,
        acceptance_run_id=arguments.acceptance_run_id,
        accepted_artifact_id=arguments.accepted_artifact_id,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PromotionError as error:
        print(f"Release manifest promotion failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
