from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
REFERENCE = "ghcr.io/bebet0o/orchestra-worker@" + DIGEST


def load(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PUBLICATION = load(
    "trusted_worker_publication",
    ROOT / ".github/scripts/worker_publication.py",
)
ANONYMOUS = load(
    "trusted_anonymous_worker_pull",
    ROOT / ".github/scripts/anonymous_worker_pull.py",
)


class TrustedPublicationContractTest(unittest.TestCase):
    def run_helper(
        self,
        command: str,
        *arguments: str,
        **values: str,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory(prefix="orchestra-publication-cli-") as directory:
            output = Path(directory) / "github-output"
            output.touch()
            environment = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "GITHUB_OUTPUT": str(output),
                **values,
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / ".github/scripts/worker_publication.py"),
                    command,
                    *arguments,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=environment,
            )
            return completed, output.read_text(encoding="utf-8")

    def test_candidate_ref_and_sha_are_strictly_bounded(self) -> None:
        self.assertEqual(
            PUBLICATION.validate_candidate_ref(
                "refs/heads/milestone/3a-distribution-foundation"
            ),
            "refs/heads/milestone/3a-distribution-foundation",
        )
        self.assertEqual(PUBLICATION.validate_candidate_sha(SHA), SHA)
        for value in (
            "main", "refs/tags/v1", "refs/heads/-candidate", "refs/heads/a..b",
            "refs/heads/a b", "refs/heads/a;touch-pwned", "refs/heads/.hidden",
            "refs/heads/a.lock", "refs/heads/a//b", "refs/heads/a\nmalicious",
        ):
            with self.subTest(value=value), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.validate_candidate_ref(value)
        for value in ("A" * 40, "a" * 39, "a" * 41, "-" + "a" * 40, "a" * 39 + "\n"):
            with self.subTest(value=value), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.validate_candidate_sha(value)

    def test_record_constructs_reference_from_one_source_and_digest(self) -> None:
        record = PUBLICATION.build_publication_record(
            requested_candidate_sha=SHA,
            fetched_candidate_sha=SHA,
            checked_out_sha=SHA,
            revision_label=SHA,
            repository="ghcr.io/bebet0o/orchestra-worker",
            platform="linux/amd64",
            registry_digest=DIGEST,
            workflow_run_id="12345",
        )
        self.assertEqual(record["source_commit"], SHA)
        self.assertEqual(record["oci_digest"], DIGEST)
        self.assertEqual(record["image_reference"], REFERENCE)
        with self.assertRaises(PUBLICATION.PublicationContractError):
            PUBLICATION.build_publication_record(
                requested_candidate_sha=SHA,
                fetched_candidate_sha=SHA,
                checked_out_sha=SHA,
                revision_label=SHA,
                repository="ghcr.io/other/worker",
                platform="linux/amd64",
                registry_digest=DIGEST,
                workflow_run_id="12345",
            )

    def test_source_and_revision_mismatches_fail_closed(self) -> None:
        for field in (
            "requested_candidate_sha", "fetched_candidate_sha",
            "checked_out_sha", "revision_label",
        ):
            values = {
                "requested_candidate_sha": SHA,
                "fetched_candidate_sha": SHA,
                "checked_out_sha": SHA,
                "revision_label": SHA,
            }
            values[field] = "c" * 40
            with self.subTest(field=field), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.build_publication_record(
                    **values,
                    repository="ghcr.io/bebet0o/orchestra-worker",
                    platform="linux/amd64",
                    registry_digest=DIGEST,
                    workflow_run_id="1",
                )

    def test_digest_mismatch_or_independent_reference_cannot_enter_record(self) -> None:
        for digest in ("", "sha256:" + "B" * 64, "sha512:" + "b" * 64, DIGEST + " "):
            with self.subTest(digest=digest), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.build_publication_record(
                    requested_candidate_sha=SHA,
                    fetched_candidate_sha=SHA,
                    checked_out_sha=SHA,
                    revision_label=SHA,
                    repository="ghcr.io/bebet0o/orchestra-worker",
                    platform="linux/amd64",
                    registry_digest=digest,
                    workflow_run_id="1",
                )

    def test_validate_request_cli_emits_only_exact_validated_outputs(self) -> None:
        completed, output = self.run_helper(
            "validate-request",
            REQUESTED_CANDIDATE_REF=(
                "refs/heads/milestone/3a-distribution-foundation"
            ),
            REQUESTED_CANDIDATE_SHA=SHA,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            output,
            "candidate_ref=refs/heads/milestone/3a-distribution-foundation\n"
            f"candidate_sha={SHA}\n",
        )
        completed, output = self.run_helper(
            "validate-request",
            REQUESTED_CANDIDATE_REF="refs/heads/foo;echo-pwned",
            REQUESTED_CANDIDATE_SHA=SHA,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(output, "")

    def test_verify_fetched_cli_fails_mismatch_without_authority_output(self) -> None:
        completed, output = self.run_helper(
            "verify-fetched",
            REQUESTED_CANDIDATE_SHA=SHA,
            FETCHED_CANDIDATE_SHA="c" * 40,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(output, "")
        self.assertNotIn("candidate_sha=", output)

        completed, output = self.run_helper(
            "verify-fetched",
            REQUESTED_CANDIDATE_SHA=SHA,
            FETCHED_CANDIDATE_SHA=SHA,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output, f"candidate_sha={SHA}\n")

    def test_verify_checkout_cli_fails_mismatch_without_authority_output(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="orchestra-feature-checkout-"
        ) as directory:
            repository = Path(directory) / "candidate"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repository),
                    "-c", "user.name=Audit",
                    "-c", "user.email=audit@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "candidate",
                ],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            attached, output = self.run_helper(
                "verify-checkout", "--checkout-path", str(repository),
                REQUESTED_CANDIDATE_SHA=head,
                FETCHED_CANDIDATE_SHA=head,
            )
            self.assertNotEqual(attached.returncode, 0)
            self.assertIn("candidate checkout is not detached", attached.stderr)
            self.assertEqual(output, "")

            subprocess.run(
                ["git", "-C", str(repository), "checkout", "-q", "--detach", head],
                check=True,
            )
            completed, output = self.run_helper(
                "verify-checkout", "--checkout-path", str(repository),
                REQUESTED_CANDIDATE_SHA=SHA,
                FETCHED_CANDIDATE_SHA=SHA,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("candidate source identities do not agree", completed.stderr)
            self.assertNotIn("candidate checkout path is required", completed.stderr)
            self.assertEqual(output, "")
            self.assertNotIn("candidate_sha=", output)

            completed, output = self.run_helper(
                "verify-checkout", "--checkout-path", str(repository),
                REQUESTED_CANDIDATE_SHA=head,
                FETCHED_CANDIDATE_SHA=head,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output, f"candidate_sha={head}\n")


class FreshDaemonAnonymousPullTest(unittest.TestCase):
    def test_exact_pull_uses_empty_ephemeral_daemon_and_cleans_up(self) -> None:
        self.assertEqual(
            ANONYMOUS.DIND_IMAGE,
            "docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515",
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((arguments, kwargs))
            if arguments[-3:-1] == ["image", "inspect"] and "--format" not in arguments:
                return subprocess.CompletedProcess(arguments, 1, "", "")
            if "--format" in arguments:
                return subprocess.CompletedProcess(arguments, 0, json.dumps([REFERENCE]), "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        ANONYMOUS.prove_anonymous_pull(REFERENCE, runner=run, sleeper=lambda _s: None)
        commands = [arguments for arguments, _kwargs in calls]
        start = commands[0]
        self.assertEqual(start[:3], ["docker", "run", "--detach"])
        self.assertIn("--privileged", start)
        self.assertIn(ANONYMOUS.DIND_IMAGE, start)
        self.assertNotIn("--volume", start)
        preinspect_index = next(
            index
            for index, command in enumerate(commands)
            if command[-3:-1] == ["image", "inspect"] and "--format" not in command
        )
        pull = next(command for command in commands if "pull" in command)
        pull_index = commands.index(pull)
        self.assertLess(preinspect_index, pull_index)
        self.assertEqual(pull[-1], REFERENCE)
        self.assertIn("linux/amd64", pull)
        self.assertEqual(commands[-1][:3], ["docker", "rm", "--force"])
        for _arguments, kwargs in calls:
            self.assertIn("timeout", kwargs)
            environment = kwargs["env"]
            self.assertEqual(environment["DOCKER_HOST"], "unix:///var/run/docker.sock")
            self.assertNotIn("HOME", environment)

    def test_wrong_repo_digest_fails_closed_and_cleans_up(self) -> None:
        calls: list[list[str]] = []

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if arguments[-3:-1] == ["image", "inspect"] and "--format" not in arguments:
                return subprocess.CompletedProcess(arguments, 1, "", "")
            if "--format" in arguments:
                wrong = "ghcr.io/other/worker@" + DIGEST
                return subprocess.CompletedProcess(arguments, 0, json.dumps([wrong]), "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with self.assertRaisesRegex(ANONYMOUS.AnonymousPullError, "exact RepoDigest"):
            ANONYMOUS.prove_anonymous_pull(REFERENCE, runner=run, sleeper=lambda _s: None)
        self.assertEqual(calls[-1][:3], ["docker", "rm", "--force"])

    def test_failure_still_removes_the_unique_daemon(self) -> None:
        calls: list[list[str]] = []

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if arguments[:3] == ["docker", "rm", "--force"]:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(arguments, 1, "", "")

        with self.assertRaises(ANONYMOUS.AnonymousPullError):
            ANONYMOUS.prove_anonymous_pull(REFERENCE, runner=run, sleeper=lambda _s: None)
        self.assertEqual(calls[-1][:3], ["docker", "rm", "--force"])


if __name__ == "__main__":
    unittest.main()
