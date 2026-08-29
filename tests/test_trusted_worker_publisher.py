from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/publish-worker.yml"
ACCEPTANCE_WORKFLOW = ROOT / ".github/workflows/accept-worker-publication.yml"
HELPER = ROOT / ".github/scripts/worker_publication.py"
ANONYMOUS_PULL = ROOT / ".github/scripts/anonymous_worker_pull.py"
CHECKER = ROOT / "scripts/check-worker-oci-image.py"
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


PUBLICATION = load("bootstrap_worker_publication", HELPER)
ANONYMOUS = load("bootstrap_anonymous_worker_pull", ANONYMOUS_PULL)
CHECKER_MODULE = load("bootstrap_worker_checker", CHECKER)


class WorkflowTrustTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.load(self.source, Loader=yaml.BaseLoader)
        self.steps = self.workflow["jobs"]["publish"]["steps"]
        self.by_name = {step["name"]: step for step in self.steps}

    def test_manual_main_only_inputs_and_permissions(self) -> None:
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"candidate_ref", "candidate_sha"})
        self.assertEqual(inputs["candidate_ref"]["required"], "true")
        self.assertEqual(inputs["candidate_sha"]["required"], "true")
        condition = self.workflow["jobs"]["publish"]["if"]
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertIn("github.repository == 'bebet0o/Orchestra'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)
        self.assertNotIn("pull_request", self.source)
        self.assertEqual(
            self.workflow["permissions"],
            {"contents": "read", "packages": "write"},
        )

    def test_actions_are_exact_official_immutable_pins(self) -> None:
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "docker/setup-buildx-action": "e468171a9de216ec08956ac3ada2f0791b6bd435",
            "docker/build-push-action": "263435318d21b8e681c14492fe198d362a7d2c83",
            "docker/login-action": "5e57cd118135c172c3672efd75eb46360885c0ef",
            "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
        }
        found: dict[str, set[str]] = {}
        for action, revision in re.findall(
            r"(?m)^\s*uses:\s*([^@\s]+)@([0-9a-f]{40})(?:\s|$)",
            self.source,
        ):
            found.setdefault(action, set()).add(revision)
        self.assertEqual(set(found), set(expected))
        for action, revision in expected.items():
            self.assertEqual(found[action], {revision})

    def test_trusted_and_candidate_trees_are_separate(self) -> None:
        trusted = self.by_name["Checkout trusted publication foundation"]
        candidate = self.by_name["Checkout detached candidate source"]
        self.assertEqual(trusted["with"]["path"], "trusted")
        self.assertEqual(trusted["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(candidate["with"]["path"], "candidate")
        self.assertEqual(
            candidate["with"]["ref"],
            "${{ steps.fetched.outputs.candidate_sha }}",
        )
        self.assertEqual(trusted["with"]["persist-credentials"], "false")
        self.assertEqual(candidate["with"]["persist-credentials"], "false")
        self.assertEqual(self.source.count("${{ github.sha }}"), 1)
        detached = self.by_name["Verify detached candidate source"]["run"]
        self.assertEqual(
            " ".join(detached.split()),
            "python3 trusted/.github/scripts/worker_publication.py "
            "verify-checkout --checkout-path candidate",
        )

    def test_fetch_uses_fixed_trusted_origin_and_destination(self) -> None:
        run = self.by_name["Fetch and authorize exact candidate branch head"]["run"]
        self.assertIn(
            "git -C trusted fetch --no-tags --no-recurse-submodules "
            "--depth=1 --force origin \\",
            run,
        )
        self.assertIn(
            '"$REQUESTED_CANDIDATE_REF:'
            'refs/remotes/orchestra-publication/candidate"',
            run,
        )
        self.assertNotIn("CANDIDATE_REMOTE", run)
        self.assertEqual(run.count("git -C trusted fetch"), 1)

    def test_every_security_command_has_its_exact_trusted_path(self) -> None:
        expected = {
            "Validate candidate request": (
                "trusted/.github/scripts/worker_publication.py", "validate-request"
            ),
            "Fetch and authorize exact candidate branch head": (
                "trusted/.github/scripts/worker_publication.py", "verify-fetched"
            ),
            "Verify detached candidate source": (
                "trusted/.github/scripts/worker_publication.py", "verify-checkout"
            ),
            "Validate candidate Dockerfile base policy": (
                "trusted/scripts/check-worker-oci-image.py", "--dockerfile-policy"
            ),
            "Resolve exact local image identity": (
                "trusted/.github/scripts/worker_publication.py", "validate-local-image"
            ),
            "Create trusted provisional publication record": (
                "trusted/.github/scripts/worker_publication.py", "record-provisional"
            ),
            "Tag and push exact validated local image": (
                "trusted/.github/scripts/worker_publication.py", "verify-pushed"
            ),
            "Validate local contract candidate with trusted checker": (
                "trusted/scripts/check-worker-oci-image.py", None
            ),
        }
        for name, invocation in expected.items():
            with self.subTest(step=name):
                run = self.by_name[name]["run"]
                matches = re.findall(
                    r"(?m)^\s*python3\s+([^\s\\]+)(?:\s+([a-z-]+))?", run
                )
                self.assertEqual(
                    [(path, command or None) for path, command in matches],
                    [invocation],
                )
                self.assertNotIn("candidate/.github/scripts/", run)
                self.assertNotIn("candidate/scripts/check-worker-oci-image.py", run)

    def test_shell_and_source_dataflow_are_closed(self) -> None:
        shell = "\n".join(step.get("run", "") for step in self.steps)
        self.assertNotIn("${{ inputs.", shell)
        self.assertNotIn("${{ github.", shell)
        request = self.by_name["Validate candidate request"]
        self.assertEqual(
            request["env"],
            {
                "REQUESTED_CANDIDATE_REF": "${{ inputs.candidate_ref }}",
                "REQUESTED_CANDIDATE_SHA": "${{ inputs.candidate_sha }}",
            },
        )
        source = "${{ steps.source.outputs.candidate_sha }}"
        build = self.by_name["Build local contract candidate"]["with"]
        self.assertEqual(build["context"], "candidate")
        self.assertEqual(build["file"], "candidate/images/orchestra-worker.Dockerfile")
        self.assertEqual(build["load"], "true")
        self.assertEqual(build["push"], "false")
        self.assertIn("OCI_REVISION=" + source, build["build-args"])
        self.assertIn("OCI_VERSION=candidate-" + source, build["build-args"])
        worker_builds = [
            step for step in self.steps
            if step.get("uses", "").startswith("docker/build-push-action@")
        ]
        self.assertEqual(worker_builds, [self.by_name["Build local contract candidate"]])
        pushed = self.by_name["Tag and push exact validated local image"]["run"]
        self.assertIn(
            'docker image tag "$VALIDATED_LOCAL_IMAGE_ID" "$REGISTRY_TAG"',
            pushed,
        )
        self.assertIn('docker image push "$REGISTRY_TAG"', pushed)
        self.assertNotIn("docker build", pushed)
        self.assertNotIn("buildx", pushed)
        self.assertIn(
            "VALIDATED_LOCAL_IMAGE_ID: "
            "${{ steps.local.outputs.validated_local_image_id }}",
            self.source,
        )
        record = self.by_name["Create trusted provisional publication record"]
        self.assertEqual(record["env"]["CHECKED_OUT_SHA"], source)
        self.assertEqual(record["env"]["REVISION_LABEL"], source)
        self.assertEqual(
            record["env"]["OCI_DIGEST"],
            "${{ steps.pushed.outputs.registry_digest }}",
        )
        self.assertIn("record-provisional", record["run"])
        self.assertNotIn("record-accepted", self.source)
        upload = self.by_name["Upload provisional publication record"]
        self.assertEqual(upload["with"]["path"], "trusted/worker-publication-provisional.json")

    def test_publication_order_and_same_local_artifact_binding(self) -> None:
        names = [step["name"] for step in self.steps]
        ordered = (
            "Validate candidate Dockerfile base policy",
            "Set up Docker Buildx",
            "Build local contract candidate",
            "Resolve exact local image identity",
            "Validate local contract candidate with trusted checker",
            "Authenticate to GHCR",
            "Tag and push exact validated local image",
            "Create trusted provisional publication record",
        )
        self.assertEqual([names.index(name) for name in ordered], sorted(
            names.index(name) for name in ordered
        ))
        pushed = self.by_name["Tag and push exact validated local image"]
        self.assertEqual(
            pushed["env"]["VALIDATED_LOCAL_IMAGE_ID"],
            "${{ steps.local.outputs.validated_local_image_id }}",
        )
        run = pushed["run"]
        self.assertIn(
            'if [[ "$TAGGED_LOCAL_IMAGE_ID" != "$VALIDATED_LOCAL_IMAGE_ID" ]]',
            run,
        )
        self.assertIn("REGISTRY_REPO_DIGESTS=", run)
        self.assertIn("worker_publication.py verify-pushed", run)


class AcceptanceWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ACCEPTANCE_WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.load(self.source, Loader=yaml.BaseLoader)
        self.steps = self.workflow["jobs"]["accept"]["steps"]
        self.by_name = {step["name"]: step for step in self.steps}

    def assert_zero_build_push_or_login(self, source: str) -> None:
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["accept"]["steps"]
        uses = [str(step.get("uses", "")) for step in steps]
        shell = "\n".join(str(step.get("run", "")) for step in steps)
        self.assertFalse(any(item.startswith("docker/build-push-action@") for item in uses))
        self.assertFalse(any(item.startswith("docker/login-action@") for item in uses))
        self.assertNotRegex(shell, r"(?m)^\s*docker\s+(?:image\s+)?push\b")
        self.assertNotRegex(shell, r"(?m)^\s*docker\s+(?:build|buildx\s+build)\b")

    def assert_acceptance_order(self, source: str) -> None:
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        names = [step["name"] for step in workflow["jobs"]["accept"]["steps"]]
        ordered = (
            "Validate acceptance request",
            "Fetch and reauthorize exact candidate branch head",
            "Checkout detached candidate source",
            "Verify detached candidate source",
            "Validate candidate Dockerfile base policy",
            "Fresh anonymous exact-digest pull and candidate validation",
            "Create final accepted publication record",
            "Upload final accepted publication record",
        )
        self.assertEqual(
            [names.index(name) for name in ordered],
            sorted(names.index(name) for name in ordered),
        )

    def assert_trusted_acceptance_commands(self, source: str) -> None:
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["accept"]["steps"]
        shell = "\n".join(str(step.get("run", "")) for step in steps)
        self.assertNotIn("candidate/.github/scripts/", shell)
        self.assertNotIn("candidate/scripts/check-worker-oci-image.py", shell)
        self.assertIn(
            "trusted/.github/scripts/worker_publication.py validate-acceptance",
            " ".join(shell.split()),
        )
        self.assertIn("trusted/.github/scripts/anonymous_worker_pull.py", shell)
        self.assertIn("trusted/scripts/check-worker-oci-image.py", shell)

    def test_manual_main_only_inputs_and_minimal_permissions(self) -> None:
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"candidate_ref", "candidate_sha", "image_digest"})
        self.assertTrue(all(value["required"] == "true" for value in inputs.values()))
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        condition = self.workflow["jobs"]["accept"]["if"]
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertIn("github.repository == 'bebet0o/Orchestra'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)

    def test_actions_are_immutable_and_acceptance_has_no_build_push_or_login(self) -> None:
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
        }
        found = dict(re.findall(
            r"(?m)^\s*uses:\s*([^@\s]+)@([0-9a-f]{40})(?:\s|$)",
            self.source,
        ))
        self.assertEqual(found, expected)
        self.assert_zero_build_push_or_login(self.source)

    def test_candidate_is_reauthorized_and_authority_stays_trusted(self) -> None:
        request = self.by_name["Validate acceptance request"]
        self.assertEqual(
            request["env"],
            {
                "REQUESTED_CANDIDATE_REF": "${{ inputs.candidate_ref }}",
                "REQUESTED_CANDIDATE_SHA": "${{ inputs.candidate_sha }}",
                "REQUESTED_IMAGE_DIGEST": "${{ inputs.image_digest }}",
            },
        )
        fetch = self.by_name["Fetch and reauthorize exact candidate branch head"]["run"]
        self.assertIn("git -C trusted fetch", fetch)
        self.assertIn('"$REQUESTED_CANDIDATE_REF:', fetch)
        self.assertIn("worker_publication.py verify-fetched", fetch)
        checkout = self.by_name["Checkout detached candidate source"]["with"]
        self.assertEqual(checkout["path"], "candidate")
        self.assertEqual(checkout["persist-credentials"], "false")
        self.assertIn(
            "verify-checkout --checkout-path candidate",
            " ".join(self.by_name["Verify detached candidate source"]["run"].split()),
        )
        self.assert_trusted_acceptance_commands(self.source)

    def test_anonymous_exact_digest_precedes_final_record(self) -> None:
        self.assert_acceptance_order(self.source)
        anonymous = self.by_name[
            "Fresh anonymous exact-digest pull and candidate validation"
        ]
        self.assertEqual(
            anonymous["env"]["IMMUTABLE_IMAGE_REFERENCE"],
            "${{ steps.request.outputs.image_reference }}",
        )
        self.assertIn('--image "$IMMUTABLE_IMAGE_REFERENCE"', anonymous["run"])
        self.assertIn('--expected-revision "$CANDIDATE_SHA"', anonymous["run"])
        record = self.by_name["Create final accepted publication record"]
        self.assertIn("record-accepted", record["run"])
        self.assertEqual(record["env"]["OCI_DIGEST"], "${{ steps.request.outputs.image_digest }}")
        self.assertEqual(record["env"]["GHCR_PACKAGE_PUBLIC"], "${{ steps.anonymous.outputs.ghcr_package_public }}")
        self.assertEqual(record["env"]["ANONYMOUS_DIGEST_PULL"], "${{ steps.anonymous.outputs.anonymous_digest_pull }}")
        self.assertEqual(record["env"]["ANONYMOUS_PULL_FRESH_DAEMON"], "${{ steps.anonymous.outputs.anonymous_pull_fresh_daemon }}")

    def test_acceptance_regression_mutations_are_rejected(self) -> None:
        mutations = []
        mutations.append((self.assert_zero_build_push_or_login, self.source.replace(
            "uses: actions/checkout@", "uses: docker/build-push-action@", 1
        )))
        mutations.append((self.assert_zero_build_push_or_login, self.source.replace(
            "python3 trusted/.github/scripts/anonymous_worker_pull.py",
            "docker image push ghcr.io/bebet0o/orchestra-worker:mutated\n          python3 trusted/.github/scripts/anonymous_worker_pull.py",
            1,
        )))
        mutations.append((self.assert_zero_build_push_or_login, self.source.replace(
            "uses: actions/upload-artifact@", "uses: docker/login-action@", 1
        )))
        swapped = self.source.replace(
            "Fresh anonymous exact-digest pull and candidate validation",
            "TEMPORARY ACCEPTANCE NAME",
            1,
        ).replace(
            "Create final accepted publication record",
            "Fresh anonymous exact-digest pull and candidate validation",
            1,
        ).replace(
            "TEMPORARY ACCEPTANCE NAME",
            "Create final accepted publication record",
            1,
        )
        mutations.append((self.assert_acceptance_order, swapped))
        mutations.append((self.assert_trusted_acceptance_commands, self.source.replace(
            "python3 trusted/.github/scripts/anonymous_worker_pull.py",
            "docker image pull",
            1,
        )))
        mutations.append((self.assert_trusted_acceptance_commands, self.source.replace(
            "validate-acceptance", "validate-request", 1
        )))
        mutations.append((self.assert_trusted_acceptance_commands, self.source.replace(
            "trusted/.github/scripts/worker_publication.py",
            "candidate/.github/scripts/worker_publication.py",
            1,
        )))
        for assertion, mutated in mutations:
            with self.subTest(assertion=assertion.__name__), self.assertRaises(AssertionError):
                assertion(mutated)


