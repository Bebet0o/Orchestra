from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from controller_api.blueprint import API_VERSION, SOURCE_FORMAT, validate_path, validate_source


VALID = """
apiVersion: orchestra.dev/v1
kind: SandboxProfile
metadata:
  name: python-project
  displayName: Python Project Worker
  labels:
    language: python
spec:
  base:
    image: python
    tag: 3.12.10-slim-bookworm
    digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  build:
    apt:
      packages:
        - git=1:2.39.5-0+deb12u2
    python:
      packages:
        - pytest==9.0.2
    environment:
      DEBIAN_FRONTEND: noninteractive
    steps:
      - name: python-version
        run: [python3, --version]
        timeout: 60s
  workspace:
    user: hermes
    group: hermes
    directory: /workspace
    sourceMode: worktree
  runtime:
    cpu: 4.0
    memory: 1024MiB
    pids: 512
    timeout: 120m
    stopGracePeriod: 30s
    tmpfsSize: 1024MiB
  network:
    build:
      mode: allowlist
      allow: [pypi.org, files.pythonhosted.org]
    runtime:
      mode: none
      allow: []
  security:
    privileged: false
    noNewPrivileges: true
    readOnlyRoot: false
    capabilities:
      drop: [ALL]
      add: []
    seccompProfile: default
    secrets: false
    allowDockerSocket: false
    allowDeviceAccess: false
  mounts:
    - name: artifacts
      type: artifact
      target: /artifacts
      readOnly: false
  validation:
    commands:
      - name: python
        run: [python3, --version]
        timeout: 30s
        expectExitCode: 0
"""


def codes(report) -> set[str]:
    return {item.code for item in report.diagnostics}


