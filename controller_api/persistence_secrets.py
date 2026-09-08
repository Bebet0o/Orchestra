from __future__ import annotations

import re


_COMMON_CREDENTIAL_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(
        rb"(?i)\b(?:token|password|secret|api[_-]?key)"
        rb"\s*[:=]\s*(?!false\b|null\b|none\b)[^\s#]+"
    ),
    re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(
        rb"(?<![A-Za-z0-9_-])"
        rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        rb"(?![A-Za-z0-9_-])"
    ),
)

_TEMPLATE_EXPANSION_PATTERN = re.compile(rb"\$\{")


def contains_credential_like(
    value: bytes,
    *,
    reject_template_expansion: bool = False,
) -> bool:
    """Return True for high-confidence persistence hazards.

    Blueprint source remains stricter because template expansion can reference
    runtime secrets. Project memory deliberately permits harmless references
    such as ``${HOME}``; actual credential signatures stay blocked.
    """

    if not isinstance(value, bytes):
        raise TypeError("credential scan requires bytes")
    if reject_template_expansion and _TEMPLATE_EXPANSION_PATTERN.search(value):
        return True
    return any(pattern.search(value) for pattern in _COMMON_CREDENTIAL_PATTERNS)
