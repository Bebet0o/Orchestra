from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import json
import sqlite3
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from controller_api.core import ControllerError
from controller_api.sandbox_profiles import SandboxProfileStore


VALID = """
apiVersion: orchestra.dev/v1
kind: SandboxProfile
metadata:
  name: python-project
  displayName: Python Project Worker
  description: Reproducible Python sandbox.
  labels:
    language: python
spec:
  base:
    image: python
    digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  build:
    python:
      packages: [pytest==9.0.2]
  workspace:
    user: hermes
    group: hermes
    directory: /workspace
    sourceMode: worktree
  runtime:
    cpu: 4
    memory: 1GiB
    pids: 512
    timeout: 2h
    stopGracePeriod: 30s
  network:
    build:
      mode: allowlist
      allow: [pypi.org]
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
  validation:
    commands:
      - name: python
        run: [python3, --version]
        timeout: 30s
        expectExitCode: 0
"""


class SandboxProfileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "controller.db"
        connection = sqlite3.connect(self.database)
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        connection.execute("PRAGMA foreign_keys=ON")
        for migration in sorted(migrations.glob("[0-9][0-9][0-9]_*.sql")):
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.close()
        self.store = SandboxProfileStore(
            SimpleNamespace(database=self.database)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_and_readiness(self) -> None:
        self.assertEqual(self.store.readiness(), (True, "ready"))
        connection = sqlite3.connect(self.database)
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0],
            29,
        )
        self.assertEqual(
            connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall(),
            [(version,) for version in range(1, 30)],
        )
        connection.close()

    def test_import_projection_and_content_idempotency(self) -> None:
        first = self.store.import_source(textwrap.dedent(VALID).encode())
        self.assertTrue(first.created)
        self.assertTrue(first.revision_created)
        profile = first.profile
        self.assertEqual(profile["state"], "draft")
        self.assertEqual(profile["source_format"], "blueprint-v1")
        self.assertEqual(profile["source_revision"], 1)
        self.assertEqual(profile["resource_revision"], 1)
        for forbidden in ("source", "source_text", "canonical", "canonical_json"):
            self.assertNotIn(forbidden, profile)
        second = self.store.import_source(textwrap.dedent(VALID).encode())
        self.assertFalse(second.created)
        self.assertFalse(second.revision_created)
        self.assertEqual(second.profile["id"], profile["id"])
        self.assertEqual(second.profile["source_revision"], 1)

    def test_source_revision_preserves_canonical_equivalence(self) -> None:
        first = self.store.import_source(textwrap.dedent(VALID).encode())
        variant = (
            "# formatting-only revision\n"
            + textwrap.dedent(VALID).replace("memory: 1GiB", "memory: 1024MiB")
        )
        second = self.store.import_source(variant.encode())
        self.assertEqual(second.profile["source_revision"], 2)
        self.assertEqual(second.profile["resource_revision"], 2)
        self.assertNotEqual(
            first.profile["source_sha256"],
            second.profile["source_sha256"],
        )
        self.assertEqual(
            first.profile["canonical_sha256"],
            second.profile["canonical_sha256"],
        )

    def test_invalid_and_secret_like_sources_are_never_persisted(self) -> None:
        invalid = textwrap.dedent(VALID).replace(
            "privileged: false",
            "privileged: true",
        )
        with self.assertRaises(ControllerError) as context:
            self.store.import_source(invalid.encode())
        self.assertEqual(context.exception.code, "sandbox_source_invalid")
        sentinels = (
            "sk-" + "A" * 32,
            "password=ultra-private-sentinel",
        )
        for sentinel in sentinels:
            with self.subTest(sentinel=sentinel[:12]):
                secret = (
                    f"# {sentinel}\n" + textwrap.dedent(VALID)
                )
                with self.assertRaises(ControllerError) as context:
                    self.store.import_source(secret.encode())
                self.assertEqual(
                    context.exception.code,
                    "sandbox_source_secret_detected",
                )
        dump = "\n".join(
            sqlite3.connect(self.database).iterdump()
        )
        for sentinel in sentinels:
            self.assertNotIn(sentinel, dump)
        self.assertNotIn("python-project", dump)

    def test_import_path_rejects_symlinks_and_multiple_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Blueprint"
            source.write_text(textwrap.dedent(VALID), encoding="utf-8")
            link = root / "Blueprint.link"
            link.symlink_to(source)
            with self.assertRaises(ControllerError) as context:
                self.store.import_path(link)
            self.assertEqual(
                context.exception.code,
                "sandbox_source_unavailable",
            )
            hardlink = root / "Blueprint.hardlink"
            hardlink.hardlink_to(source)
            with self.assertRaises(ControllerError) as context:
                self.store.import_path(source)
            self.assertEqual(
                context.exception.code,
                "sandbox_source_unavailable",
            )
            hardlink.unlink()
            result = self.store.import_path(source)
            self.assertTrue(result.created)

    def test_revisions_are_immutable(self) -> None:
        result = self.store.import_source(textwrap.dedent(VALID).encode())
        connection = sqlite3.connect(self.database)
        revision_id = connection.execute(
            "SELECT current_revision_id FROM sandbox_profiles"
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sandbox_profile_revisions "
                "SET source_sha256=? WHERE revision_id=?",
                ("0" * 64, revision_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM sandbox_profile_revisions "
                "WHERE revision_id=?",
                (revision_id,),
            )
        connection.close()
        self.assertEqual(
            self.store.get_profile(result.profile["id"])["source_revision"],
            1,
        )

    def test_profile_identity_and_revision_sequence_fail_closed(self) -> None:
        result = self.store.import_source(textwrap.dedent(VALID).encode())
        sandbox_id = result.profile["id"]
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sandbox_profiles SET profile_name=? WHERE sandbox_id=?",
                ("renamed-profile", sandbox_id),
            )
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sandbox_profiles "
                "SET current_source_revision=current_source_revision+2, "
                "resource_revision=resource_revision+1 "
                "WHERE sandbox_id=?",
                (sandbox_id,),
            )
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sandbox_profiles "
                "SET current_revision_id=? "
                "WHERE sandbox_id=?",
                ("sandbox-revision-" + "f" * 32, sandbox_id),
            )
            connection.commit()
        connection.rollback()
        connection.execute(
            "UPDATE sandbox_profiles "
            "SET state='inactive', resource_revision=resource_revision+1, "
            "updated_at='2026-12-31T23:59:59.999Z' "
            "WHERE sandbox_id=?",
            (sandbox_id,),
        )
        connection.commit()
        row = connection.execute(
            "SELECT state, current_source_revision, resource_revision "
            "FROM sandbox_profiles WHERE sandbox_id=?",
            (sandbox_id,),
        ).fetchone()
        self.assertEqual(row, ("inactive", 1, 2))
        connection.close()

    def test_public_input_validation_fails_closed(self) -> None:
        for limit in (0, 201, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ControllerError):
                    self.store.list_profiles(
                        limit=limit,
                        cursor=None,
                        state=None,
                        cursor_secret="test-session-secret",
                    )
        with self.assertRaises(ControllerError) as context:
            self.store.list_profiles(
                limit=1,
                cursor=None,
                state="unknown",
                cursor_secret="test-session-secret",
            )
        self.assertEqual(context.exception.code, "invalid_state")
        with self.assertRaises(ControllerError) as context:
            self.store.get_profile("../../etc/passwd")
        self.assertEqual(context.exception.code, "sandbox_profile_not_found")

    def test_duplicate_persisted_json_members_fail_closed(self) -> None:
        result = self.store.import_source(textwrap.dedent(VALID).encode())
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE sandbox_profiles "
            "SET labels_json=?, resource_revision=resource_revision+1, "
            "updated_at='2026-12-31T23:59:59.999Z' "
            "WHERE sandbox_id=?",
            ('{"purpose":"safe","purpose":"overridden"}', result.profile["id"]),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(ControllerError) as context:
            self.store.get_profile(result.profile["id"])
        self.assertEqual(
            context.exception.code,
            "sandbox_profile_projection_failed",
        )

    def test_corrupt_public_metadata_fails_closed_without_echo(self) -> None:
        result = self.store.import_source(textwrap.dedent(VALID).encode())
        sentinel = "password=do-not-display-this-value"
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE sandbox_profiles "
            "SET labels_json=?, resource_revision=resource_revision+1, "
            "updated_at='2026-12-31T23:59:59.999Z' "
            "WHERE sandbox_id=?",
            (json.dumps({"purpose": sentinel}), result.profile["id"]),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(ControllerError) as context:
            self.store.get_profile(result.profile["id"])
        self.assertEqual(
            context.exception.code,
            "sandbox_profile_projection_failed",
        )
        serialized = json.dumps(
            {
                "title": context.exception.title,
                "detail": context.exception.detail,
            }
        )
        self.assertNotIn(sentinel, serialized)

    def test_unexpected_integrity_failure_is_not_reported_as_conflict(self) -> None:
        with mock.patch(
            "controller_api.sandbox_profiles.secrets.token_hex",
            return_value="a" * 32,
        ):
            self.store.import_source(textwrap.dedent(VALID).encode())
            second = textwrap.dedent(VALID).replace(
                "python-project",
                "another-project",
            )
            with self.assertRaises(ControllerError) as context:
                self.store.import_source(second.encode())
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(
            context.exception.code,
            "sandbox_profile_persistence_failed",
        )

    def test_signed_cursor_rejects_invalid_bound_profile_name(self) -> None:
        cursor = self.store._encode_cursor(
            profile_name="../unsafe",
            sandbox_id="sandbox-" + "a" * 32,
            state=None,
            cursor_secret="test-session-secret",
        )
        with self.assertRaises(ControllerError) as context:
            self.store.list_profiles(
                limit=1,
                cursor=cursor,
                state=None,
                cursor_secret="test-session-secret",
            )
        self.assertEqual(context.exception.code, "invalid_cursor")

    @staticmethod
    def _base64url_alias(segment: str) -> str:
        raw = base64.urlsafe_b64decode(
            segment + "=" * (-len(segment) % 4)
        )
        alphabet = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789-_"
        )
        for character in alphabet:
            candidate = segment[:-1] + character
            if candidate == segment:
                continue
            try:
                decoded = base64.urlsafe_b64decode(
                    candidate + "=" * (-len(candidate) % 4)
                )
            except ValueError:
                continue
            if decoded == raw:
                return candidate
        raise AssertionError("no noncanonical Base64URL alias found")

    @staticmethod
    def _signed_cursor_from_payload(payload: bytes, secret: str) -> str:
        signature = hmac.new(
            SandboxProfileStore._cursor_key(secret),
            payload,
            hashlib.sha256,
        ).digest()
        return (
            base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii")
        )

    def test_signed_cursor_is_filter_bound_and_tamper_resistant(self) -> None:
        for index in range(3):
            source = textwrap.dedent(VALID).replace(
                "python-project",
                f"python-project-{index}",
            )
            self.store.import_source(source.encode())
        first, cursor = self.store.list_profiles(
            limit=1,
            cursor=None,
            state="draft",
            cursor_secret="test-session-secret",
        )
        self.assertEqual(len(first), 1)
        self.assertIsNotNone(cursor)
        second, _ = self.store.list_profiles(
            limit=1,
            cursor=cursor,
            state="draft",
            cursor_secret="test-session-secret",
        )
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        assert cursor is not None
        left, right = cursor.split(".", 1)
        replacement = "A" if right[0] != "A" else "B"
        tampered = left + "." + replacement + right[1:]
        with self.assertRaises(ControllerError):
            self.store.list_profiles(
                limit=1,
                cursor=tampered,
                state="draft",
                cursor_secret="test-session-secret",
            )
        with self.assertRaises(ControllerError):
            self.store.list_profiles(
                limit=1,
                cursor=cursor,
                state=None,
                cursor_secret="test-session-secret",
            )

    def test_signed_cursor_rejects_noncanonical_base64url_aliases(self) -> None:
        cursor = self.store._encode_cursor(
            profile_name="python-project-0",
            sandbox_id="sandbox-" + "a" * 32,
            state="draft",
            cursor_secret="test-session-secret",
        )
        left, right = cursor.split(".", 1)
        aliases = (
            self._base64url_alias(left) + "." + right,
            left + "." + self._base64url_alias(right),
        )
        for alias in aliases:
            with self.subTest(alias=alias[-16:]):
                with self.assertRaises(ControllerError) as context:
                    self.store._decode_cursor(
                        alias,
                        state="draft",
                        cursor_secret="test-session-secret",
                    )
                self.assertEqual(context.exception.code, "invalid_cursor")

    def test_signed_cursor_rejects_signed_noncanonical_payloads(self) -> None:
        secret = "test-session-secret"
        sandbox_id = "sandbox-" + "a" * 32
        payloads = (
            (
                b'{"profile_name":"python-project",'
                b'"sandbox_id":"' + sandbox_id.encode("ascii") + b'",'
                b'"state":"draft", "v":1}'
            ),
            (
                b'{"profile_name":"python-project",'
                b'"profile_name":"python-project",'
                b'"sandbox_id":"' + sandbox_id.encode("ascii") + b'",'
                b'"state":"draft","v":1}'
            ),
            (
                b'{"extra":null,"profile_name":"python-project",'
                b'"sandbox_id":"' + sandbox_id.encode("ascii") + b'",'
                b'"state":"draft","v":1}'
            ),
            (
                b'{"profile_name":"python-project",'
                b'"sandbox_id":"' + sandbox_id.encode("ascii") + b'",'
                b'"state":"draft","v":true}'
            ),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:32]):
                cursor = self._signed_cursor_from_payload(payload, secret)
                with self.assertRaises(ControllerError) as context:
                    self.store._decode_cursor(
                        cursor,
                        state="draft",
                        cursor_secret=secret,
                    )
                self.assertEqual(context.exception.code, "invalid_cursor")

    def test_signed_cursor_rejects_invalid_base64url_syntax(self) -> None:
        cursor = self.store._encode_cursor(
            profile_name="python-project",
            sandbox_id="sandbox-" + "a" * 32,
            state=None,
            cursor_secret="test-session-secret",
        )
        left, right = cursor.split(".", 1)
        malformed = (
            left + "=." + right,
            left + "." + right + "=",
            left + "+." + right,
            left + "." + right + "/",
            left + " ." + right,
            "A." + right,
            left + ".A",
            "." + right,
            left + ".",
        )
        for candidate in malformed:
            with self.subTest(candidate=candidate[-16:]):
                with self.assertRaises(ControllerError) as context:
                    self.store._decode_cursor(
                        candidate,
                        state=None,
                        cursor_secret="test-session-secret",
                    )
                self.assertEqual(context.exception.code, "invalid_cursor")

    def test_concurrent_distinct_imports_are_serialized(self) -> None:
        self.store.import_source(textwrap.dedent(VALID).encode())
        sources = [
            (
                "# revision\n"
                + textwrap.dedent(VALID).replace(
                    "Reproducible Python sandbox.",
                    f"Reproducible Python sandbox revision {index}.",
                )
            ).encode()
            for index in range(1, 5)
        ]

        def import_one(source: bytes):
            return self.store.import_source(source)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(import_one, sources))
        revisions = sorted(
            result.profile["source_revision"] for result in results
        )
        self.assertEqual(revisions, [2, 3, 4, 5])
        final = self.store.get_profile(results[-1].profile["id"])
        self.assertEqual(final["source_revision"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