class BlueprintV1Test(unittest.TestCase):
    def test_valid_source_and_canonical_defaults(self) -> None:
        report = validate_source(textwrap.dedent(VALID))
        self.assertTrue(report.valid, report.as_dict())
        assert report.result is not None
        self.assertEqual(report.result.api_version, API_VERSION)
        self.assertEqual(report.result.source_format, SOURCE_FORMAT)
        self.assertEqual(report.result.canonical["spec"]["base"]["registry"], "docker.io")
        self.assertEqual(report.result.canonical["spec"]["runtime"]["cpu"], 4)
        self.assertEqual(report.result.canonical["spec"]["runtime"]["memory"], "1GiB")
        self.assertEqual(report.result.canonical["spec"]["runtime"]["timeout"], "2h")
        self.assertEqual(report.result.canonical["spec"]["runtime"]["tmpfsSize"], "1GiB")
        self.assertEqual(
            hashlib_sha256(report.result.canonical_bytes),
            report.result.canonical_sha256,
        )

    def test_formatting_comments_and_equivalent_units_share_canonical_hash(self) -> None:
        first = validate_source(textwrap.dedent(VALID))
        variant = textwrap.dedent(VALID).replace("1024MiB", "1GiB").replace("120m", "2h")
        variant = "# source comment\n" + variant.replace(
            "  displayName: Python Project Worker\n", ""
        ).replace(
            "  labels:\n    language: python\n",
            "  labels: {language: python}\n  displayName: Python Project Worker\n",
        )
        second = validate_source(variant)
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        assert first.result is not None and second.result is not None
        self.assertNotEqual(first.result.source_sha256, second.result.source_sha256)
        self.assertEqual(first.result.canonical_sha256, second.result.canonical_sha256)

    def test_v0_and_unknown_fields_are_rejected(self) -> None:
        old = textwrap.dedent(VALID).replace(
            "orchestra.dev/v1", "orchestra.dev/v0alpha1"
        )
        report = validate_source(old)
        self.assertFalse(report.valid)
        self.assertIn("unsupported_api_version", codes(report))
        unknown = textwrap.dedent(VALID).replace(
            "metadata:\n", "metadata:\n  extra: forbidden\n"
        )
        report = validate_source(unknown)
        self.assertFalse(report.valid)
        self.assertIn("unknown_field", codes(report))

    def test_historical_hermesops_api_version_is_rejected_as_public_input(self) -> None:
        historical = textwrap.dedent(VALID).replace(
            "orchestra.dev/v1", "hermesops.dev/v1"
        )
        report = validate_source(historical)
        self.assertFalse(report.valid)
        self.assertIn("unsupported_api_version", codes(report))

    def test_duplicate_keys_aliases_and_multiple_documents_are_rejected(self) -> None:
        duplicate = textwrap.dedent(VALID).replace(
            "  name: python-project\n",
            "  name: python-project\n  name: duplicate\n",
        )
        self.assertIn("yaml_parse_failed", codes(validate_source(duplicate)))
        alias = "apiVersion: &v orchestra.dev/v1\nkind: *v\nmetadata: {}\nspec: {}\n"
        self.assertIn("yaml_parse_failed", codes(validate_source(alias)))
        multiple = textwrap.dedent(VALID) + "\n---\n{}\n"
        self.assertIn("multiple_yaml_documents", codes(validate_source(multiple)))

    def test_yaml_11_boolean_words_remain_strings(self) -> None:
        source = textwrap.dedent(VALID).replace(
            "DEBIAN_FRONTEND: noninteractive",
            "DEBIAN_FRONTEND: on",
        )
        report = validate_source(source)
        self.assertTrue(report.valid, report.as_dict())
        assert report.result is not None
        self.assertEqual(
            report.result.canonical["spec"]["build"]["environment"]["DEBIAN_FRONTEND"],
            "on",
        )

    def test_security_invariants_fail_closed(self) -> None:
        for old, new in (
            ("privileged: false", "privileged: true"),
            ("noNewPrivileges: true", "noNewPrivileges: false"),
            ("secrets: false", "secrets: true"),
            ("allowDockerSocket: false", "allowDockerSocket: true"),
            ("allowDeviceAccess: false", "allowDeviceAccess: true"),
            ("add: []", "add: [NET_ADMIN]"),
        ):
            with self.subTest(new=new):
                report = validate_source(textwrap.dedent(VALID).replace(old, new))
                self.assertFalse(report.valid)
                self.assertIn("security_invariant_violation", codes(report))

    def test_secret_like_environment_is_rejected_without_echo(self) -> None:
        source = textwrap.dedent(VALID).replace(
            "DEBIAN_FRONTEND: noninteractive",
            "API_TOKEN: do-not-display-this-value",
        )
        report = validate_source(source)
        self.assertFalse(report.valid)
        serialized = json.dumps(report.as_dict())
        self.assertIn("secret_environment_key_forbidden", serialized)
        self.assertNotIn("do-not-display-this-value", serialized)

    def test_shell_passthrough_is_rejected_but_python_c_is_not(self) -> None:
        shell = textwrap.dedent(VALID).replace(
            "run: [python3, --version]",
            "run: [sh, -c, echo unsafe]",
            1,
        )
        report = validate_source(shell)
        self.assertFalse(report.valid)
        self.assertIn("shell_execution_forbidden", codes(report))
        python = textwrap.dedent(VALID).replace(
            "run: [python3, --version]",
            "run: [python3, -c, print-ok]",
            1,
        )
        self.assertTrue(validate_source(python).valid)

    def test_mount_overlap_and_protected_paths_are_rejected(self) -> None:
        overlap = textwrap.dedent(VALID).replace(
            "  validation:\n",
            "    - name: nested\n"
            "      type: cache\n"
            "      target: /artifacts/cache\n"
            "      readOnly: false\n"
            "  validation:\n",
        )
        report = validate_source(overlap)
        self.assertFalse(report.valid)
        self.assertIn("overlapping_mount_targets", codes(report))
        protected = textwrap.dedent(VALID).replace("/artifacts", "/etc")
        report = validate_source(protected)
        self.assertFalse(report.valid)
        self.assertIn("unsafe_container_path", codes(report))

    def test_network_destination_and_full_network_warning(self) -> None:
        bad = textwrap.dedent(VALID).replace("pypi.org", "https://u:p@example.invalid")
        report = validate_source(bad)
        self.assertFalse(report.valid)
        self.assertIn("invalid_network_destination", codes(report))
        full = textwrap.dedent(VALID).replace(
            "mode: allowlist\n      allow: [pypi.org, files.pythonhosted.org]",
            "mode: full\n      allow: []",
        )
        report = validate_source(full)
        self.assertTrue(report.valid, report.as_dict())
        self.assertIn("full_network_requires_policy", codes(report))

    def test_unpinned_package_is_warning_not_error(self) -> None:
        source = textwrap.dedent(VALID).replace(
            "git=1:2.39.5-0+deb12u2", "git"
        )
        report = validate_source(source)
        self.assertTrue(report.valid, report.as_dict())
        self.assertIn("package_not_version_pinned", codes(report))

    def test_source_bounds_utf8_and_nonfinite_numbers(self) -> None:
        self.assertIn("source_too_large", codes(validate_source(b"x" * (256 * 1024 + 1))))
        self.assertIn("invalid_utf8", codes(validate_source(b"\xff")))
        nan = textwrap.dedent(VALID).replace("cpu: 4.0", "cpu: .nan")
        self.assertFalse(validate_source(nan).valid)

    def test_path_validation_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "Blueprint"
            real.write_text(textwrap.dedent(VALID), encoding="utf-8")
            link = root / "Blueprint.link"
            link.symlink_to(real)
            report = validate_path(link)
            self.assertFalse(report.valid)
            self.assertIn("source_unavailable", codes(report))

    def test_schema_contract_and_example(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (repository / "specs/blueprint-v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["apiVersion"]["const"], API_VERSION)
        self.assertEqual(schema["properties"]["kind"]["const"], "SandboxProfile")
        security = schema["properties"]["spec"]["properties"]["security"]["properties"]
        self.assertIs(security["privileged"]["const"], False)
        self.assertIs(security["secrets"]["const"], False)
        self.assertIs(security["allowDockerSocket"]["const"], False)
        example = repository / "config/examples/Blueprint"
        report = validate_path(example)
        self.assertTrue(report.valid, report.as_dict())

    def test_cli_validate_fingerprint_and_canonicalize(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        cli = repository / "scripts/orchestra-blueprint.py"
        self.assertTrue(cli.is_file())
        self.assertFalse((repository / "scripts/orchestra-hermesfile.py").exists())
        example = repository / "config/examples/Blueprint"
        help_result = subprocess.run(
            [sys.executable, str(cli), "--help"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Blueprint v1", help_result.stdout)
        self.assertNotIn("Hermesfile", help_result.stdout)
        validate = subprocess.run(
            [sys.executable, str(cli), "validate", str(example), "--json"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertTrue(json.loads(validate.stdout)["valid"])
        fingerprint = subprocess.run(
            [sys.executable, str(cli), "fingerprint", str(example), "--json"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(fingerprint.returncode, 0, fingerprint.stderr)
        self.assertEqual(json.loads(fingerprint.stdout)["source_format"], SOURCE_FORMAT)
        canonical = subprocess.run(
            [sys.executable, str(cli), "canonicalize", str(example)],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(canonical.returncode, 0, canonical.stderr.decode())
        parsed = json.loads(canonical.stdout.decode())
        self.assertEqual(parsed["apiVersion"], API_VERSION)

    def test_cli_rejects_historical_hermesops_api_version(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        cli = repository / "scripts/orchestra-blueprint.py"
        with tempfile.TemporaryDirectory() as directory:
            historical = Path(directory) / "Blueprint"
            historical.write_text(
                textwrap.dedent(VALID).replace(
                    "orchestra.dev/v1", "hermesops.dev/v1"
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(cli), "validate", str(historical), "--json"],
                cwd=repository,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])
        self.assertIn(
            "unsupported_api_version",
            {item["code"] for item in payload["diagnostics"]},
        )


def hashlib_sha256(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    unittest.main(verbosity=2)
