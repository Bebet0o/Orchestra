from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish-official-images.yml"
ACCEPT_WORKFLOW = ROOT / ".github/workflows/accept-official-images.yml"
SHA = "a" * 40
OTHER_SHA = "c" * 40
APPLICATION_DIGEST = "sha256:" + "b" * 64
RUNTIME_DIGEST = "sha256:" + "d" * 64
WORKER_DIGEST = "sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49"
CANDIDATE_REF = "refs/heads/milestone/0.1-release-distribution"


def load(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PUBLICATION = load("official_image_publication_test", SCRIPTS / "official_image_publication.py")
ANONYMOUS = load("anonymous_official_image_pull_test", SCRIPTS / "anonymous_official_image_pull.py")


def inspection(image_name: str, *, revision: str = SHA, digest: str | None = None) -> str:
    contract = PUBLICATION.IMAGE_CONTRACTS[image_name]
    repository = contract["repository"]
    payload: dict[str, object] = {
        "Id": "sha256:" + ("1" if image_name == "application" else "2") * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "RepoDigests": [] if digest is None else [repository + "@" + digest],
        "Config": {
            "User": contract["user"],
            "Entrypoint": contract["entrypoint"],
            "Labels": {
                "org.opencontainers.image.source": PUBLICATION.SOURCE,
                "org.opencontainers.image.revision": revision,
                "org.opencontainers.image.version": "candidate-" + revision,
                "org.opencontainers.image.title": contract["title"],
            },
        },
    }
    return json.dumps([payload])


class PublicationHelperTest(unittest.TestCase):
    def test_exact_repositories_platform_and_worker_authority(self) -> None:
        self.assertEqual(PUBLICATION.APPLICATION_REPOSITORY, "ghcr.io/bebet0o/orchestra")
        self.assertEqual(PUBLICATION.RUNTIME_REPOSITORY, "ghcr.io/bebet0o/orchestra-runtime")
        self.assertEqual(PUBLICATION.PLATFORM, "linux/amd64")
        self.assertEqual(PUBLICATION.WORKER_DIGEST, WORKER_DIGEST)

    def test_local_image_set_is_same_source_platform_and_distinguishable(self) -> None:
        application_id = PUBLICATION.validate_image_inspection(
            inspection("application"), image_name="application", candidate_sha=SHA
        )
        runtime_id = PUBLICATION.validate_image_inspection(
            inspection("runtime"), image_name="runtime", candidate_sha=SHA
        )
        self.assertNotEqual(application_id, runtime_id)
        for image_name in ("application", "runtime"):
            with self.subTest(image=image_name), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.validate_image_inspection(
                    inspection(image_name, revision=OTHER_SHA),
                    image_name=image_name,
                    candidate_sha=SHA,
                )

    def test_wrong_platform_user_entrypoint_or_title_fails_closed(self) -> None:
        for key, value in (
            ("Architecture", "arm64"),
            ("Os", "windows"),
        ):
            payload = json.loads(inspection("application"))
            payload[0][key] = value
            with self.subTest(key=key), self.assertRaises(PUBLICATION.PublicationContractError):
                PUBLICATION.validate_image_inspection(
                    json.dumps(payload), image_name="application", candidate_sha=SHA
                )
        for key, value in (
            ("User", "root"),
            ("Entrypoint", ["sh"]),
        ):
            payload = json.loads(inspection("application"))
            payload[0]["Config"][key] = value
            with self.subTest(key=key), self.assertRaises(PUBLICATION.PublicationContractError):
                PUBLICATION.validate_image_inspection(
                    json.dumps(payload), image_name="application", candidate_sha=SHA
                )
        payload = json.loads(inspection("runtime"))
        payload[0]["Config"]["Labels"]["org.opencontainers.image.title"] = "Orchestra"
        with self.assertRaises(PUBLICATION.PublicationContractError):
            PUBLICATION.validate_image_inspection(
                json.dumps(payload), image_name="runtime", candidate_sha=SHA
            )

    def test_manifest_binds_complete_set_to_one_source(self) -> None:
        manifest = PUBLICATION.build_release_manifest(
            publication_state="provisional",
            candidate_ref=CANDIDATE_REF,
            requested_candidate_sha=SHA,
            fetched_candidate_sha=SHA,
            checked_out_sha=SHA,
            application_revision=SHA,
            runtime_revision=SHA,
            application_digest=APPLICATION_DIGEST,
            runtime_digest=RUNTIME_DIGEST,
            workflow_run_id="123",
        )
        self.assertEqual(manifest["source_revision"], SHA)
        self.assertEqual(manifest["platform"], "linux/amd64")
        self.assertEqual(manifest["application"]["digest"], APPLICATION_DIGEST)
        self.assertEqual(manifest["runtime"]["digest"], RUNTIME_DIGEST)
        self.assertEqual(manifest["worker"]["digest"], WORKER_DIGEST)
        self.assertEqual(manifest["publication_state"], "provisional")

    def test_mixed_source_or_partial_set_cannot_be_accepted(self) -> None:
        values = dict(
            publication_state="accepted",
            candidate_ref=CANDIDATE_REF,
            requested_candidate_sha=SHA,
            fetched_candidate_sha=SHA,
            checked_out_sha=SHA,
            application_revision=SHA,
            runtime_revision=SHA,
            application_digest=APPLICATION_DIGEST,
            runtime_digest=RUNTIME_DIGEST,
            workflow_run_id="123",
            anonymous_digest_pull="PASS",
            anonymous_pull_fresh_daemon="YES",
        )
        for mutation in (
            {"runtime_revision": OTHER_SHA},
            {"runtime_digest": None},
            {"application_digest": "sha256:" + "B" * 64},
            {"anonymous_digest_pull": "FAIL"},
            {"anonymous_pull_fresh_daemon": "NO"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(
                PUBLICATION.PublicationContractError
            ):
                PUBLICATION.build_release_manifest(**{**values, **mutation})

    def test_invalid_acceptance_set_emits_no_partial_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            output.touch()
            environment = {
                "GITHUB_OUTPUT": str(output),
                "REQUESTED_CANDIDATE_REF": CANDIDATE_REF,
                "REQUESTED_CANDIDATE_SHA": SHA,
                "REQUESTED_APPLICATION_DIGEST": APPLICATION_DIGEST,
                "REQUESTED_RUNTIME_DIGEST": "sha256:" + "D" * 64,
            }
            with self.assertRaises(PUBLICATION.PublicationContractError):
                PUBLICATION.main(["validate-acceptance"], environment)
            self.assertEqual(output.read_text(encoding="utf-8"), "")

    def test_existing_strict_push_parser_is_reused_for_both_repositories(self) -> None:
        for repository, digest in (
            (PUBLICATION.APPLICATION_REPOSITORY, APPLICATION_DIGEST),
            (PUBLICATION.RUNTIME_REPOSITORY, RUNTIME_DIGEST),
        ):
            tag = repository + ":candidate-" + SHA
            with tempfile.NamedTemporaryFile("w", encoding="utf-8") as stream:
                stream.write(f"candidate-{SHA}: digest: {digest} size: 1234\n")
                stream.flush()
                parsed = PUBLICATION.parse_push_registry_digest(
                    Path(stream.name), tag, expected_repository=repository
                )
            self.assertEqual(parsed, digest)

    def test_candidate_dockerfiles_are_pinned_and_host_socket_free(self) -> None:
        PUBLICATION.validate_candidate_dockerfiles(ROOT)
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            (candidate / "images").mkdir()
            (candidate / "images/orchestra.Dockerfile").write_text(
                "FROM python:latest\nFROM docker@sha256:" + "a" * 64 + " AS cli\n"
            )
            (candidate / "images/orchestra-runtime.Dockerfile").write_text(
                "FROM docker@sha256:" + "a" * 64 + "\n"
            )
            with self.assertRaises(PUBLICATION.PublicationContractError):
                PUBLICATION.validate_candidate_dockerfiles(candidate)


class WorkflowContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.publish_source = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        self.accept_source = ACCEPT_WORKFLOW.read_text(encoding="utf-8")
        self.publish = yaml.load(self.publish_source, Loader=yaml.BaseLoader)
        self.accept = yaml.load(self.accept_source, Loader=yaml.BaseLoader)
        self.publish_steps = self.publish["jobs"]["publish"]["steps"]
        self.accept_steps = self.accept["jobs"]["accept"]["steps"]
        self.publish_by_name = {step["name"]: step for step in self.publish_steps}

    def test_manual_main_only_and_serialized_per_candidate(self) -> None:
        for workflow, job in ((self.publish, "publish"), (self.accept, "accept")):
            self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
            condition = workflow["jobs"][job]["if"]
            self.assertIn("github.ref == 'refs/heads/main'", condition)
            self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
            self.assertIn("${{ inputs.candidate_sha }}", workflow["concurrency"]["group"])
        self.assertEqual(self.publish["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(self.accept["permissions"], {"contents": "read"})

    def test_all_actions_are_immutable_commit_pins(self) -> None:
        for source in (self.publish_source, self.accept_source):
            uses = re.findall(r"(?m)^\s*uses:\s*([^\s]+)", source)
            self.assertTrue(uses)
            for action in uses:
                with self.subTest(action=action):
                    self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_trusted_and_candidate_checkouts_are_separate(self) -> None:
        for source, workflow, job in (
            (self.publish_source, self.publish, "publish"),
            (self.accept_source, self.accept, "accept"),
        ):
            steps = {step["name"]: step for step in workflow["jobs"][job]["steps"]}
            self.assertEqual(steps["Checkout trusted publication foundation"]["with"]["path"], "trusted")
            self.assertEqual(steps["Checkout detached candidate source"]["with"]["path"], "candidate")
            self.assertEqual(steps["Checkout trusted publication foundation"]["with"]["persist-credentials"], "false")
            self.assertEqual(steps["Checkout detached candidate source"]["with"]["persist-credentials"], "false")
            self.assertNotIn("candidate/.github/scripts", source)

    def test_each_image_builds_once_from_same_candidate_without_push(self) -> None:
        builds = [
            step for step in self.publish_steps
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
        ]
        self.assertEqual(len(builds), 2)
        self.assertEqual(
            {step["name"] for step in builds},
            {"Build application image exactly once", "Build runtime image exactly once"},
        )
        for step in builds:
            settings = step["with"]
            self.assertEqual(settings["context"], "candidate")
            self.assertEqual(settings["platforms"], "${{ env.PLATFORM }}")
            self.assertEqual(settings["load"], "true")
            self.assertEqual(settings["push"], "false")
            self.assertIn("${{ steps.source.outputs.candidate_sha }}", settings["build-args"])
        shell = "\n".join(str(step.get("run", "")) for step in self.publish_steps)
        self.assertNotRegex(shell, r"(?m)^\s*docker\s+(?:build|buildx\s+build)\b")

    def test_credentials_follow_all_local_validation(self) -> None:
        names = [step["name"] for step in self.publish_steps]
        login = names.index("Authenticate to GHCR")
        for name in (
            "Validate exact local image set",
            "Probe validated application image contents",
            "Probe validated runtime image contents",
        ):
            self.assertLess(names.index(name), login)
        self.assertLess(login, names.index("Tag and push exact validated image set"))
        pre_login = self.publish_steps[:login]
        self.assertNotIn("secrets.GITHUB_TOKEN", "\n".join(json.dumps(step) for step in pre_login))

    def test_application_probe_requires_canonical_schema_24_migration(self) -> None:
        canonical_name = "024_blueprint_apiversion.sql"
        stale_name = "024_blueprint_api_namespace.sql"
        probe = self.publish_by_name["Probe validated application image contents"]["run"]
        required_path = f"/opt/orchestra/app/migrations/{canonical_name}"

        self.assertTrue((ROOT / "migrations" / canonical_name).is_file())
        self.assertFalse((ROOT / "migrations" / stale_name).exists())
        self.assertIn(f"test -f {required_path}", probe)
        self.assertNotIn(stale_name, probe)

        with tempfile.TemporaryDirectory() as directory:
            absent_path = Path(directory) / "migrations" / canonical_name
            result = subprocess.run(
                ["sh", "-ec", f'test -f "{absent_path}"'],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)

    def test_push_reuses_validated_ids_and_records_only_complete_set(self) -> None:
        push = self.publish_by_name["Tag and push exact validated image set"]["run"]
        self.assertEqual(len(re.findall(r"(?m)^\s*docker image push ", push)), 2)
        self.assertNotIn("docker build", push)
        self.assertIn('docker image tag "$VALIDATED_APPLICATION_IMAGE_ID"', push)
        self.assertIn('docker image tag "$VALIDATED_RUNTIME_IMAGE_ID"', push)
        self.assertIn("verify-pushed-set", push)
        names = [step["name"] for step in self.publish_steps]
        self.assertLess(
            names.index("Tag and push exact validated image set"),
            names.index("Create trusted provisional image-set manifest"),
        )

    def test_acceptance_has_no_build_login_or_push_and_pulls_both(self) -> None:
        uses = [str(step.get("uses", "")) for step in self.accept_steps]
        shell = "\n".join(str(step.get("run", "")) for step in self.accept_steps)
        self.assertFalse(any(item.startswith("docker/build-push-action@") for item in uses))
        self.assertFalse(any(item.startswith("docker/login-action@") for item in uses))
        self.assertNotRegex(shell, r"(?m)^\s*docker\s+(?:image\s+)?push\b")
        self.assertIn("anonymous_official_image_pull.py", shell)
        inputs = self.accept["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs),
            {"candidate_ref", "candidate_sha", "application_digest", "runtime_digest"},
        )

    def test_official_flow_never_builds_or_publishes_worker(self) -> None:
        combined = self.publish_source + self.accept_source
        self.assertNotIn("orchestra-worker.Dockerfile", combined)
        self.assertNotRegex(combined, r"docker image push[^\n]*orchestra-worker")


class ManifestAndInstallerContractTest(unittest.TestCase):
    def test_template_has_no_invented_application_or_runtime_digest(self) -> None:
        template = json.loads(
            (ROOT / "config/releases/v0.1.0.manifest.template.json").read_text(encoding="utf-8")
        )
        self.assertEqual(template["publication_state"], "template")
        self.assertIsNone(template["application"]["digest"])
        self.assertIsNone(template["runtime"]["digest"])
        self.assertEqual(template["worker"]["digest"], WORKER_DIGEST)
        schema = json.loads(
            (ROOT / "specs/release-manifest-v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["platform"]["const"], "linux/amd64")

    def test_compose_and_installer_inject_all_immutable_authorities(self) -> None:
        compose = (ROOT / "compose/orchestra.yaml").read_text(encoding="utf-8")
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("${ORCHESTRA_IMAGE:-", compose)
        self.assertIn("${ORCHESTRA_RUNTIME_IMAGE:-", compose)
        self.assertIn("${ORCHESTRA_WORKER_IMAGE:-", compose)
        self.assertIn("orchestra-release-manifest.json", installer)
        self.assertIn('.publication_state == "accepted"', installer)
        self.assertIn('.version == "v0.1.0"', installer)
        self.assertIn("ORCHESTRA_WORKER_IMAGE=%s", installer)
        self.assertNotIn("ORCHESTRA_IMAGE=\"ghcr.io/bebet0o/orchestra:v0.1.0\"", installer)


class AnonymousImageSetContractTest(unittest.TestCase):
    def test_one_empty_daemon_pulls_and_validates_both_exact_digests(self) -> None:
        calls: list[list[str]] = []
        inspect_counts = {"application": 0, "runtime": 0}

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if arguments[-3:-1] == ["image", "inspect"]:
                reference = arguments[-1]
                image_name = "runtime" if "orchestra-runtime@" in reference else "application"
                inspect_counts[image_name] += 1
                if inspect_counts[image_name] == 1:
                    return subprocess.CompletedProcess(arguments, 1, "", "")
                digest = RUNTIME_DIGEST if image_name == "runtime" else APPLICATION_DIGEST
                return subprocess.CompletedProcess(
                    arguments, 0, inspection(image_name, digest=digest), ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        ANONYMOUS.prove_anonymous_image_set(
            APPLICATION_DIGEST, RUNTIME_DIGEST, SHA,
            runner=run, sleeper=lambda _seconds: None,
        )
        self.assertEqual(sum(command.count("pull") for command in calls), 2)
        self.assertEqual(calls[0][:3], ["docker", "run", "--detach"])
        self.assertIn("--privileged", calls[0])
        self.assertNotIn("--volume", calls[0])
        self.assertEqual(calls[-1][:3], ["docker", "rm", "--force"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
