#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

ALLOWED_ENV_FILES = {"compose/images.lock.env"}
ALLOWED_ENV_SUFFIXES = (".env.example", ".example.env")
PLACEHOLDERS = (
    "replace-with", "changeme", "example", "placeholder",
    "<redacted>", "<secret>", "${",
)


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return [
            root / Path(os.fsdecode(item))
            for item in result.stdout.split(b"\0")
            if item
        ]
    return [
        path for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    ]


def filename_findings(path_text: str) -> list[str]:
    pure = PurePosixPath(path_text)
    lower = path_text.lower()
    parts = [part.lower() for part in pure.parts]
    findings: list[str] = []
    if pure.name == "auth.json":
        findings.append("AUTH_JSON")
    if "secrets" in parts:
        findings.append("SECRET_DIRECTORY")
    if lower.endswith((".sqlite", ".sqlite3", ".db")):
        findings.append("SQLITE_DATABASE")
    if pure.name.lower() in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        findings.append("PRIVATE_KEY")
    if lower.endswith((".pem", ".key", ".p12", ".pfx")):
        findings.append("PRIVATE_KEY_MATERIAL")
    if (
        lower.endswith(".env")
        and path_text not in ALLOWED_ENV_FILES
        and not lower.endswith(ALLOWED_ENV_SUFFIXES)
    ):
        findings.append("PRIVATE_ENV_FILE")
    return findings


SECRET_SHAPES = [
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("TELEGRAM_TOKEN", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")),
    ("PRIVATE_KEY_BLOCK", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
ASSIGNMENT = re.compile(
    r'''^\s*(?:export\s+)?["']?
    (?P<key>[A-Za-z_][A-Za-z0-9_.-]*
    (?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY)
    [A-Za-z0-9_.-]*)["']?
    \s*[:=]\s*(?P<value>.+?)\s*[,;]?\s*$''',
    re.IGNORECASE | re.VERBOSE,
)


def suspicious_assignment(line: str) -> bool:
    match = ASSIGNMENT.match(line)
    if not match:
        return False
    key = match.group("key").upper()
    value = match.group("value").strip().strip("\"'")
    lowered = value.lower()
    if key.endswith(("_FILE", "_PATH", "_DIR", "_NAME", "_URL", "_BASE", "_MODE", "_ENABLED")):
        return False
    if not value or any(token in lowered for token in PLACEHOLDERS):
        return False
    if value.startswith("<") and value.endswith(">"):
        return False
    if value in {"None", "null", "false", "true", "0", "1"}:
        return False
    if value.startswith(("$(", "${", "os.environ", "environ.", "source.get(", "cfg.get(")):
        return False
    return True


def content_findings(path: Path) -> list[tuple[int, str]]:
    try:
        data = path.read_bytes()
    except OSError:
        return [(0, "READ_ERROR")]
    if b"\0" in data[:8192]:
        return []
    text = data.decode("utf-8", errors="replace")
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_SHAPES:
            if pattern.search(line):
                findings.append((lineno, label))
        if path.suffix.lower() in {".env", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf"} and suspicious_assignment(line):
            findings.append((lineno, "SENSITIVE_ASSIGNMENT"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject tracked Orchestra secrets without displaying values."
    )
    parser.add_argument("--root", default=None)
    args = parser.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent

    filename_hits: list[tuple[str, str]] = []
    content_hits: list[tuple[str, int, str]] = []
    for path in tracked_files(root):
        path_text = path.relative_to(root).as_posix()
        for label in filename_findings(path_text):
            filename_hits.append((path_text, label))
        for lineno, label in content_findings(path):
            content_hits.append((path_text, lineno, label))

    print("SECRET_SCAN_FILENAMES")
    if filename_hits:
        for path_text, label in sorted(set(filename_hits)):
            print(f"{label}: {path_text}")
    else:
        print("NONE")

    print("\nSECRET_SCAN_CONTENT_REDACTED")
    if content_hits:
        for path_text, lineno, label in sorted(set(content_hits)):
            print(f"{label}: {path_text}:{lineno}")
    else:
        print("NONE")

    if filename_hits or content_hits:
        print("\nOrchestra secret scan: FAIL")
        return 1
    print("\nOrchestra secret scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
