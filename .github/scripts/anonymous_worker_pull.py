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
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    if _REFERENCE.fullmatch(image_reference) is None:
        raise AnonymousPullError("anonymous pull requires the exact worker digest")
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
                "--env", "DOCKER_TLS_CERTDIR=", DIND_IMAGE,
                "dockerd", "--host=unix:///var/run/docker.sock",
                "--storage-driver=overlay2", "--log-level=error",
            ],
            timeout=60,
        )
        if started.returncode != 0:
            raise AnonymousPullError("fresh anonymous DIND failed to start")
        for _attempt in range(60):
            ready = run(
                ["docker", "exec", name, "docker", "info"],
                timeout=5,
            )
            if ready.returncode == 0:
                break
            sleeper(1)
        else:
            raise AnonymousPullError("fresh anonymous DIND did not become ready")

        preexisting = run(
            ["docker", "exec", name, "docker", "image", "inspect", image_reference],
            timeout=10,
        )
        if preexisting.returncode == 0:
            raise AnonymousPullError("fresh daemon unexpectedly contains worker image")
        pulled = run(
            [
                "docker", "exec", name, "docker", "image", "pull",
                "--platform", "linux/amd64", image_reference,
            ],
            timeout=900,
        )
        if pulled.returncode != 0:
            raise AnonymousPullError("anonymous exact-digest pull failed")
        inspected = run(
            [
                "docker", "exec", name, "docker", "image", "inspect",
                "--format", "{{json .RepoDigests}}", image_reference,
            ],
            timeout=30,
            capture=True,
        )
        if inspected.returncode != 0 or len(inspected.stdout) > 262_144:
            raise AnonymousPullError("fresh-daemon image inspection failed")
        try:
            repo_digests = json.loads(inspected.stdout)
        except json.JSONDecodeError as error:
            raise AnonymousPullError("fresh-daemon RepoDigests are malformed") from error
        if not isinstance(repo_digests, list) or image_reference not in repo_digests:
            raise AnonymousPullError("fresh daemon lacks the exact RepoDigest")
    finally:
        removed = run(["docker", "rm", "--force", name], timeout=30)
        if removed.returncode != 0:
            raise AnonymousPullError("fresh anonymous DIND cleanup failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    arguments = parser.parse_args()
    prove_anonymous_pull(arguments.image)
    print("ANONYMOUS_PULL_FRESH_DAEMON=YES")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnonymousPullError as error:
        print(f"Anonymous worker pull failed: {error}", file=__import__("sys").stderr)
        raise SystemExit(1) from None
