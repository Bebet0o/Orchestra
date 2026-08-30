"""Pure immutable contracts for resolving distributable environments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oci_reference import (
    is_canonical_oci_digest,
    parse_immutable_oci_reference,
)


ENVIRONMENT_SCHEMA_VERSION = 1
DEFAULT_ENVIRONMENT_ID = "default-worker"
DEFAULT_PLATFORM = "linux/amd64"

_ENVIRONMENT_ID = re.compile(
    r"(?:[a-z0-9]|[a-z0-9][a-z0-9-]{0,61}[a-z0-9])"
)
_ASCII_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _validate_schema_version(value: object) -> None:
    if type(value) is not int or value != ENVIRONMENT_SCHEMA_VERSION:
        raise ValueError(
            "Environment schema_version must equal "
            f"{ENVIRONMENT_SCHEMA_VERSION}"
        )


def _validate_environment_id(value: object) -> None:
    if not isinstance(value, str) or _ENVIRONMENT_ID.fullmatch(value) is None:
        raise ValueError("Environment environment_id is invalid")


def _validate_provenance(provenance: object) -> None:
    if (
        not isinstance(provenance, str)
        or not provenance.strip()
        or provenance != provenance.strip()
        or _ASCII_CONTROL.search(provenance) is not None
    ):
        raise ValueError(
            "Environment provenance must be non-empty, trimmed, and free of "
            "ASCII control characters"
        )


@dataclass(frozen=True)
class EnvironmentSpec:
    """Stable logical environment selection, independent of its registry."""

    schema_version: int
    environment_id: str

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_environment_id(self.environment_id)


@dataclass(frozen=True)
class ResolvedEnvironment:
    """Validated distribution identity ready for a future sandbox backend."""

    schema_version: int
    environment_id: str
    image_reference: str
    oci_digest: str
    platform: str
    provenance: str

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_environment_id(self.environment_id)
        reference_digest = parse_immutable_oci_reference(
            self.image_reference
        ).digest
        if not is_canonical_oci_digest(self.oci_digest):
            raise ValueError(
                "Environment oci_digest must be sha256:<64 lowercase hex>"
            )
        if self.oci_digest != reference_digest:
            raise ValueError(
                "Environment oci_digest does not match image_reference"
            )
        if self.platform != DEFAULT_PLATFORM:
            raise ValueError(
                f"Environment platform must equal {DEFAULT_PLATFORM}"
            )
        _validate_provenance(self.provenance)


@runtime_checkable
class EnvironmentResolver(Protocol):
    """Resolve a logical environment into one immutable distribution identity."""

    def resolve(self, spec: EnvironmentSpec) -> ResolvedEnvironment:
        ...
