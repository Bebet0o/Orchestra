#!/usr/bin/env python3
"""Prove an anonymous exact worker pull in a disposable, empty DIND daemon."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable


DIND_IMAGE = "docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515"
EXPECTED_REPOSITORY = "ghcr.io/bebet0o/orchestra-worker"
EXPECTED_SOURCE = "https://github.com/bebet0o/Orchestra"
EXPECTED_BASE_DIGEST = (
    "sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
)
_SHA = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_REFERENCE = re.compile(
    re.escape(EXPECTED_REPOSITORY) + r"@sha256:[0-9a-f]{64}"
)
_OUTER_ENVIRONMENT = {
    "PATH": os.defpath,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}


class AnonymousPullError(RuntimeError):
    pass


def prove_anonymous_pull(
    image_reference: str,
    expected_revision: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    if _REFERENCE.fullmatch(image_reference) is None:
        raise AnonymousPullError("anonymous pull requires the exact worker digest")
    if _SHA.fullmatch(expected_revision) is None:
        raise AnonymousPullError("anonymous pull candidate revision is invalid")
    name = "orchestra-anonymous-pull-" + secrets.token_hex(8)

    def run(
        arguments: list[str],
        *,
        timeout: int,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
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
            raise AnonymousPullError("anonymous pull Docker operation failed") from error

    try:
        started = run(
            [
                "docker", "run", "--detach", "--privileged", "--name", name,
                "--env", "DOCKER_TLS_CERTDIR=",
                "--env", "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                DIND_IMAGE,
                "dockerd", "--host=unix:///var/run/docker.sock",
                "--storage-driver=overlay2", "--log-level=error",
            ],
            timeout=60,
        )
        if started.returncode != 0:
            raise AnonymousPullError("fresh anonymous DIND failed to start")
        for _attempt in range(60):
            ready = run(
                [
                    "docker", "exec", "--env",
                    "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                    name, "docker", "info",
                ],
                timeout=5,
            )
            if ready.returncode == 0:
                break
            sleeper(1)
        else:
            raise AnonymousPullError("fresh anonymous DIND did not become ready")

        preexisting = run(
            [
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "image", "inspect", image_reference,
            ],
            timeout=10,
        )
        if preexisting.returncode == 0:
            raise AnonymousPullError("fresh daemon unexpectedly contains worker image")
        pulled = run(
            [
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "image", "pull",
                "--platform", "linux/amd64", image_reference,
            ],
            timeout=900,
        )
        if pulled.returncode != 0:
            raise AnonymousPullError("anonymous exact-digest pull failed")
        inspected = run(
            [
                "docker", "exec", "--env",
                "DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config",
                name, "docker", "image", "inspect", image_reference,
            ],
            timeout=30,
            capture=True,
        )
        if inspected.returncode != 0 or len(inspected.stdout) > 262_144:
            raise AnonymousPullError("fresh-daemon image inspection failed")
        try:
            payload = json.loads(inspected.stdout)
        except json.JSONDecodeError as error:
            raise AnonymousPullError("fresh-daemon image metadata are malformed") from error
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise AnonymousPullError("fresh-daemon image inspection is invalid")
        image = payload[0]
        repo_digests = image.get("RepoDigests")
        if not isinstance(repo_digests, list) or image_reference not in repo_digests:
            raise AnonymousPullError("fresh daemon lacks the exact RepoDigest")
        config = image.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if (
            _IMAGE_ID.fullmatch(str(image.get("Id"))) is None
            or image.get("Os") != "linux"
            or image.get("Architecture") != "amd64"
            or not isinstance(labels, dict)
            or labels.get("org.opencontainers.image.source") != EXPECTED_SOURCE
            or labels.get("org.opencontainers.image.base.digest") != EXPECTED_BASE_DIGEST
            or labels.get("org.opencontainers.image.revision") != expected_revision
            or labels.get("org.opencontainers.image.version")
            != "candidate-" + expected_revision
        ):
            raise AnonymousPullError("fresh-daemon candidate metadata mismatched")
    finally:
        removed = run(["docker", "rm", "--force", name], timeout=30)
        if removed.returncode != 0:
            raise AnonymousPullError("fresh anonymous DIND cleanup failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-revision", required=True)
    arguments = parser.parse_args()
    prove_anonymous_pull(arguments.image, arguments.expected_revision)
    try:
        output = os.environ["GITHUB_OUTPUT"]
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("ghcr_package_public=YES\n")
            stream.write("anonymous_digest_pull=PASS\n")
            stream.write("anonymous_pull_fresh_daemon=YES\n")
    except (KeyError, OSError) as error:
        raise AnonymousPullError("anonymous pull evidence cannot be recorded") from error
    print("ANONYMOUS_PULL_FRESH_DAEMON=YES")
    print("ANONYMOUS_PULL_HAS_NO_REGISTRY_CREDENTIALS=YES")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnonymousPullError as error:
        print(f"Anonymous worker pull failed: {error}", file=__import__("sys").stderr)
        raise SystemExit(1) from None
