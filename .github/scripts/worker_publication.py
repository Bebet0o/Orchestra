#!/usr/bin/env python3
"""Trusted validation and record construction for worker publication."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping


_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_BRANCH_BODY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
EXPECTED_REPOSITORY = "ghcr.io/bebet0o/orchestra-worker"
MAX_PUSH_OUTPUT_BYTES = 1_048_576


class PublicationContractError(ValueError):
    """Trusted publication input or identity did not validate."""


def validate_candidate_sha(value: object) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise PublicationContractError(
            "candidate_sha must be exactly 40 lowercase hexadecimal characters"
        )
    return value


def validate_candidate_ref(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("refs/heads/"):
        raise PublicationContractError(
            "candidate_ref must be a full repository branch ref"
        )
    body = value.removeprefix("refs/heads/")
    if (
        len(value) > 240
        or _BRANCH_BODY.fullmatch(body) is None
        or ".." in body
        or "//" in body
        or "@{" in body
        or body.endswith(("/", ".", ".lock"))
        or any(
            component.startswith(".") or component.endswith((".", ".lock"))
            for component in body.split("/")
        )
    ):
        raise PublicationContractError("candidate_ref is not canonical")
    return value


def validate_canonical_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PublicationContractError(f"{field} is not a canonical sha256 digest")
    return value


def validate_local_image_id(value: object) -> str:
    return validate_canonical_digest(value, field="local image ID")


def resolve_detached_checkout(path: Path) -> str:
    try:
        symbolic = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if symbolic.returncode == 0:
            raise PublicationContractError("candidate checkout is not detached")
        if symbolic.returncode != 1:
            raise PublicationContractError("candidate checkout state is invalid")
        resolved = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        raise PublicationContractError("candidate checkout cannot be inspected") from error
    if resolved.returncode != 0:
        raise PublicationContractError("candidate checkout HEAD cannot be resolved")
    return validate_candidate_sha(resolved.stdout.rstrip("\n"))


def parse_push_registry_digest(path: Path) -> str:
    try:
        metadata = path.stat()
        if not path.is_file() or path.is_symlink() or metadata.st_size > MAX_PUSH_OUTPUT_BYTES:
            raise PublicationContractError("Docker push output is not a bounded regular file")
        output = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PublicationContractError("Docker push output cannot be read safely") from error
    matches = re.findall(
        r"(?m)^digest:[ \t]+(sha256:[0-9a-f]{64})"
        r"[ \t]+size:[ \t]+[0-9]+[ \t]*$",
        output,
    )
    if len(matches) != 1:
        raise PublicationContractError("Docker push output lacks one unambiguous digest")
    return validate_canonical_digest(matches[0], field="registry digest")


def verify_pushed_image_binding(
    *,
    requested_candidate_sha: object,
    validated_local_image_id: object,
    registry_tag_image_id: object,
    repository: object,
    registry_tag: object,
    registry_digest: object,
    repo_digests_json: object,
) -> str:
    candidate_sha = validate_candidate_sha(requested_candidate_sha)
    local_id = validate_local_image_id(validated_local_image_id)
    tagged_id = validate_local_image_id(registry_tag_image_id)
    if local_id != tagged_id:
        raise PublicationContractError("registry tag does not identify the validated local image")
    if repository != EXPECTED_REPOSITORY:
        raise PublicationContractError("publication repository is invalid")
    expected_tag = EXPECTED_REPOSITORY + ":candidate-" + candidate_sha
    if registry_tag != expected_tag:
        raise PublicationContractError("candidate registry tag is invalid")
    digest = validate_canonical_digest(registry_digest, field="registry digest")
    if not isinstance(repo_digests_json, str) or len(repo_digests_json) > 262_144:
        raise PublicationContractError("registry RepoDigests are invalid")
    try:
        repo_digests = json.loads(repo_digests_json)
    except json.JSONDecodeError as error:
        raise PublicationContractError("registry RepoDigests are malformed") from error
    immutable_reference = EXPECTED_REPOSITORY + "@" + digest
    if (
        not isinstance(repo_digests, list)
        or any(not isinstance(item, str) for item in repo_digests)
        or immutable_reference not in repo_digests
    ):
        raise PublicationContractError("registry digest is not bound to the validated image")
    return digest


def verify_source_identities(
    requested_candidate_sha: object,
    fetched_candidate_sha: object,
    checked_out_sha: object,
    revision_label: object,
) -> str:
    identities = tuple(
        validate_candidate_sha(value)
        for value in (
            requested_candidate_sha,
            fetched_candidate_sha,
            checked_out_sha,
            revision_label,
        )
    )
    if len(set(identities)) != 1:
        raise PublicationContractError("candidate source identities do not agree")
    return identities[0]


def build_publication_record(
    *,
    publication_state: object,
    candidate_ref: object,
    requested_candidate_sha: object,
    fetched_candidate_sha: object,
    checked_out_sha: object,
    revision_label: object,
    repository: object,
    platform: object,
    registry_digest: object,
    workflow_run_id: object,
    ghcr_package_public: object | None = None,
    anonymous_digest_pull: object | None = None,
    anonymous_pull_fresh_daemon: object | None = None,
) -> dict[str, object]:
    candidate_ref = validate_candidate_ref(candidate_ref)
    source_commit = verify_source_identities(
        requested_candidate_sha,
        fetched_candidate_sha,
        checked_out_sha,
        revision_label,
    )
    if repository != EXPECTED_REPOSITORY:
        raise PublicationContractError("publication repository is invalid")
    if platform != "linux/amd64":
        raise PublicationContractError("publication platform is invalid")
    registry_digest = validate_canonical_digest(
        registry_digest,
        field="registry digest",
    )
    if (
        not isinstance(workflow_run_id, str)
        or not workflow_run_id.isascii()
        or not workflow_run_id.isdecimal()
    ):
        raise PublicationContractError("workflow run identity is invalid")
    if publication_state not in {"provisional", "accepted"}:
        raise PublicationContractError("publication state is invalid")
    record: dict[str, object] = {
        "schema_version": 2,
        "publication_state": publication_state,
        "candidate_ref": candidate_ref,
        "candidate_sha": source_commit,
        "source_commit": source_commit,
        "repository": repository,
        "platform": platform,
        "oci_digest": registry_digest,
        "image_reference": repository + "@" + registry_digest,
        "workflow_run": workflow_run_id,
    }
    if publication_state == "provisional":
        if any(
            value is not None
            for value in (
                ghcr_package_public,
                anonymous_digest_pull,
                anonymous_pull_fresh_daemon,
            )
        ):
            raise PublicationContractError("provisional publication cannot claim acceptance")
        record["anonymous_pull"] = "not-yet-verified"
    else:
        if (
            ghcr_package_public != "YES"
            or anonymous_digest_pull != "PASS"
            or anonymous_pull_fresh_daemon != "YES"
        ):
            raise PublicationContractError("accepted publication lacks anonymous proof")
        record.update(
            {
                "GHCR_PACKAGE_PUBLIC": "YES",
                "ANONYMOUS_DIGEST_PULL": "PASS",
                "ANONYMOUS_PULL_FRESH_DAEMON": "YES",
            }
        )
    return record


def _required(environment: Mapping[str, str], name: str) -> str:
    try:
        return environment[name]
    except KeyError as error:
        raise PublicationContractError(f"required trusted input {name} is absent") from error


def _write_output(name: str, value: str, environment: Mapping[str, str]) -> None:
    output = Path(_required(environment, "GITHUB_OUTPUT"))
    with output.open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def main(
    argv: list[str] | None = None,
    environment: Mapping[str, str] = os.environ,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "validate-request",
            "verify-fetched",
            "verify-checkout",
            "validate-local-image",
            "verify-pushed",
            "validate-acceptance",
            "record-provisional",
            "record-accepted",
        ),
    )
    parser.add_argument("--checkout-path", type=Path)
    arguments = parser.parse_args(argv)

    requested = validate_candidate_sha(_required(environment, "REQUESTED_CANDIDATE_SHA"))
    if arguments.command in {"validate-request", "validate-acceptance"}:
        candidate_ref = validate_candidate_ref(_required(environment, "REQUESTED_CANDIDATE_REF"))
        digest = None
        if arguments.command == "validate-acceptance":
            digest = validate_canonical_digest(
                _required(environment, "REQUESTED_IMAGE_DIGEST"),
                field="acceptance image digest",
            )
        _write_output("candidate_ref", candidate_ref, environment)
        _write_output("candidate_sha", requested, environment)
        if arguments.command == "validate-acceptance":
            assert digest is not None
            _write_output("image_digest", digest, environment)
            _write_output("image_reference", EXPECTED_REPOSITORY + "@" + digest, environment)
        return 0

    fetched = validate_candidate_sha(_required(environment, "FETCHED_CANDIDATE_SHA"))
    if fetched != requested:
        raise PublicationContractError("fetched branch head does not equal candidate_sha")
    if arguments.command == "verify-fetched":
        _write_output("candidate_sha", fetched, environment)
        return 0

    if arguments.command == "verify-checkout":
        if arguments.checkout_path is None:
            raise PublicationContractError("candidate checkout path is required")
        checked_out = resolve_detached_checkout(arguments.checkout_path)
    else:
        checked_out = validate_candidate_sha(_required(environment, "CHECKED_OUT_SHA"))
    verified = verify_source_identities(requested, fetched, checked_out, checked_out)
    if arguments.command == "verify-checkout":
        _write_output("candidate_sha", verified, environment)
        return 0

    if arguments.command == "validate-local-image":
        local_image_id = validate_local_image_id(
            _required(environment, "LOCAL_IMAGE_ID")
        )
        _write_output("validated_local_image_id", local_image_id, environment)
        return 0

    if arguments.command == "verify-pushed":
        push_digest = parse_push_registry_digest(
            Path(_required(environment, "PUSH_OUTPUT_PATH"))
        )
        verified_digest = verify_pushed_image_binding(
            requested_candidate_sha=requested,
            validated_local_image_id=_required(
                environment, "VALIDATED_LOCAL_IMAGE_ID"
            ),
            registry_tag_image_id=_required(environment, "REGISTRY_TAG_IMAGE_ID"),
            repository=_required(environment, "IMAGE_REPOSITORY"),
            registry_tag=_required(environment, "REGISTRY_TAG"),
            registry_digest=push_digest,
            repo_digests_json=_required(environment, "REGISTRY_REPO_DIGESTS"),
        )
        _write_output("registry_digest", verified_digest, environment)
        return 0

    record = build_publication_record(
        publication_state=(
            "provisional" if arguments.command == "record-provisional" else "accepted"
        ),
        candidate_ref=_required(environment, "REQUESTED_CANDIDATE_REF"),
        requested_candidate_sha=requested,
        fetched_candidate_sha=fetched,
        checked_out_sha=checked_out,
        revision_label=_required(environment, "REVISION_LABEL"),
        repository=_required(environment, "IMAGE_REPOSITORY"),
        platform=_required(environment, "PLATFORM"),
        registry_digest=_required(environment, "OCI_DIGEST"),
        workflow_run_id=_required(environment, "WORKFLOW_RUN"),
        ghcr_package_public=environment.get("GHCR_PACKAGE_PUBLIC"),
        anonymous_digest_pull=environment.get("ANONYMOUS_DIGEST_PULL"),
        anonymous_pull_fresh_daemon=environment.get("ANONYMOUS_PULL_FRESH_DAEMON"),
    )
    suffix = "provisional" if arguments.command == "record-provisional" else "accepted"
    output = Path(__file__).resolve().parents[2] / f"worker-publication-{suffix}.json"
    with output.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationContractError as error:
        print(f"Worker publication validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
