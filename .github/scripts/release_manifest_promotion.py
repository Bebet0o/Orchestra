#!/usr/bin/env python3
"""Promote one exact accepted RC2 manifest to the v0.1.0 release contract."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import stat
import zipfile
from pathlib import Path


CERTIFIED_SOURCE = "172071c71cf58076e85524b57cfa19ec8e9f5cb8"
CANDIDATE_REF = "refs/heads/milestone/0.1-release-distribution-rc2"
CANDIDATE_VERSION = "candidate-" + CERTIFIED_SOURCE
RELEASE_VERSION = "v0.1.0"
ACCEPTANCE_RUN = "33803446425"
ACCEPTED_ARTIFACT_ID = 9911963558
ACCEPTED_ARTIFACT_NAME = "orchestra-official-publication-accepted-" + CERTIFIED_SOURCE
ACCEPTED_ARTIFACT_DIGEST = "sha256:3e53f2e783c99d873a57803df711ddd1217ae207d8fbab29fbcd95fdfa444cc5"
ACCEPTED_MANIFEST_SHA256 = "9c98604270ab11cd6760b9ca12929e9b4c3a13b196eda96c3afac0d0a29456e9"
S3_MANIFEST_SHA256 = "7044f7ca800c0d18f916a3be4ac12601608012d7211f009db6b2e7b8b4396802"
APPLICATION_DIGEST = "sha256:3fb1c6b2ed6a0e9b2bde04a40ace47296e7827ef76b314cf634cefce8f842076"
RUNTIME_DIGEST = "sha256:fc4445af780a17da815b1a813c29758657e880e78707cedc68aa62c2175baf28"
WORKER_DIGEST = "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
PLATFORM = "linux/amd64"
MANIFEST_MEMBER = "orchestra-release-manifest-accepted.json"
MAX_INPUT_BYTES = 1_048_576


class PromotionError(ValueError):
    """The accepted input cannot be promoted under the frozen contract."""


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


def validate_artifact(metadata_path: Path, archive_path: Path) -> bytes:
    metadata = _load_json_bytes(_read_bounded(metadata_path), description="artifact metadata")
    expected_metadata = {
        "id": ACCEPTED_ARTIFACT_ID,
        "name": ACCEPTED_ARTIFACT_NAME,
        "digest": ACCEPTED_ARTIFACT_DIGEST,
        "workflow_run": {"id": int(ACCEPTANCE_RUN)},
    }
    for key in ("id", "name", "digest"):
        if metadata.get(key) != expected_metadata[key]:
            raise PromotionError(f"accepted artifact {key} is not exact")
    workflow_run = metadata.get("workflow_run")
    if not isinstance(workflow_run, dict) or workflow_run.get("id") != int(ACCEPTANCE_RUN):
        raise PromotionError("accepted artifact workflow run is not exact")
    archive = _read_bounded(archive_path)
    if "sha256:" + sha256_bytes(archive) != ACCEPTED_ARTIFACT_DIGEST:
        raise PromotionError("accepted artifact archive digest is not exact")
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            members = bundle.infolist()
            if len(members) != 1 or members[0].filename != MANIFEST_MEMBER:
                raise PromotionError("accepted artifact file set is not exact")
            member = members[0]
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                raise PromotionError("accepted manifest member is unsafe")
            return bundle.read(member)
    except zipfile.BadZipFile as error:
        raise PromotionError("accepted artifact archive is invalid") from error


def expected_image(repository: str, digest: str) -> dict[str, str]:
    return {
        "digest": digest,
        "image_reference": repository + "@" + digest,
        "repository": repository,
    }


def validate_accepted_manifest(value: bytes) -> dict[str, object]:
    if sha256_bytes(value) != ACCEPTED_MANIFEST_SHA256:
        raise PromotionError("accepted manifest SHA-256 is not exact")
    manifest = _load_json_bytes(value, description="accepted manifest")
    expected = {
        "schema_version": 1,
        "publication_state": "accepted",
        "version": CANDIDATE_VERSION,
        "candidate_ref": CANDIDATE_REF,
        "source_revision": CERTIFIED_SOURCE,
        "platform": PLATFORM,
        "application": expected_image("ghcr.io/bebet0o/orchestra", APPLICATION_DIGEST),
        "runtime": expected_image("ghcr.io/bebet0o/orchestra-runtime", RUNTIME_DIGEST),
        "worker": expected_image("ghcr.io/bebet0o/orchestra-worker", WORKER_DIGEST),
        "workflow_run": ACCEPTANCE_RUN,
        "anonymous_verification": {
            "digest_pull": "PASS",
            "fresh_daemon": "YES",
            "image_set_complete": "YES",
        },
    }
    if manifest != expected:
        raise PromotionError("accepted manifest authority is not exact")
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


def validate_installer_contract(installer_path: Path, promoted: dict[str, object]) -> None:
    installer = _read_bounded(installer_path).decode("utf-8")
    required_predicates = (
        '.publication_state == "accepted"',
        '.version == "v0.1.0"',
        '.platform == "linux/amd64"',
        '.application.repository == "ghcr.io/bebet0o/orchestra"',
        '.runtime.repository == "ghcr.io/bebet0o/orchestra-runtime"',
        '.worker.repository == "ghcr.io/bebet0o/orchestra-worker"',
    )
    if any(predicate not in installer for predicate in required_predicates):
        raise PromotionError("certified installer predicate is not recognized")
    if promoted != {
        **validate_accepted_manifest(canonical_json({**promoted, "version": CANDIDATE_VERSION})),
        "version": RELEASE_VERSION,
    }:
        raise PromotionError("promoted manifest does not satisfy certified authority")


def promote(metadata_path: Path, archive_path: Path, installer_path: Path, output_dir: Path) -> tuple[Path, Path]:
    accepted_bytes = validate_artifact(metadata_path, archive_path)
    accepted = validate_accepted_manifest(accepted_bytes)
    promoted = promote_manifest(accepted)
    promoted_bytes = canonical_json(promoted)
    validate_installer_contract(installer_path, promoted)
    promoted_hash = sha256_bytes(promoted_bytes)
    if promoted_hash != S3_MANIFEST_SHA256:
        raise PromotionError("promoted manifest is not byte-identical to certified S3 manifest")
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = output_dir / "orchestra-release-manifest.json"
    evidence_path = output_dir / "orchestra-release-promotion-evidence.json"
    manifest_path.write_bytes(promoted_bytes)
    evidence = {
        "schema_version": 1,
        "release_version": RELEASE_VERSION,
        "certified_source_revision": CERTIFIED_SOURCE,
        "accepted_artifact_id": ACCEPTED_ARTIFACT_ID,
        "accepted_artifact_digest": ACCEPTED_ARTIFACT_DIGEST,
        "accepted_manifest_sha256": ACCEPTED_MANIFEST_SHA256,
        "promoted_manifest_sha256": promoted_hash,
        "s3_manifest_sha256": S3_MANIFEST_SHA256,
        "promoted_manifest_equals_s3_manifest": True,
        "application_digest": APPLICATION_DIGEST,
        "runtime_digest": RUNTIME_DIGEST,
        "worker_digest": WORKER_DIGEST,
        "semantic_diff": ["version"],
    }
    evidence_path.write_bytes(canonical_json(evidence))
    return manifest_path, evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--artifact-archive", required=True, type=Path)
    parser.add_argument("--certified-installer", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    arguments = parser.parse_args()
    promote(arguments.artifact_metadata, arguments.artifact_archive, arguments.certified_installer, arguments.output_directory)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PromotionError as error:
        print(f"Release manifest promotion failed: {error}", file=__import__("sys").stderr)
        raise SystemExit(1) from None
