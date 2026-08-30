"""Fail-closed resolver for Orchestra's default worker environment."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .contract import (
    DEFAULT_ENVIRONMENT_ID,
    DEFAULT_PLATFORM,
    ENVIRONMENT_SCHEMA_VERSION,
    EnvironmentSpec,
    ResolvedEnvironment,
    _validate_provenance,
)


DEFAULT_DISTRIBUTION_DATA = (
    Path(__file__).resolve().parents[2]
    / "config/environments/default-worker.toml"
)


class EnvironmentResolutionError(RuntimeError):
    """Base error for explicit environment-resolution failures."""


class UnsupportedEnvironmentError(EnvironmentResolutionError):
    """The requested logical environment is not supported by this resolver."""


class EnvironmentNotPublishedError(EnvironmentResolutionError):
    """The requested environment has no frozen distribution identity yet."""


class EnvironmentDistributionDataError(EnvironmentResolutionError):
    """Versioned environment distribution data is absent or invalid."""


class DefaultEnvironmentResolver:
    """Read and validate versioned distribution data without external effects."""

    _BASE_KEYS = frozenset(
        {
            "schema_version",
            "environment_id",
            "status",
            "platform",
            "provenance",
        }
    )
    _PUBLISHED_KEYS = _BASE_KEYS | {"image_reference", "oci_digest"}

    def __init__(self, distribution_path: Path | None = None) -> None:
        if distribution_path is not None and not isinstance(
            distribution_path,
            Path,
        ):
            raise TypeError("Environment distribution_path must be a Path")
        self._distribution_path = (
            DEFAULT_DISTRIBUTION_DATA
            if distribution_path is None
            else distribution_path
        )

    def resolve(self, spec: EnvironmentSpec) -> ResolvedEnvironment:
        if not isinstance(spec, EnvironmentSpec):
            raise TypeError("Default resolver requires an EnvironmentSpec")
        if spec.environment_id != DEFAULT_ENVIRONMENT_ID:
            raise UnsupportedEnvironmentError(
                f"Unsupported environment: {spec.environment_id}"
            )

        document = self._read_distribution_data()
        self._validate_common_data(document, spec)
        status = document.get("status")

        if status == "unpublished":
            self._require_exact_keys(document, self._BASE_KEYS)
            raise EnvironmentNotPublishedError(
                "Environment default-worker is not published; no immutable OCI "
                "reference has been frozen"
            )
        if status != "published":
            raise EnvironmentDistributionDataError(
                "Environment distribution status must be published or unpublished"
            )

        self._require_exact_keys(document, self._PUBLISHED_KEYS)
        try:
            return ResolvedEnvironment(
                schema_version=document["schema_version"],
                environment_id=document["environment_id"],
                image_reference=document["image_reference"],
                oci_digest=document["oci_digest"],
                platform=document["platform"],
                provenance=document["provenance"],
            )
        except (TypeError, ValueError) as error:
            raise EnvironmentDistributionDataError(
                f"Invalid published environment distribution data: {error}"
            ) from error

    def _read_distribution_data(self) -> dict[str, Any]:
        try:
            with self._distribution_path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise EnvironmentDistributionDataError(
                "Unable to read environment distribution data at "
                f"{self._distribution_path}: {error}"
            ) from error
        return document

    @staticmethod
    def _validate_common_data(
        document: dict[str, Any],
        spec: EnvironmentSpec,
    ) -> None:
        if type(document.get("schema_version")) is not int or document.get(
            "schema_version"
        ) != ENVIRONMENT_SCHEMA_VERSION:
            raise EnvironmentDistributionDataError(
                "Environment distribution schema_version is unsupported"
            )
        if document.get("environment_id") != spec.environment_id:
            raise EnvironmentDistributionDataError(
                "Environment distribution environment_id does not match request"
            )
        if document.get("platform") != DEFAULT_PLATFORM:
            raise EnvironmentDistributionDataError(
                f"Environment distribution platform must equal {DEFAULT_PLATFORM}"
            )
        try:
            _validate_provenance(document.get("provenance"))
        except ValueError as error:
            raise EnvironmentDistributionDataError(
                f"Environment distribution provenance is invalid: {error}"
            ) from error

    @staticmethod
    def _require_exact_keys(
        document: dict[str, Any],
        expected: frozenset[str],
    ) -> None:
        actual = frozenset(document)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise EnvironmentDistributionDataError(
                "Environment distribution keys are invalid: "
                f"missing={missing}, unknown={unknown}"
            )
