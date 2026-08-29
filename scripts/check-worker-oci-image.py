#!/usr/bin/env python3
"""Validate a local candidate or an exact published Orchestra worker image."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.oci_reference import parse_immutable_oci_reference  # noqa: E402


EXPECTED_REPOSITORY = "ghcr.io/bebet0o/orchestra-worker"
EXPECTED_SOURCE = "https://github.com/bebet0o/Orchestra"
EXPECTED_BASE_DIGEST = (
    "sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
)
SENSITIVE_FRAGMENTS = (
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "PRIVATE_KEY",
)


class WorkerImageContractError(RuntimeError):
    pass


def docker(
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=environment,
        )
    except OSError as error:
        raise WorkerImageContractError("Docker is unavailable") from error
    if result.returncode != 0:
        raise WorkerImageContractError("Worker image contract command failed")
    return result


def inspect_image(image: str) -> dict[str, Any]:
    try:
        payload = json.loads(docker("image", "inspect", image).stdout)
    except json.JSONDecodeError as error:
        raise WorkerImageContractError("Worker image inspection is malformed") from error
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise WorkerImageContractError("Worker image inspection is invalid")
    return payload[0]


def validate_metadata(
    inspected: dict[str, Any],
    *,
    expected_revision: str | None,
) -> None:
    config = inspected.get("Config")
    if not isinstance(config, dict):
        raise WorkerImageContractError("Worker image configuration is invalid")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise WorkerImageContractError("Worker image labels are absent")
    if labels.get("org.opencontainers.image.source") != EXPECTED_SOURCE:
        raise WorkerImageContractError("Worker image source label mismatched")
    if labels.get("org.opencontainers.image.base.digest") != EXPECTED_BASE_DIGEST:
        raise WorkerImageContractError("Worker image base digest label mismatched")
    revision = labels.get("org.opencontainers.image.revision")
    version = labels.get("org.opencontainers.image.version")
    if not isinstance(revision, str) or not revision:
        raise WorkerImageContractError("Worker image revision label is absent")
    if expected_revision is not None and revision != expected_revision:
        raise WorkerImageContractError("Worker image revision label mismatched")
    if not isinstance(version, str) or not version:
        raise WorkerImageContractError("Worker image version label is absent")
    if config.get("Cmd") != ["sleep", "infinity"]:
        raise WorkerImageContractError("Worker image default command mismatched")
    if config.get("Entrypoint") not in (None, []):
        raise WorkerImageContractError("Worker image entrypoint is unexpected")
    environment = config.get("Env")
    if not isinstance(environment, list) or "HOME=/home/orchestra" not in environment:
        raise WorkerImageContractError("Worker image HOME contract mismatched")
    volumes = config.get("Volumes")
    if volumes not in (None, {}):
        rendered_volumes = json.dumps(volumes, sort_keys=True)
        if "docker.sock" in rendered_volumes:
            raise WorkerImageContractError("Worker image embeds a Docker socket")
    rendered = json.dumps(
        {"Env": environment, "Labels": labels},
        sort_keys=True,
    ).upper()
    if any(fragment in rendered for fragment in SENSITIVE_FRAGMENTS):
        raise WorkerImageContractError("Worker image metadata looks sensitive")


def validate_arbitrary_numeric_identity(image: str) -> None:
    probe = """
set -eu
test "$(id -u)" = 12345
test "$(id -g)" = 23456
test "$HOME" = /home/orchestra
touch "$HOME/worker-contract-home"
touch /workspace/worker-contract-workspace
test ! -S /var/run/docker.sock
for executable in bash git sh find grep ps sed; do
    command -v "$executable" >/dev/null
done
git --version >/dev/null
""".strip()
    docker(
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--user",
        "12345:23456",
        "--tmpfs",
        "/home/orchestra:rw,mode=1777",
        "--tmpfs",
        "/workspace:rw,mode=1777",
        image,
        "bash",
        "-c",
        probe,
    )

    reviewer_probe = """
set -eu
test "$(id -u)" = 12345
test "$HOME" = /home/orchestra
touch "$HOME/reviewer-contract-home"
if touch /workspace/reviewer-must-remain-read-only 2>/dev/null; then
    exit 1
fi
test ! -S /var/run/docker.sock
git --version >/dev/null
""".strip()
    docker(
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--user",
        "12345:23456",
        "--tmpfs",
        "/home/orchestra:rw,mode=1777",
        image,
        "bash",
        "-c",
        reviewer_probe,
    )


def anonymous_pull(image_reference: str) -> None:
    parsed = parse_immutable_oci_reference(image_reference)
    if not parsed.image_reference.startswith(EXPECTED_REPOSITORY + "@"):
        raise WorkerImageContractError("Anonymous pull target repository mismatched")
    with tempfile.TemporaryDirectory(prefix="orchestra-anonymous-docker-") as directory:
        environment = {
            "DOCKER_CONFIG": directory,
            "HOME": directory,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        docker(
            "image",
            "pull",
            "--platform",
            "linux/amd64",
            parsed.image_reference,
            environment=environment,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-revision")
    parser.add_argument("--anonymous-pull", action="store_true")
    arguments = parser.parse_args()

    if arguments.anonymous_pull:
        anonymous_pull(arguments.image)
    inspected = inspect_image(arguments.image)
    validate_metadata(
        inspected,
        expected_revision=arguments.expected_revision,
    )
    validate_arbitrary_numeric_identity(arguments.image)
    print("ORCHESTRA_WORKER_OCI_IMAGE_CONTRACT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TypeError, ValueError, WorkerImageContractError) as error:
        print(f"Worker image contract failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