class PublicationHelperCLITest(unittest.TestCase):
    def run_helper(
        self,
        command: str,
        *arguments: str,
        **values: str,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory(prefix="orchestra-bootstrap-cli-") as directory:
            output = Path(directory) / "github-output"
            output.touch()
            completed = subprocess.run(
                [sys.executable, str(HELPER), command, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "GITHUB_OUTPUT": str(output),
                    **values,
                },
            )
            return completed, output.read_text(encoding="utf-8")

    def test_candidate_ref_and_sha_syntax(self) -> None:
        valid_ref = "refs/heads/milestone/3a-distribution-foundation"
        self.assertEqual(PUBLICATION.validate_candidate_ref(valid_ref), valid_ref)
        self.assertEqual(PUBLICATION.validate_candidate_sha(SHA), SHA)
        for value in (
            "main", "refs/tags/v1", "refs/pull/1/head", "https://evil.invalid/x",
            "refs/heads/-x", "refs/heads/a b", "refs/heads/a..b",
            "refs/heads/a//b", "refs/heads/a.lock", "refs/heads/a;echo",
            "refs/heads/$(id)", "-upload-pack=evil", "refs/heads/a\nmalicious",
        ):
            with self.subTest(value=value), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.validate_candidate_ref(value)
        for value in ("A" * 40, "a" * 39, "a" * 41, "$(id)", "a" * 39 + "\n"):
            with self.subTest(value=value), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.validate_candidate_sha(value)

    def test_validate_request_cli_output_is_atomic(self) -> None:
        ref = "refs/heads/milestone/3a-distribution-foundation"
        completed, output = self.run_helper(
            "validate-request",
            REQUESTED_CANDIDATE_REF=ref,
            REQUESTED_CANDIDATE_SHA=SHA,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output, f"candidate_ref={ref}\ncandidate_sha={SHA}\n")
        completed, output = self.run_helper(
            "validate-request",
            REQUESTED_CANDIDATE_REF="refs/heads/a;echo",
            REQUESTED_CANDIDATE_SHA=SHA,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(output, "")

    def test_validate_acceptance_digest_is_strict_and_constructs_reference(self) -> None:
        ref = "refs/heads/milestone/3a-distribution-foundation"
        completed, output = self.run_helper(
            "validate-acceptance",
            REQUESTED_CANDIDATE_REF=ref,
            REQUESTED_CANDIDATE_SHA=SHA,
            REQUESTED_IMAGE_DIGEST=DIGEST,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            output,
            f"candidate_ref={ref}\ncandidate_sha={SHA}\n"
            f"image_digest={DIGEST}\nimage_reference={REFERENCE}\n",
        )
        for invalid in (
            "sha256:short",
            "sha256:" + "B" * 64,
            "sha512:" + "b" * 64,
            REFERENCE,
            "candidate-tag",
            " " + DIGEST,
            DIGEST + "\n",
            DIGEST + " " + DIGEST,
        ):
            completed, output = self.run_helper(
                "validate-acceptance",
                REQUESTED_CANDIDATE_REF=ref,
                REQUESTED_CANDIDATE_SHA=SHA,
                REQUESTED_IMAGE_DIGEST=invalid,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn("image_digest=", output)

    def test_fetched_cli_mismatches_emit_nothing(self) -> None:
        completed, output = self.run_helper(
            "verify-fetched",
            REQUESTED_CANDIDATE_SHA=SHA,
            FETCHED_CANDIDATE_SHA="c" * 40,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(output, "")
        completed, output = self.run_helper(
            "verify-fetched",
            REQUESTED_CANDIDATE_SHA=SHA,
            FETCHED_CANDIDATE_SHA=SHA,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output, f"candidate_sha={SHA}\n")

    def test_checkout_cli_rejects_branch_and_accepts_detached_head(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestra-bootstrap-git-") as directory:
            repository = Path(directory) / "candidate"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "-c", "user.name=Audit",
                 "-c", "user.email=audit@example.invalid", "commit", "-q",
                 "--allow-empty", "-m", "candidate"],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            completed, output = self.run_helper(
                "verify-checkout", "--checkout-path", str(repository),
                REQUESTED_CANDIDATE_SHA=head,
                FETCHED_CANDIDATE_SHA=head,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(output, "")
            subprocess.run(
                ["git", "-C", str(repository), "checkout", "-q", "--detach", head],
                check=True,
            )
            completed, output = self.run_helper(
                "verify-checkout", "--checkout-path", str(repository),
                REQUESTED_CANDIDATE_SHA=head,
                FETCHED_CANDIDATE_SHA=head,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output, f"candidate_sha={head}\n")

    def test_publication_record_binds_source_and_digest(self) -> None:
        record = PUBLICATION.build_publication_record(
            publication_state="provisional",
            candidate_ref="refs/heads/milestone/3a-distribution-foundation",
            requested_candidate_sha=SHA,
            fetched_candidate_sha=SHA,
            checked_out_sha=SHA,
            revision_label=SHA,
            repository="ghcr.io/bebet0o/orchestra-worker",
            platform="linux/amd64",
            registry_digest=DIGEST,
            workflow_run_id="123",
        )
        self.assertEqual(record["source_commit"], SHA)
        self.assertEqual(record["publication_state"], "provisional")
        self.assertEqual(record["anonymous_pull"], "not-yet-verified")
        self.assertEqual(record["oci_digest"], DIGEST)
        self.assertEqual(record["image_reference"], REFERENCE)
        accepted = PUBLICATION.build_publication_record(
            publication_state="accepted",
            candidate_ref="refs/heads/milestone/3a-distribution-foundation",
            requested_candidate_sha=SHA,
            fetched_candidate_sha=SHA,
            checked_out_sha=SHA,
            revision_label=SHA,
            repository="ghcr.io/bebet0o/orchestra-worker",
            platform="linux/amd64",
            registry_digest=DIGEST,
            workflow_run_id="124",
            ghcr_package_public="YES",
            anonymous_digest_pull="PASS",
            anonymous_pull_fresh_daemon="YES",
        )
        self.assertEqual(accepted["publication_state"], "accepted")
        self.assertEqual(accepted["GHCR_PACKAGE_PUBLIC"], "YES")
        self.assertEqual(accepted["ANONYMOUS_DIGEST_PULL"], "PASS")
        self.assertEqual(accepted["ANONYMOUS_PULL_FRESH_DAEMON"], "YES")
        with self.assertRaises(PUBLICATION.PublicationContractError):
            PUBLICATION.build_publication_record(
                publication_state="accepted",
                candidate_ref="refs/heads/milestone/3a-distribution-foundation",
                requested_candidate_sha=SHA,
                fetched_candidate_sha=SHA,
                checked_out_sha=SHA,
                revision_label=SHA,
                repository="ghcr.io/bebet0o/orchestra-worker",
                platform="linux/amd64",
                registry_digest=DIGEST,
                workflow_run_id="125",
            )
        for field in (
            "requested_candidate_sha", "fetched_candidate_sha",
            "checked_out_sha", "revision_label",
        ):
            identities = {
                "requested_candidate_sha": SHA,
                "fetched_candidate_sha": SHA,
                "checked_out_sha": SHA,
                "revision_label": SHA,
            }
            identities[field] = "c" * 40
            with self.subTest(field=field), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.build_publication_record(
                    publication_state="provisional",
                    candidate_ref="refs/heads/milestone/3a-distribution-foundation",
                    **identities,
                    repository="ghcr.io/bebet0o/orchestra-worker",
                    platform="linux/amd64",
                    registry_digest=DIGEST,
                    workflow_run_id="123",
                )
        for invalid_digest in (
            "", "arbitrary-text", "sha256:short", "sha256:" + "A" * 64,
            "sha512:" + "b" * 64, " " + DIGEST, DIGEST + "\n",
            DIGEST + " " + DIGEST, "candidate-tag", SHA,
        ):
            with self.subTest(digest=invalid_digest), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.build_publication_record(
                    publication_state="provisional",
                    candidate_ref="refs/heads/milestone/3a-distribution-foundation",
                    requested_candidate_sha=SHA,
                    fetched_candidate_sha=SHA,
                    checked_out_sha=SHA,
                    revision_label=SHA,
                    repository="ghcr.io/bebet0o/orchestra-worker",
                    platform="linux/amd64",
                    registry_digest=invalid_digest,
                    workflow_run_id="123",
                )

    def test_push_digest_and_local_registry_binding(self) -> None:
        local_id = "sha256:" + "c" * 64
        tag = "ghcr.io/bebet0o/orchestra-worker:candidate-" + SHA
        with tempfile.TemporaryDirectory(prefix="orchestra-push-output-") as directory:
            output = Path(directory) / "push.log"
            output.write_text(f"digest: {DIGEST} size: 1234\n", encoding="utf-8")
            self.assertEqual(PUBLICATION.parse_push_registry_digest(output), DIGEST)
            output.write_text(
                f"digest:\t{DIGEST}\tsize:\t1234\t\n",
                encoding="utf-8",
            )
            self.assertEqual(PUBLICATION.parse_push_registry_digest(output), DIGEST)
            verified = PUBLICATION.verify_pushed_image_binding(
                requested_candidate_sha=SHA,
                validated_local_image_id=local_id,
                registry_tag_image_id=local_id,
                repository="ghcr.io/bebet0o/orchestra-worker",
                registry_tag=tag,
                registry_digest=DIGEST,
                repo_digests_json=json.dumps([REFERENCE]),
            )
            self.assertEqual(verified, DIGEST)
            for content in (
                "arbitrary-text\n", "digest: sha256:short size: 1\n",
                f"digest: {DIGEST} size: 1\ndigest: {DIGEST} size: 1\n",
                f"digest: {DIGEST}\nsize: 1\n",
                f"digest:\n{DIGEST} size: 1\n",
                f"digest: {DIGEST} size:\n1\n",
                f"digest: {DIGEST}\r\nsize: 1\r\n",
                f"digest:\r{DIGEST} size: 1\r",
                f"digest: {DIGEST} size:\r1\r",
                f"digest:\v{DIGEST} size: 1\n",
                f"digest: {DIGEST}\fsize: 1\n",
                f"digest: {DIGEST} size: 1 trailing\n",
            ):
                output.write_text(content, encoding="utf-8")
                with self.assertRaises(PUBLICATION.PublicationContractError):
                    PUBLICATION.parse_push_registry_digest(output)
        for field, value in (
            ("registry_tag_image_id", "sha256:" + "d" * 64),
            ("registry_tag", "ghcr.io/bebet0o/orchestra-worker:candidate-wrong"),
            ("repo_digests_json", json.dumps([])),
        ):
            values = {
                "requested_candidate_sha": SHA,
                "validated_local_image_id": local_id,
                "registry_tag_image_id": local_id,
                "repository": "ghcr.io/bebet0o/orchestra-worker",
                "registry_tag": tag,
                "registry_digest": DIGEST,
                "repo_digests_json": json.dumps([REFERENCE]),
            }
            values[field] = value
            with self.subTest(field=field), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.verify_pushed_image_binding(**values)


class TrustedCheckerBoundaryTest(unittest.TestCase):
    def test_checker_is_bootstrap_runnable_and_uses_trusted_parser(self) -> None:
        parsed = CHECKER_MODULE.parse_immutable_oci_reference(REFERENCE)
        self.assertEqual(parsed.image_reference, REFERENCE)
        inspected = {
            "Config": {
                "Labels": {
                    "org.opencontainers.image.source": CHECKER_MODULE.EXPECTED_SOURCE,
                    "org.opencontainers.image.base.digest": (
                        CHECKER_MODULE.EXPECTED_BASE_DIGEST
                    ),
                    "org.opencontainers.image.revision": SHA,
                    "org.opencontainers.image.version": "candidate-" + SHA,
                },
                "Cmd": ["sleep", "infinity"],
                "Entrypoint": None,
                "Env": ["PATH=/usr/bin", "HOME=/home/orchestra"],
                "Volumes": None,
            }
        }
        CHECKER_MODULE.validate_metadata(inspected, expected_revision=SHA)
        inspected["Config"]["Labels"]["org.opencontainers.image.version"] = "latest"
        with self.assertRaises(CHECKER_MODULE.WorkerImageContractError):
            CHECKER_MODULE.validate_metadata(inspected, expected_revision=SHA)

    def test_dockerfile_base_policy_is_authoritative_not_candidate_label(self) -> None:
        accepted = (
            "# trusted candidate\nFROM "
            + CHECKER_MODULE.EXPECTED_BASE_REFERENCE
            + "\nRUN true\n"
        )
        rejected = (
            "FROM evil.example/worker@sha256:" + "c" * 64 + "\n"
            "LABEL org.opencontainers.image.base.digest=\""
            + CHECKER_MODULE.EXPECTED_BASE_DIGEST + "\"\n"
        )
        with tempfile.TemporaryDirectory(prefix="orchestra-dockerfile-policy-") as directory:
            dockerfile = Path(directory) / "Dockerfile"
            dockerfile.write_text(accepted, encoding="utf-8")
            CHECKER_MODULE.validate_dockerfile_base_policy(dockerfile)
            for malicious in (
                rejected,
                "ARG BASE=" + CHECKER_MODULE.EXPECTED_BASE_REFERENCE + "\nFROM $BASE\n",
                accepted + "FROM scratch\n",
                "FROM python:latest\n",
            ):
                dockerfile.write_text(malicious, encoding="utf-8")
                with self.assertRaises(CHECKER_MODULE.WorkerImageContractError):
                    CHECKER_MODULE.validate_dockerfile_base_policy(dockerfile)

    def test_runtime_contract_invocation_is_testable_without_candidate_source(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(CHECKER_MODULE, "docker", return_value=completed) as docker:
            CHECKER_MODULE.validate_arbitrary_numeric_identity("candidate-image")
        self.assertEqual(docker.call_count, 2)
        self.assertIn("12345:23456", docker.call_args_list[0].args)
        self.assertIn("--read-only", docker.call_args_list[1].args)


class FreshDaemonHarnessTest(unittest.TestCase):
    def test_fresh_dind_exact_pull_and_cleanup_control_flow(self) -> None:
        expected_dind = (
            "docker@sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515"
        )
        self.assertEqual(ANONYMOUS.DIND_IMAGE, expected_dind)
        self.assertEqual(ANONYMOUS.EXPECTED_SOURCE, CHECKER_MODULE.EXPECTED_SOURCE)
        self.assertEqual(
            ANONYMOUS.EXPECTED_BASE_DIGEST,
            CHECKER_MODULE.EXPECTED_BASE_DIGEST,
        )
        calls: list[list[str]] = []
        environments: list[dict[str, str]] = []
        inspections = 0

        def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal inspections
            calls.append(arguments)
            environments.append(kwargs["env"])
            if arguments[-3:-1] == ["image", "inspect"] and "--format" not in arguments:
                inspections += 1
                if inspections == 1:
                    return subprocess.CompletedProcess(arguments, 1, "", "")
                payload = [{
                    "Id": "sha256:" + "c" * 64,
                    "RepoDigests": [REFERENCE],
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": {"Labels": {
                        "org.opencontainers.image.source": ANONYMOUS.EXPECTED_SOURCE,
                        "org.opencontainers.image.base.digest": ANONYMOUS.EXPECTED_BASE_DIGEST,
                        "org.opencontainers.image.revision": SHA,
                        "org.opencontainers.image.version": "candidate-" + SHA,
                    }},
                }]
                return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        ANONYMOUS.prove_anonymous_pull(
            REFERENCE,
            SHA,
            runner=run,
            sleeper=lambda _s: None,
        )
        self.assertEqual(calls[0][:3], ["docker", "run", "--detach"])
        self.assertIn("--privileged", calls[0])
        self.assertIn(expected_dind, calls[0])
        self.assertNotIn("--volume", calls[0])
        pull = next(command for command in calls if "pull" in command)
        self.assertEqual(pull[-1], REFERENCE)
        self.assertIn("DOCKER_CONFIG=/nonexistent/orchestra-empty-docker-config", pull)
        for environment in environments:
            self.assertEqual(
                environment["DOCKER_CONFIG"],
                "/nonexistent/orchestra-empty-docker-config",
            )
            self.assertNotIn("HOME", environment)
        self.assertEqual(calls[-1][:3], ["docker", "rm", "--force"])

    def test_failure_still_cleans_up(self) -> None:
        calls: list[list[str]] = []

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if arguments[:3] == ["docker", "rm", "--force"]:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(arguments, 1, "", "")

        with self.assertRaises(ANONYMOUS.AnonymousPullError):
            ANONYMOUS.prove_anonymous_pull(
                REFERENCE,
                SHA,
                runner=run,
                sleeper=lambda _s: None,
            )
        self.assertEqual(calls[-1][:3], ["docker", "rm", "--force"])


if __name__ == "__main__":
    unittest.main()
