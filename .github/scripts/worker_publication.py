#!/usr/bin/env python3
"""Trusted validation and record construction for worker publication."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping


_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_BRANCH_BODY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
EXPECTED_REPOSITORY = "ghcr.io/bebet0o/orchestra-worker"


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
    requested_candidate_sha: object,
    fetched_candidate_sha: object,
    checked_out_sha: object,
    revision_label: object,
    repository: object,
    platform: object,
    registry_digest: object,
    workflow_run_id: object,
) -> dict[str, object]:
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
    if not isinstance(registry_digest, str) or _DIGEST.fullmatch(registry_digest) is None:
        raise PublicationContractError("registry digest is not canonical")
    if (
        not isinstance(workflow_run_id, str)
        or not workflow_run_id.isascii()
        or not workflow_run_id.isdecimal()
    ):
        raise PublicationContractError("workflow run identity is invalid")
    return {
        "schema_version": 1,
        "source_commit": source_commit,
        "repository": repository,
        "platform": platform,
        "oci_digest": registry_digest,
        "image_reference": repository + "@" + registry_digest,
        "workflow_run": workflow_run_id,
    }


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
        choices=("validate-request", "verify-fetched", "verify-checkout", "record"),
    )
    arguments = parser.parse_args(argv)

    requested = validate_candidate_sha(_required(environment, "REQUESTED_CANDIDATE_SHA"))
    if arguments.command == "validate-request":
        candidate_ref = validate_candidate_ref(_required(environment, "REQUESTED_CANDIDATE_REF"))
        _write_output("candidate_ref", candidate_ref, environment)
        _write_output("candidate_sha", requested, environment)
        return 0

    fetched = validate_candidate_sha(_required(environment, "FETCHED_CANDIDATE_SHA"))
    if fetched != requested:
        raise PublicationContractError("fetched branch head does not equal candidate_sha")
    if arguments.command == "verify-fetched":
        _write_output("candidate_sha", fetched, environment)
        return 0

    checked_out = validate_candidate_sha(_required(environment, "CHECKED_OUT_SHA"))
    verified = verify_source_identities(requested, fetched, checked_out, checked_out)
    if arguments.command == "verify-checkout":
        _write_output("candidate_sha", verified, environment)
        return 0

    record = build_publication_record(
        requested_candidate_sha=requested,
        fetched_candidate_sha=fetched,
        checked_out_sha=checked_out,
        revision_label=_required(environment, "REVISION_LABEL"),
        repository=_required(environment, "IMAGE_REPOSITORY"),
        platform=_required(environment, "PLATFORM"),
        registry_digest=_required(environment, "OCI_DIGEST"),
        workflow_run_id=_required(environment, "WORKFLOW_RUN"),
    )
    output = Path(__file__).resolve().parents[2] / "worker-publication.json"
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
