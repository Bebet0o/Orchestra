from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import os
import shutil
import tarfile
import zipfile
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from environment_resolution import (  # noqa: E402
    DEFAULT_ENVIRONMENT_ID,
    DEFAULT_PLATFORM,
    ENVIRONMENT_SCHEMA_VERSION,
    DefaultEnvironmentResolver,
    EnvironmentDistributionDataError,
    EnvironmentNotPublishedError,
    EnvironmentSpec,
    ResolvedEnvironment,
    UnsupportedEnvironmentError,
)


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
IMAGE_REFERENCE = f"ghcr.io/example/orchestra-worker@{DIGEST}"


class EnvironmentContractTest(unittest.TestCase):
    def resolved(self, **overrides: object) -> ResolvedEnvironment:
        values: dict[str, object] = {
            "schema_version": ENVIRONMENT_SCHEMA_VERSION,
            "environment_id": DEFAULT_ENVIRONMENT_ID,
            "image_reference": IMAGE_REFERENCE,
            "oci_digest": DIGEST,
            "platform": DEFAULT_PLATFORM,
            "provenance": "test-publication",
        }
        values.update(overrides)
        return ResolvedEnvironment(**values)

    def test_valid_environment_spec_is_immutable(self) -> None:
        spec = EnvironmentSpec(
            schema_version=ENVIRONMENT_SCHEMA_VERSION,
            environment_id=DEFAULT_ENVIRONMENT_ID,
        )

        self.assertEqual(spec.environment_id, "default-worker")
        with self.assertRaises(FrozenInstanceError):
            spec.environment_id = "changed"  # type: ignore[misc]

    def test_environment_spec_rejects_wrong_schema_and_invalid_identity(self) -> None:
        for schema_version in (0, 2, True, "1"):
            with self.subTest(schema_version=schema_version):
                with self.assertRaises(ValueError):
                    EnvironmentSpec(schema_version, DEFAULT_ENVIRONMENT_ID)

        for environment_id in ("", " ", "Default-Worker", "-worker", "worker-"):
            with self.subTest(environment_id=environment_id):
                with self.assertRaises(ValueError):
                    EnvironmentSpec(ENVIRONMENT_SCHEMA_VERSION, environment_id)

    def test_complete_immutable_oci_reference_is_accepted_and_frozen(self) -> None:
        environment = self.resolved()

        self.assertEqual(environment.image_reference, IMAGE_REFERENCE)
        self.assertEqual(environment.oci_digest, DIGEST)
        with self.assertRaises(FrozenInstanceError):
            environment.platform = "changed"  # type: ignore[misc]

    def test_supported_registry_forms_are_accepted(self) -> None:
        references = (
            f"ghcr.io/bebet0o/orchestra-worker@{DIGEST}",
            f"registry.example.com/team/worker@{DIGEST}",
            f"registry:5000/team/worker@{DIGEST}",
            f"localhost:5000/orchestra-worker@{DIGEST}",
            f"127.0.0.1:5000/team/worker@{DIGEST}",
        )

        for image_reference in references:
            with self.subTest(image_reference=image_reference):
                environment = self.resolved(image_reference=image_reference)
                self.assertEqual(environment.image_reference, image_reference)

    def test_non_authoritative_or_malformed_references_are_rejected(self) -> None:
        invalid_references = (
            "ghcr.io/example/orchestra-worker:latest",
            DIGEST,
            f"orchestra-worker@{DIGEST}",
            f"https://ghcr.io/example/orchestra-worker@{DIGEST}",
            f"ghcr.io/example/orchestra-worker:stable@{DIGEST}",
            "ghcr.io/example/orchestra-worker@sha256:" + "a" * 63,
            "ghcr.io/example/orchestra-worker@sha256:" + "a" * 65,
            "ghcr.io/example/orchestra-worker@sha256:" + "A" * 64,
            "ghcr.io/example/orchestra-worker@sha512:" + "a" * 64,
            f"registry:0/team/worker@{DIGEST}",
            f"registry:65536/team/worker@{DIGEST}",
            f"registry:99999/team/worker@{DIGEST}",
            f"registry:05000/team/worker@{DIGEST}",
            f"bad_host.example/team/worker@{DIGEST}",
            f"-registry.example/team/worker@{DIGEST}",
            f"registry-.example/team/worker@{DIGEST}",
            f"999.0.0.1/team/worker@{DIGEST}",
            f"ghcr.io//worker@{DIGEST}",
            f"ghcr.io/team//worker@{DIGEST}",
            f"ghcr.io/team/./worker@{DIGEST}",
            f"ghcr.io/team/../worker@{DIGEST}",
            f"ghcr.io/team/Worker@{DIGEST}",
        )

        for image_reference in invalid_references:
            with self.subTest(image_reference=image_reference):
                with self.assertRaises(ValueError):
                    self.resolved(image_reference=image_reference)

    def test_reference_and_digest_must_match(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.resolved(oci_digest=OTHER_DIGEST)

        for oci_digest in (
            "sha256:" + "a" * 63,
            "sha256:" + "A" * 64,
            "sha512:" + "a" * 64,
        ):
            with self.subTest(oci_digest=oci_digest):
                with self.assertRaises(ValueError):
                    self.resolved(oci_digest=oci_digest)

    def test_default_platform_and_provenance_are_strict(self) -> None:
        for platform in ("linux/arm64", "linux/AMD64", ""):
            with self.subTest(platform=platform):
                with self.assertRaises(ValueError):
                    self.resolved(platform=platform)

        for provenance in (
            "",
            " ",
            " untrimmed",
            "audit\nforged",
            "audit\rforged",
            "audit\tforged",
            "audit\x00forged",
            "audit\x7fforged",
        ):
            with self.subTest(provenance=provenance):
                with self.assertRaises(ValueError):
                    self.resolved(provenance=provenance)

        accepted = self.resolved(
            provenance="release:2026-08-28/build+verified",
        )
        self.assertEqual(
            accepted.provenance,
            "release:2026-08-28/build+verified",
        )


class DefaultEnvironmentResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.distribution_path = self.directory / "default-worker.toml"
        self.spec = EnvironmentSpec(
            ENVIRONMENT_SCHEMA_VERSION,
            DEFAULT_ENVIRONMENT_ID,
        )

    def write_distribution(self, **overrides: object) -> None:
        values: dict[str, object] = {
            "schema_version": ENVIRONMENT_SCHEMA_VERSION,
            "environment_id": DEFAULT_ENVIRONMENT_ID,
            "status": "published",
            "platform": DEFAULT_PLATFORM,
            "provenance": "fixture-publication",
            "image_reference": IMAGE_REFERENCE,
            "oci_digest": DIGEST,
        }
        values.update(overrides)
        lines = []
        for key, value in values.items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            elif isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            else:
                lines.append(f"{key} = {value}")
        self.distribution_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def resolver(self) -> DefaultEnvironmentResolver:
        return DefaultEnvironmentResolver(self.distribution_path)

    def test_resolver_returns_deterministic_immutable_result(self) -> None:
        self.write_distribution()

        first = self.resolver().resolve(self.spec)
        second = self.resolver().resolve(self.spec)

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(first.image_reference, IMAGE_REFERENCE)
        with self.assertRaises(FrozenInstanceError):
            first.oci_digest = OTHER_DIGEST  # type: ignore[misc]

    def test_resolver_rejects_unsupported_environment_before_reading_data(self) -> None:
        unsupported = EnvironmentSpec(
            ENVIRONMENT_SCHEMA_VERSION,
            "another-worker",
        )

        with self.assertRaisesRegex(UnsupportedEnvironmentError, "another-worker"):
            self.resolver().resolve(unsupported)

    def test_distribution_identity_and_platform_must_match_request(self) -> None:
        for key, value in (
            ("environment_id", "another-worker"),
            ("platform", "linux/arm64"),
            ("provenance", "audit\nforged"),
            ("provenance", "audit\x7fforged"),
        ):
            with self.subTest(key=key):
                self.write_distribution(**{key: value})
                with self.assertRaises(EnvironmentDistributionDataError):
                    self.resolver().resolve(self.spec)

    def test_invalid_published_reference_is_not_downgraded_or_repaired(self) -> None:
        for image_reference, oci_digest in (
            ("ghcr.io/example/orchestra-worker:latest", DIGEST),
            (IMAGE_REFERENCE, OTHER_DIGEST),
        ):
            with self.subTest(image_reference=image_reference):
                self.write_distribution(
                    image_reference=image_reference,
                    oci_digest=oci_digest,
                )
                with self.assertRaises(EnvironmentDistributionDataError):
                    self.resolver().resolve(self.spec)

    def test_distribution_data_is_versioned_and_rejects_unknown_keys(self) -> None:
        for overrides in (
            {"schema_version": 2},
            {"schema_version": True},
            {"unexpected_policy": "not-allowed"},
            {"status": "pending"},
        ):
            with self.subTest(overrides=overrides):
                self.write_distribution(**overrides)
                with self.assertRaises(EnvironmentDistributionDataError):
                    self.resolver().resolve(self.spec)

    def test_resolution_paths_have_no_external_or_global_side_effects(self) -> None:
        published = self.directory / "published.toml"
        unpublished = self.directory / "unpublished.toml"
        invalid_schema = self.directory / "invalid-schema.toml"
        invalid_reference = self.directory / "invalid-reference.toml"
        missing = self.directory / "missing.toml"

        self.write_distribution()
        published.write_text(
            self.distribution_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.write_distribution(status="unpublished")
        unpublished.write_text(
            "\n".join(
                line
                for line in self.distribution_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if not line.startswith(("image_reference", "oci_digest"))
            )
            + "\n",
            encoding="utf-8",
        )
        self.write_distribution(schema_version=2)
        invalid_schema.write_text(
            self.distribution_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.write_distribution(
            image_reference="ghcr.io/example/orchestra-worker:latest"
        )
        invalid_reference.write_text(
            self.distribution_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        environment_before = dict(os.environ)
        cwd_before = os.getcwd()
        opened_paths: list[Path] = []
        original_path_open = Path.open

        def audited_open(path: Path, *args: object, **kwargs: object):
            opened_paths.append(path.resolve(strict=False))
            return original_path_open(path, *args, **kwargs)

        guarded_operations = (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (subprocess, "call"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
            (os, "system"),
            (os, "popen"),
            (os, "chdir"),
            (os, "putenv"),
            (os, "unsetenv"),
            (socket, "socket"),
            (socket, "create_connection"),
            (urllib.request, "urlopen"),
            (http.client, "HTTPConnection"),
            (http.client, "HTTPSConnection"),
            (tarfile, "open"),
            (zipfile, "ZipFile"),
            (shutil, "unpack_archive"),
            (shutil, "make_archive"),
        )

        with ExitStack() as stack:
            operation_mocks = [
                stack.enter_context(mock.patch.object(owner, name))
                for owner, name in guarded_operations
            ]
            stack.enter_context(mock.patch.object(Path, "open", audited_open))

            environment = DefaultEnvironmentResolver(published).resolve(self.spec)
            with self.assertRaises(EnvironmentNotPublishedError):
                DefaultEnvironmentResolver(unpublished).resolve(self.spec)
            with self.assertRaises(EnvironmentDistributionDataError):
                DefaultEnvironmentResolver(invalid_schema).resolve(self.spec)
            with self.assertRaises(EnvironmentDistributionDataError):
                DefaultEnvironmentResolver(invalid_reference).resolve(self.spec)
            with self.assertRaises(EnvironmentDistributionDataError):
                DefaultEnvironmentResolver(missing).resolve(self.spec)

        self.assertEqual(environment.image_reference, IMAGE_REFERENCE)
        for operation in operation_mocks:
            operation.assert_not_called()
        self.assertEqual(os.getcwd(), cwd_before)
        self.assertEqual(dict(os.environ), environment_before)
        self.assertEqual(
            opened_paths,
            [
                published.resolve(),
                unpublished.resolve(),
                invalid_schema.resolve(),
                invalid_reference.resolve(),
                missing.resolve(),
            ],
        )

    def test_repository_default_is_explicitly_unpublished(self) -> None:
        with self.assertRaisesRegex(
            EnvironmentNotPublishedError,
            "not published",
        ):
            DefaultEnvironmentResolver().resolve(self.spec)

    def test_unpublished_data_never_falls_back_to_legacy_artifacts(self) -> None:
        self.write_distribution(status="unpublished")
        lines = self.distribution_path.read_text(encoding="utf-8").splitlines()
        self.distribution_path.write_text(
            "\n".join(
                line
                for line in lines
                if not line.startswith(("image_reference", "oci_digest"))
            )
            + "\n",
            encoding="utf-8",
        )
        (self.directory / "worker-image-archive.tar.gz").touch()
        (self.directory / "worker-sandbox.lock.toml").write_text(
            f'image_id = "{DIGEST}"\n',
            encoding="utf-8",
        )

        with self.assertRaises(EnvironmentNotPublishedError):
            self.resolver().resolve(self.spec)

    def test_missing_distribution_data_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(
            EnvironmentDistributionDataError,
            "Unable to read",
        ):
            self.resolver().resolve(self.spec)


if __name__ == "__main__":
    unittest.main()
