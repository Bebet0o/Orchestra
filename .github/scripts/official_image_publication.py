#!/usr/bin/env python3
"""Trusted validation and manifest construction for official Orchestra images."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping

SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, SCRIPT_DIRECTORY)

from worker_publication import (  # noqa: E402
    PublicationContractError,
    parse_push_registry_digest,
    resolve_detached_checkout,
    validate_candidate_ref,
    validate_candidate_sha,
    validate_canonical_digest,
    validate_local_image_id,
)


APPLICATION_REPOSITORY = "ghcr.io/bebet0o/orchestra"
RUNTIME_REPOSITORY = "ghcr.io/bebet0o/orchestra-runtime"
WORKER_REPOSITORY = "ghcr.io/bebet0o/orchestra-worker"
WORKER_DIGEST = "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
SOURCE = "https://github.com/bebet0o/Orchestra"
PLATFORM = "linux/amd64"
MAX_INSPECT_BYTES = 262_144
EXPECTED_FROM_LINES = {
    "images/orchestra.Dockerfile": [
        "FROM docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515 AS docker-cli",
        "FROM python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS application",
    ],
    "images/orchestra-runtime.Dockerfile": [
        "FROM docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515",
    ],
}

IMAGE_CONTRACTS = {
    "application": {
        "repository": APPLICATION_REPOSITORY,
        "title": "Orchestra",
        "user": "orchestra:orchestra",
        "entrypoint": ["python3", "/opt/orchestra/app/scripts/orchestra-appliance.py", "run"],
    },
    "runtime": {
        "repository": RUNTIME_REPOSITORY,
        "title": "Orchestra Runtime",
        "user": "",
        "entrypoint": ["/usr/local/bin/orchestra-runtime-entrypoint"],
    },
}


def verify_source_identities(*values: object) -> str:
    identities = tuple(validate_candidate_sha(value) for value in values)
    if len(set(identities)) != 1:
        raise PublicationContractError("candidate source identities do not agree")
    return identities[0]


def validate_candidate_dockerfiles(checkout: Path) -> None:
    for relative, expected_lines in EXPECTED_FROM_LINES.items():
        path = checkout / relative
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65_536:
                raise PublicationContractError(f"candidate {relative} is unsafe")
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PublicationContractError(f"candidate {relative} cannot be read") from error
        from_lines = [line for line in source.splitlines() if line.upper().startswith("FROM ")]
        if from_lines != expected_lines:
            raise PublicationContractError(f"candidate {relative} base images are not exactly pinned")
        if "/var/run/docker.sock" in source or "/run/docker.sock" in source:
            raise PublicationContractError(f"candidate {relative} assumes a host Docker socket")


def _inspection(value: object, *, image_name: str) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_INSPECT_BYTES:
        raise PublicationContractError(f"{image_name} inspection is invalid")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise PublicationContractError(f"{image_name} inspection is malformed") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise PublicationContractError(f"{image_name} inspection is not singular")
    return payload[0]


def validate_image_inspection(
    value: object,
    *,
    image_name: str,
    candidate_sha: object,
    expected_reference: str | None = None,
) -> str:
    candidate = validate_candidate_sha(candidate_sha)
    contract = IMAGE_CONTRACTS[image_name]
    image = _inspection(value, image_name=image_name)
    image_id = validate_local_image_id(image.get("Id"))
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise PublicationContractError(f"{image_name} platform is not linux/amd64")
    config = image.get("Config")
    if not isinstance(config, dict):
        raise PublicationContractError(f"{image_name} config is absent")
    labels = config.get("Labels")
    expected_labels = {
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.revision": candidate,
        "org.opencontainers.image.version": "candidate-" + candidate,
        "org.opencontainers.image.title": contract["title"],
    }
    if not isinstance(labels, dict) or any(labels.get(key) != expected for key, expected in expected_labels.items()):
        raise PublicationContractError(f"{image_name} OCI metadata mismatched")
    if config.get("User", "") != contract["user"]:
        raise PublicationContractError(f"{image_name} runtime user mismatched")
    if config.get("Entrypoint") != contract["entrypoint"]:
        raise PublicationContractError(f"{image_name} entrypoint mismatched")
    if expected_reference is not None:
        repo_digests = image.get("RepoDigests")
        if (
            not isinstance(repo_digests, list)
            or any(not isinstance(item, str) for item in repo_digests)
            or expected_reference not in repo_digests
        ):
            raise PublicationContractError(f"{image_name} exact RepoDigest is absent")
    return image_id


def verify_pushed_image(
    *,
    image_name: str,
    candidate_sha: object,
    validated_local_image_id: object,
    registry_tag_image_id: object,
    registry_tag: object,
    push_output_path: Path,
    repo_digests_json: object,
) -> str:
    candidate = validate_candidate_sha(candidate_sha)
    contract = IMAGE_CONTRACTS[image_name]
    repository = str(contract["repository"])
    expected_tag = repository + ":candidate-" + candidate
    if registry_tag != expected_tag:
        raise PublicationContractError(f"{image_name} candidate registry tag is invalid")
    local_id = validate_local_image_id(validated_local_image_id)
    tagged_id = validate_local_image_id(registry_tag_image_id)
    if local_id != tagged_id:
        raise PublicationContractError(f"{image_name} registry tag changed local identity")
    digest = parse_push_registry_digest(
        push_output_path,
        expected_tag,
        expected_repository=repository,
    )
    if not isinstance(repo_digests_json, str) or len(repo_digests_json) > MAX_INSPECT_BYTES:
        raise PublicationContractError(f"{image_name} RepoDigests are invalid")
    try:
        repo_digests = json.loads(repo_digests_json)
    except json.JSONDecodeError as error:
        raise PublicationContractError(f"{image_name} RepoDigests are malformed") from error
    reference = repository + "@" + digest
    if (
        not isinstance(repo_digests, list)
        or any(not isinstance(item, str) for item in repo_digests)
        or reference not in repo_digests
    ):
        raise PublicationContractError(f"{image_name} digest is not bound to validated image")
    return digest


def build_release_manifest(
    *,
    publication_state: object,
    candidate_ref: object,
    requested_candidate_sha: object,
    fetched_candidate_sha: object,
    checked_out_sha: object,
    application_revision: object,
    runtime_revision: object,
    application_digest: object,
    runtime_digest: object,
    workflow_run_id: object,
    anonymous_digest_pull: object | None = None,
    anonymous_pull_fresh_daemon: object | None = None,
) -> dict[str, object]:
    reference = validate_candidate_ref(candidate_ref)
    source_commit = verify_source_identities(
        requested_candidate_sha,
        fetched_candidate_sha,
        checked_out_sha,
        application_revision,
        runtime_revision,
    )
    application_digest = validate_canonical_digest(application_digest, field="application digest")
    runtime_digest = validate_canonical_digest(runtime_digest, field="runtime digest")
    if publication_state not in {"provisional", "accepted"}:
        raise PublicationContractError("release publication state is invalid")
    if not isinstance(workflow_run_id, str) or not workflow_run_id.isascii() or not workflow_run_id.isdecimal():
        raise PublicationContractError("workflow run identity is invalid")
    if publication_state == "accepted" and (
        anonymous_digest_pull != "PASS" or anonymous_pull_fresh_daemon != "YES"
    ):
        raise PublicationContractError("accepted image set lacks fresh anonymous proof")
    if publication_state == "provisional" and (
        anonymous_digest_pull is not None or anonymous_pull_fresh_daemon is not None
    ):
        raise PublicationContractError("provisional image set cannot claim acceptance")

    def image(repository: str, digest: str) -> dict[str, str]:
        return {
            "repository": repository,
            "digest": digest,
            "image_reference": repository + "@" + digest,
        }

    manifest: dict[str, object] = {
        "schema_version": 1,
        "publication_state": publication_state,
        "version": "candidate-" + source_commit,
        "candidate_ref": reference,
        "source_revision": source_commit,
        "platform": PLATFORM,
        "application": image(APPLICATION_REPOSITORY, application_digest),
        "runtime": image(RUNTIME_REPOSITORY, runtime_digest),
        "worker": image(WORKER_REPOSITORY, WORKER_DIGEST),
        "workflow_run": workflow_run_id,
    }
    manifest["anonymous_verification"] = (
        {
            "digest_pull": "PASS",
            "fresh_daemon": "YES",
            "image_set_complete": "YES",
        }
        if publication_state == "accepted"
        else {
            "digest_pull": "not-yet-verified",
            "fresh_daemon": "not-yet-verified",
            "image_set_complete": "not-yet-verified",
        }
    )
    return manifest


def _required(environment: Mapping[str, str], name: str) -> str:
    try:
        return environment[name]
    except KeyError as error:
        raise PublicationContractError(f"required trusted input {name} is absent") from error


def _write_output(name: str, value: str, environment: Mapping[str, str]) -> None:
    output = Path(_required(environment, "GITHUB_OUTPUT"))
    with output.open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def _source(environment: Mapping[str, str], checkout_path: Path | None) -> tuple[str, str, str]:
    requested = validate_candidate_sha(_required(environment, "REQUESTED_CANDIDATE_SHA"))
    fetched = validate_candidate_sha(_required(environment, "FETCHED_CANDIDATE_SHA"))
    if fetched != requested:
        raise PublicationContractError("fetched branch head does not equal candidate_sha")
    checked = resolve_detached_checkout(checkout_path) if checkout_path is not None else validate_candidate_sha(
        _required(environment, "CHECKED_OUT_SHA")
    )
    verify_source_identities(requested, fetched, checked)
    return requested, fetched, checked


def main(argv: list[str] | None = None, environment: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=(
        "validate-request", "verify-fetched", "verify-checkout", "validate-dockerfiles", "validate-local-set",
        "verify-pushed-set", "validate-acceptance", "record-provisional", "record-accepted",
    ))
    parser.add_argument("--checkout-path", type=Path)
    arguments = parser.parse_args(argv)
    requested = validate_candidate_sha(_required(environment, "REQUESTED_CANDIDATE_SHA"))
    if arguments.command in {"validate-request", "validate-acceptance"}:
        candidate_ref = validate_candidate_ref(_required(environment, "REQUESTED_CANDIDATE_REF"))
        acceptance: dict[str, tuple[str, str]] = {}
        if arguments.command == "validate-acceptance":
            for image_name, repository in (
                ("application", APPLICATION_REPOSITORY), ("runtime", RUNTIME_REPOSITORY)
            ):
                digest = validate_canonical_digest(
                    _required(environment, f"REQUESTED_{image_name.upper()}_DIGEST"),
                    field=f"{image_name} acceptance digest",
                )
                acceptance[image_name] = (digest, repository + "@" + digest)
        _write_output("candidate_ref", candidate_ref, environment)
        _write_output("candidate_sha", requested, environment)
        for image_name, (digest, image_reference) in acceptance.items():
            _write_output(f"{image_name}_digest", digest, environment)
            _write_output(f"{image_name}_reference", image_reference, environment)
        return 0
    if arguments.command == "verify-fetched":
        fetched = validate_candidate_sha(_required(environment, "FETCHED_CANDIDATE_SHA"))
        if fetched != requested:
            raise PublicationContractError("fetched branch head does not equal candidate_sha")
        _write_output("candidate_sha", fetched, environment)
        return 0
    requested, fetched, checked = _source(
        environment,
        arguments.checkout_path
        if arguments.command in {"verify-checkout", "validate-dockerfiles"}
        else None,
    )
    if arguments.command == "verify-checkout":
        _write_output("candidate_sha", checked, environment)
        return 0
    if arguments.command == "validate-dockerfiles":
        if arguments.checkout_path is None:
            raise PublicationContractError("candidate checkout path is required")
        validate_candidate_dockerfiles(arguments.checkout_path)
        return 0
    if arguments.command == "validate-local-set":
        application_id = validate_image_inspection(
            _required(environment, "APPLICATION_INSPECT_JSON"),
            image_name="application", candidate_sha=checked,
        )
        runtime_id = validate_image_inspection(
            _required(environment, "RUNTIME_INSPECT_JSON"),
            image_name="runtime", candidate_sha=checked,
        )
        _write_output("validated_application_image_id", application_id, environment)
        _write_output("validated_runtime_image_id", runtime_id, environment)
        return 0
    if arguments.command == "verify-pushed-set":
        pushed: dict[str, str] = {}
        for image_name in ("application", "runtime"):
            upper = image_name.upper()
            pushed[image_name] = verify_pushed_image(
                image_name=image_name,
                candidate_sha=checked,
                validated_local_image_id=_required(environment, f"VALIDATED_{upper}_IMAGE_ID"),
                registry_tag_image_id=_required(environment, f"{upper}_REGISTRY_TAG_IMAGE_ID"),
                registry_tag=_required(environment, f"{upper}_REGISTRY_TAG"),
                push_output_path=Path(_required(environment, f"{upper}_PUSH_OUTPUT_PATH")),
                repo_digests_json=_required(environment, f"{upper}_REGISTRY_REPO_DIGESTS"),
            )
        for image_name, digest in pushed.items():
            _write_output(f"{image_name}_digest", digest, environment)
            _write_output(
                f"{image_name}_reference",
                str(IMAGE_CONTRACTS[image_name]["repository"]) + "@" + digest,
                environment,
            )
        return 0
    command_state = "provisional" if arguments.command == "record-provisional" else "accepted"
    manifest = build_release_manifest(
        publication_state=command_state,
        candidate_ref=_required(environment, "REQUESTED_CANDIDATE_REF"),
        requested_candidate_sha=requested,
        fetched_candidate_sha=fetched,
        checked_out_sha=checked,
        application_revision=_required(environment, "APPLICATION_REVISION_LABEL"),
        runtime_revision=_required(environment, "RUNTIME_REVISION_LABEL"),
        application_digest=_required(environment, "APPLICATION_DIGEST"),
        runtime_digest=_required(environment, "RUNTIME_DIGEST"),
        workflow_run_id=_required(environment, "WORKFLOW_RUN"),
        anonymous_digest_pull=environment.get("ANONYMOUS_DIGEST_PULL"),
        anonymous_pull_fresh_daemon=environment.get("ANONYMOUS_PULL_FRESH_DAEMON"),
    )
    output = Path(__file__).resolve().parents[2] / f"orchestra-release-manifest-{command_state}.json"
    with output.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationContractError as error:
        print(f"Official image publication validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
