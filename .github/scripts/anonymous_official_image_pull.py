#!/usr/bin/env python3
"""Verify both official Orchestra images in one fresh anonymous DIND daemon."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, SCRIPT_DIRECTORY)

from official_image_publication import (  # noqa: E402
    APPLICATION_REPOSITORY,
    RUNTIME_REPOSITORY,
    PublicationContractError,
    validate_candidate_sha,
    validate_canonical_digest,
    validate_image_inspection,
)

DIND_IMAGE = "docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515"
_OUTER_ENVIRONMENT = {
    "PATH": os.defpath,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}


class AnonymousImageSetPullError(RuntimeError):
    pass


def prove_anonymous_image_set(
    application_digest: object,
    runtime_digest: object,
    expected_revision: object,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    try:
        candidate = validate_candidate_sha(expected_revision)
        application_reference = APPLICATION_REPOSITORY + "@" + validate_canonical_digest(
            application_digest, field="application digest"
        )
        runtime_reference = RUNTIME_REPOSITORY + "@" + validate_canonical_digest(
            runtime_digest, field="runtime digest"
        )
    except PublicationContractError as error:
        raise AnonymousImageSetPullError(str(error)) from error
    name = "orchestra-official-acceptance-" + secrets.token_hex(8)

    def run(arguments: list[str], *, timeout: int, capture: bool = False) -> subprocess.CompletedProcess[str]:
        try:
            return runner(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=timeout,
                env=_OUTER_ENVIRONMENT,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise AnonymousImageSetPullError("anonymous image-set Docker operation failed") from error

    try:
        started = run([
            "docker", "run", "--detach", "--privileged", "--name", name,
            "--env", "DOCKER_TLS_CERTDIR=",
            "--env", "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
            DIND_IMAGE,
            "dockerd", "--host=unix:///var/run/docker.sock",
            "--storage-driver=overlay2", "--log-level=error",
        ], timeout=60)
        if started.returncode != 0:
            raise AnonymousImageSetPullError("fresh anonymous DIND failed to start")
        for _attempt in range(60):
            ready = run([
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "info",
            ], timeout=5)
            if ready.returncode == 0:
                break
            sleeper(1)
        else:
            raise AnonymousImageSetPullError("fresh anonymous DIND did not become ready")

        inspections: dict[str, str] = {}
        for image_name, reference in (
            ("application", application_reference),
            ("runtime", runtime_reference),
        ):
            preexisting = run([
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "image", "inspect", reference,
            ], timeout=10)
            if preexisting.returncode == 0:
                raise AnonymousImageSetPullError("fresh daemon unexpectedly contains an official image")
            pulled = run([
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "image", "pull", "--platform", "linux/amd64", reference,
            ], timeout=900)
            if pulled.returncode != 0:
                raise AnonymousImageSetPullError(f"anonymous {image_name} exact-digest pull failed")
            inspected = run([
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "image", "inspect", reference,
            ], timeout=30, capture=True)
            if inspected.returncode != 0:
                raise AnonymousImageSetPullError(f"fresh-daemon {image_name} inspection failed")
            inspections[image_name] = inspected.stdout

        try:
            validate_image_inspection(
                inspections["application"], image_name="application",
                candidate_sha=candidate, expected_reference=application_reference,
            )
            validate_image_inspection(
                inspections["runtime"], image_name="runtime",
                candidate_sha=candidate, expected_reference=runtime_reference,
            )
        except PublicationContractError as error:
            raise AnonymousImageSetPullError(str(error)) from error
    finally:
        removed = run(["docker", "rm", "--force", name], timeout=30)
        if removed.returncode != 0:
            raise AnonymousImageSetPullError("fresh anonymous DIND cleanup failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-digest", required=True)
    parser.add_argument("--runtime-digest", required=True)
    parser.add_argument("--expected-revision", required=True)
    arguments = parser.parse_args()
    prove_anonymous_image_set(
        arguments.application_digest,
        arguments.runtime_digest,
        arguments.expected_revision,
    )
    try:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("anonymous_digest_pull=PASS\n")
            stream.write("anonymous_pull_fresh_daemon=YES\n")
            stream.write("image_set_complete=YES\n")
    except (KeyError, OSError) as error:
        raise AnonymousImageSetPullError("anonymous image-set evidence cannot be recorded") from error
    print("ANONYMOUS_OFFICIAL_IMAGE_SET_PULL=PASS")
    print("ANONYMOUS_PULL_FRESH_DAEMON=YES")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnonymousImageSetPullError as error:
        print(f"Anonymous official image pull failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
