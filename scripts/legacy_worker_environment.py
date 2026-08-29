"""Temporary adapter from the shared environment spec to the inherited lock."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from environment_resolution import DEFAULT_ENVIRONMENT_ID, EnvironmentSpec


_LOCAL_CONFIG_ID = re.compile(r"sha256:[0-9a-f]{64}")
_LOCAL_TAG_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
_LOCAL_IMAGE_TAG = re.compile(
    rf"{_LOCAL_TAG_COMPONENT}(?:/{_LOCAL_TAG_COMPONENT})*:"
    r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
)


class LegacyEnvironmentError(RuntimeError):
    """The inherited local worker environment is absent or invalid."""


@dataclass(frozen=True)
class LegacyLocalEnvironment:
    """Explicitly local, same-daemon evidence retained only during migration."""

    environment_id: str
    local_image_config_id: str
    local_image_tag: str
    provenance: str = "legacy-worker-sandbox-lock"

    def __post_init__(self) -> None:
        if self.environment_id != DEFAULT_ENVIRONMENT_ID:
            raise ValueError("Legacy environment_id must be default-worker")
        # Transitional hardening is intentional: a same-daemon config identity
        # must be canonical even though it is never OCI distribution authority.
        if _LOCAL_CONFIG_ID.fullmatch(self.local_image_config_id) is None:
            raise ValueError("Legacy local image config identity is invalid")
        if (
            not isinstance(self.local_image_tag, str)
            or _LOCAL_IMAGE_TAG.fullmatch(self.local_image_tag) is None
        ):
            raise ValueError("Legacy local image tag is invalid")
        if not self.provenance:
            raise ValueError("Legacy environment provenance is required")


class LegacyWorkerEnvironmentAdapter:
    """Bridge EnvironmentSpec to today's local runtime without OCI fallback."""

    def __init__(
        self,
        lock_path: Path,
        availability_check: Callable[[str], object],
    ) -> None:
        if not isinstance(lock_path, Path):
            raise TypeError("Legacy worker lock_path must be a Path")
        if not callable(availability_check):
            raise TypeError("Legacy worker availability_check must be callable")
        self._lock_path = lock_path
        self._availability_check = availability_check

    def load(self, spec: EnvironmentSpec) -> LegacyLocalEnvironment:
        if not isinstance(spec, EnvironmentSpec):
            raise TypeError("Legacy worker adapter requires an EnvironmentSpec")
        if spec.environment_id != DEFAULT_ENVIRONMENT_ID:
            raise LegacyEnvironmentError(
                f"Unsupported legacy environment: {spec.environment_id}"
            )

        try:
            with self._lock_path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise LegacyEnvironmentError(
                f"Unable to read legacy worker lock at {self._lock_path}: {error}"
            ) from error

        if type(document.get("schema_version")) is not int or document.get(
            "schema_version"
        ) != 1:
            raise LegacyEnvironmentError(
                "Legacy worker lock schema_version is unsupported"
            )

        try:
            environment = LegacyLocalEnvironment(
                environment_id=spec.environment_id,
                local_image_config_id=document.get("image_id"),
                local_image_tag=document.get("tag"),
            )
        except (TypeError, ValueError) as error:
            raise LegacyEnvironmentError(
                f"Invalid legacy worker lock: {error}"
            ) from error

        self._availability_check(environment.local_image_config_id)
        return environment
