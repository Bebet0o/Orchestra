from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "images/orchestra-worker.Dockerfile"
DOCKERIGNORE = ROOT / "images/orchestra-worker.Dockerfile.dockerignore"
WORKFLOW = ROOT / ".github/workflows/publish-worker.yml"
LOCK = ROOT / "config/environments/default-worker.toml"
IMAGE_CONTRACT = ROOT / "scripts/check-worker-oci-image.py"
EXPECTED_BASE_DIGEST = (
    "sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
)


def load_image_contract() -> object:
    spec = importlib.util.spec_from_file_location(
        "worker_image_contract",
        IMAGE_CONTRACT,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load worker image contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WorkerDockerfileContractTest(unittest.TestCase):
    def test_new_source_keeps_pinned_base_tools_and_dynamic_identity(self) -> None:
        source = DOCKERFILE.read_text(encoding="utf-8")
        from_lines = [
            line for line in source.splitlines() if line.startswith("FROM ")
        ]
        self.assertEqual(
            from_lines,
            ["FROM python@" + EXPECTED_BASE_DIGEST],
        )
        for executable in (
            "bash",
            "ca-certificates",
            "coreutils",
            "findutils",
            "git",
            "grep",
            "procps",
            "sed",
        ):
            with self.subTest(executable=executable):
                self.assertIn(executable, source)
        self.assertIn("chmod 1777 /home/orchestra /workspace", source)
        self.assertIn("ENV HOME=/home/orchestra", source)
        self.assertNotIn("chown -R 1000:1000", source)
        self.assertNotRegex(source, r"(?m)^(?:COPY|ADD)\s")
        self.assertNotIn("docker.sock", source)
        self.assertIn('CMD ["sleep", "infinity"]', source)

    def test_provenance_is_source_commit_bound_but_not_authority(self) -> None:
        source = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("org.opencontainers.image.source", source)
        self.assertIn("org.opencontainers.image.revision", source)
        self.assertIn("org.opencontainers.image.version", source)
        self.assertIn("org.opencontainers.image.base.digest", source)
        self.assertIn("ARG OCI_REVISION", source)
        self.assertIn('test -n "${OCI_REVISION}"', source)
        self.assertNotIn("config/environments", source)

    def test_distribution_lock_is_excluded_from_build_inputs(self) -> None:
        rules = [
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(rules, ["**", "!images/orchestra-worker.Dockerfile"])
        self.assertNotIn("default-worker.toml", DOCKERFILE.read_text())

    def test_inherited_source_remains_as_an_explicit_transition(self) -> None:
        self.assertTrue((ROOT / "images/worker-sandbox.Dockerfile").is_file())
        self.assertIn(
            "inherited image stays",
            DOCKERFILE.read_text(encoding="utf-8"),
        )


class PublicationWorkflowContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.load(self.source, Loader=yaml.BaseLoader)

    def assert_single_build_contract(self, source: str) -> None:
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["publish"]["steps"]
        builds = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
        ]
        self.assertEqual(len(builds), 1)
        build = builds[0]["with"]
        self.assertEqual(build["context"], "candidate")
        self.assertEqual(
            build["file"], "candidate/images/orchestra-worker.Dockerfile"
        )
        self.assertEqual(build["platforms"], "${{ env.PLATFORM }}")
        self.assertEqual(build["load"], "true")
        self.assertEqual(build["push"], "false")
        self.assertNotIn("Push candidate", source)

    def assert_validated_artifact_flow(self, source: str) -> None:
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["publish"]["steps"]
        by_name = {step["name"]: step for step in steps}
        ordered = [
            "Resolve exact local image identity",
            "Validate local contract candidate with trusted checker",
            "Authenticate to GHCR",
            "Tag and push exact validated local image",
            "Create trusted machine-readable publication record",
        ]
        positions = [
            next(index for index, step in enumerate(steps) if step["name"] == name)
            for name in ordered
        ]
        self.assertEqual(positions, sorted(positions))
        local = by_name["Resolve exact local image identity"]["run"]
        self.assertIn("LOCAL_IMAGE_ID=", local)
        self.assertIn("validate-local-image", local)
        checker = by_name["Validate local contract candidate with trusted checker"]
        self.assertIn('--image "$VALIDATED_LOCAL_IMAGE_ID"', checker["run"])
        push = by_name["Tag and push exact validated local image"]["run"]
        self.assertIn(
            'docker image tag "$VALIDATED_LOCAL_IMAGE_ID" "$REGISTRY_TAG"', push
        )
        self.assertIn('"$TAGGED_LOCAL_IMAGE_ID" != "$VALIDATED_LOCAL_IMAGE_ID"', push)
        self.assertIn('docker image push "$REGISTRY_TAG"', push)
        self.assertIn("PUSH_OUTPUT_PATH", push)
        self.assertIn("REGISTRY_REPO_DIGESTS", push)
        self.assertIn("verify-pushed", push)

    def assert_trusted_helper_contract(self, source: str) -> None:
        self.assertNotIn("candidate/.github/scripts/worker_publication.py", source)
        self.assertNotIn("candidate/scripts/check-worker-oci-image.py", source)
        self.assertIn(
            "trusted/.github/scripts/worker_publication.py verify-checkout",
            source,
        )
        self.assertIn("trusted/scripts/check-worker-oci-image.py", source)

    def test_publication_is_manual_main_repository_only(self) -> None:
        self.assertEqual(
            set(self.workflow["on"]),
            {"workflow_dispatch"},
        )
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"candidate_ref", "candidate_sha"})
        self.assertEqual(inputs["candidate_ref"]["required"], "true")
        self.assertEqual(inputs["candidate_sha"]["required"], "true")
        condition = self.workflow["jobs"]["publish"]["if"]
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertIn("github.repository == 'bebet0o/Orchestra'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)
        self.assertNotIn("pull_request", self.source)

    def test_permissions_are_minimal_and_explicit(self) -> None:
        self.assertEqual(
            self.workflow["permissions"],
            {"contents": "read", "packages": "write"},
        )
        self.assertNotIn("id-token", self.workflow["permissions"])
        self.assertNotIn("attestations", self.workflow["permissions"])
        self.assertEqual(self.source.count("provenance: false"), 1)

    def test_all_publication_actions_are_known_immutable_pins(self) -> None:
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "docker/setup-buildx-action": (
                "e468171a9de216ec08956ac3ada2f0791b6bd435"
            ),
            "docker/build-push-action": (
                "263435318d21b8e681c14492fe198d362a7d2c83"
            ),
            "docker/login-action": (
                "5e57cd118135c172c3672efd75eb46360885c0ef"
            ),
            "actions/upload-artifact": (
                "ea165f8d65b6e75b540449e92b4886f43607fa02"
            ),
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
        self.assertNotRegex(self.source, r"(?m)^\s*uses:\s*[^\s]+@v\d")

    def test_candidate_is_amd64_tested_then_pushed_without_latest(self) -> None:
        self.assertIn("PLATFORM: linux/amd64", self.source)
        self.assert_single_build_contract(self.source)
        self.assert_validated_artifact_flow(self.source)
        self.assertIn(
            "candidate-${{ steps.source.outputs.candidate_sha }}",
            self.source,
        )
        self.assertNotIn("OCI_REVISION=${{ github.sha }}", self.source)
        self.assertNotRegex(self.source.lower(), r"(?:^|[:/])latest(?:\s|$)")

    def test_trusted_and_candidate_checkouts_are_separate_and_exact(self) -> None:
        steps = self.workflow["jobs"]["publish"]["steps"]
        by_name = {step["name"]: step for step in steps}
        trusted = by_name["Checkout trusted publication foundation"]
        candidate = by_name["Checkout detached candidate source"]
        self.assertEqual(trusted["with"]["path"], "trusted")
        self.assertEqual(trusted["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(self.source.count("${{ github.sha }}"), 1)
        self.assertEqual(candidate["with"]["path"], "candidate")
        self.assertEqual(
            candidate["with"]["ref"],
            "${{ steps.fetched.outputs.candidate_sha }}",
        )
        self.assertEqual(trusted["with"]["persist-credentials"], "false")
        self.assertEqual(candidate["with"]["persist-credentials"], "false")
        fetch = by_name["Fetch and authorize exact candidate branch head"]
        self.assertIn(
            '"$REQUESTED_CANDIDATE_REF:refs/remotes/orchestra-publication/candidate"',
            fetch["run"],
        )
        self.assertIn("verify-fetched", fetch["run"])
        checkout_verification = by_name["Verify detached candidate source"]["run"]
        self.assertIn("trusted/.github/scripts/worker_publication.py", checkout_verification)
        self.assertIn("verify-checkout --checkout-path candidate", checkout_verification)

    def test_shell_never_directly_interpolates_workflow_inputs_or_contexts(self) -> None:
        steps = self.workflow["jobs"]["publish"]["steps"]
        shell = "\n".join(step.get("run", "") for step in steps)
        self.assertNotIn("${{ inputs.", shell)
        self.assertNotIn("${{ github.", shell)
        request = next(step for step in steps if step.get("id") == "request")
        self.assertEqual(
            request["env"],
            {
                "REQUESTED_CANDIDATE_REF": "${{ inputs.candidate_ref }}",
                "REQUESTED_CANDIDATE_SHA": "${{ inputs.candidate_sha }}",
            },
        )

    def test_every_security_critical_command_uses_its_trusted_path(self) -> None:
        steps = self.workflow["jobs"]["publish"]["steps"]
        by_name = {step["name"]: step for step in steps}
        expected = {
            "Validate candidate request": (
                "trusted/.github/scripts/worker_publication.py",
                "validate-request",
            ),
            "Fetch and authorize exact candidate branch head": (
                "trusted/.github/scripts/worker_publication.py",
                "verify-fetched",
            ),
            "Verify detached candidate source": (
                "trusted/.github/scripts/worker_publication.py",
                "verify-checkout",
            ),
            "Create trusted machine-readable publication record": (
                "trusted/.github/scripts/worker_publication.py",
                "record",
            ),
            "Validate local contract candidate with trusted checker": (
                "trusted/scripts/check-worker-oci-image.py",
                None,
            ),
        }
        for step_name, expected_invocation in expected.items():
            with self.subTest(step=step_name):
                run = by_name[step_name]["run"]
                invocations = re.findall(
                    r"(?m)^\s*python3\s+([^\s\\]+)(?:\s+([a-z-]+))?",
                    run,
                )
                normalized = [
                    (path, command or None) for path, command in invocations
                ]
                self.assertEqual(normalized, [expected_invocation])
                self.assertNotIn("candidate/.github/scripts/", run)
                self.assertNotIn("candidate/scripts/check-worker-oci-image.py", run)

    def test_registry_digest_and_source_dataflow_are_semantically_bound(self) -> None:
        steps = self.workflow["jobs"]["publish"]["steps"]
        by_name = {step["name"]: step for step in steps}
        source = "${{ steps.source.outputs.candidate_sha }}"
        local = by_name["Build local contract candidate"]["with"]
        self.assertEqual(local["context"], "candidate")
        self.assertEqual(
            local["file"], "candidate/images/orchestra-worker.Dockerfile"
        )
        self.assertIn("OCI_REVISION=" + source, local["build-args"])
        self.assertIn("OCI_VERSION=candidate-" + source, local["build-args"])
        self.assert_validated_artifact_flow(self.source)
        record = by_name["Create trusted machine-readable publication record"]
        self.assertEqual(
            record["env"],
            {
                "REQUESTED_CANDIDATE_SHA": "${{ steps.request.outputs.candidate_sha }}",
                "FETCHED_CANDIDATE_SHA": "${{ steps.fetched.outputs.candidate_sha }}",
                "CHECKED_OUT_SHA": source,
                "REVISION_LABEL": source,
                "OCI_DIGEST": "${{ steps.pushed.outputs.registry_digest }}",
                "WORKFLOW_RUN": "${{ github.run_id }}",
            },
        )
        self.assertEqual(
            record["run"],
            "python3 trusted/.github/scripts/worker_publication.py record",
        )
        self.assertFalse((ROOT / "worker-publication.json").exists())

    def test_certified_flow_mutations_are_rejected(self) -> None:
        second_build = self.source.replace(
            "uses: docker/setup-buildx-action@",
            "uses: docker/build-push-action@",
            1,
        )
        with self.assertRaises(AssertionError):
            self.assert_single_build_contract(second_build)

        push_true = self.source.replace("push: false", "push: true", 1)
        with self.assertRaises(AssertionError):
            self.assert_single_build_contract(push_true)

        substituted_image = self.source.replace(
            'docker image tag "$VALIDATED_LOCAL_IMAGE_ID" "$REGISTRY_TAG"',
            'docker image tag "$LOCAL_CANDIDATE_TAG" "$REGISTRY_TAG"',
            1,
        )
        with self.assertRaises(AssertionError):
            self.assert_validated_artifact_flow(substituted_image)

        candidate_helper = self.source.replace(
            "trusted/.github/scripts/worker_publication.py",
            "candidate/.github/scripts/worker_publication.py",
            1,
        )
        with self.assertRaises(AssertionError):
            self.assert_trusted_helper_contract(candidate_helper)


class WorkerImageExecutableContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_image_contract()

    def test_metadata_contract_requires_provenance_home_and_default_command(
        self,
    ) -> None:
        revision = "a" * 40
        inspected = {
            "Config": {
                "Labels": {
                    "org.opencontainers.image.source": (
                        "https://github.com/bebet0o/Orchestra"
                    ),
                    "org.opencontainers.image.base.digest": EXPECTED_BASE_DIGEST,
                    "org.opencontainers.image.revision": revision,
                    "org.opencontainers.image.version": "candidate-" + revision,
                },
                "Cmd": ["sleep", "infinity"],
                "Entrypoint": None,
                "Env": ["PATH=/usr/bin", "HOME=/home/orchestra"],
                "Volumes": None,
            }
        }
        self.contract.validate_metadata(
            inspected,
            expected_revision=revision,
        )
        for mutation in (
            lambda data: data["Config"]["Labels"].__setitem__(
                "org.opencontainers.image.revision", "wrong"
            ),
            lambda data: data["Config"].__setitem__("Cmd", ["sh"]),
            lambda data: data["Config"].__setitem__("Env", ["PATH=/usr/bin"]),
            lambda data: data["Config"].__setitem__(
                "Volumes", {"/var/run/docker.sock": {}}
            ),
        ):
            candidate = json.loads(json.dumps(inspected))
            mutation(candidate)
            with self.assertRaises(self.contract.WorkerImageContractError):
                self.contract.validate_metadata(
                    candidate,
                    expected_revision=revision,
                )

    def test_runtime_probe_uses_arbitrary_uid_read_only_root_and_no_socket(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            self.contract,
            "docker",
            return_value=completed,
        ) as docker:
            self.contract.validate_arbitrary_numeric_identity("candidate-image")
        arguments = docker.call_args_list[0].args
        self.assertIn("12345:23456", arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn("--network=none", arguments)
        self.assertIn("/home/orchestra:rw,mode=1777", arguments)
        self.assertIn("/workspace:rw,mode=1777", arguments)
        self.assertIn("test ! -S /var/run/docker.sock", arguments[-1])
        reviewer_arguments = docker.call_args_list[1].args
        self.assertIn("--read-only", reviewer_arguments)
        self.assertNotIn("/workspace:rw,mode=1777", reviewer_arguments)
        self.assertIn("reviewer-must-remain-read-only", reviewer_arguments[-1])


class ActivationGateTest(unittest.TestCase):
    def test_default_environment_remains_unpublished_without_fake_identity(
        self,
    ) -> None:
        with LOCK.open("rb") as stream:
            document = tomllib.load(stream)
        self.assertEqual(document["status"], "unpublished")
        self.assertNotIn("image_reference", document)
        self.assertNotIn("oci_digest", document)

    def test_production_worker_and_reviewer_remain_on_legacy_preparation(
        self,
    ) -> None:
        worker = (ROOT / "scripts/hermesops-worker.py").read_text(encoding="utf-8")
        reviewer = (ROOT / "scripts/hermesops-reviewer.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("return prepare_legacy_environment(", worker)
        self.assertNotIn(".materialize(", worker)
        self.assertIn("WORKER.prepare_worker_environment()", reviewer)
        self.assertNotIn(".materialize(", reviewer)

    def test_default_resolver_has_no_legacy_fallback(self) -> None:
        source = (
            ROOT / "scripts/environment_resolution/default.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Legacy", source)
        self.assertNotIn("worker-sandbox.lock.toml", source)

    def test_runtime_never_consumes_latest_or_candidate_discovery_tags(self) -> None:
        paths = (
            ROOT / "scripts/sandbox_backend.py",
            ROOT / "scripts/hermesops-worker.py",
            ROOT / "scripts/hermesops-reviewer.py",
            ROOT / "scripts/agent_runtime/hermes.py",
            LOCK,
        )
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertNotIn(":latest", source.lower())
        self.assertNotIn("candidate-", source.lower())


if __name__ == "__main__":
    unittest.main()
