"""Pure canonical parsing for Orchestra immutable OCI image references."""

from __future__ import annotations

import re
from dataclasses import dataclass


_REGISTRY_LABEL = re.compile(
    r"(?:[a-z0-9]|[a-z0-9][a-z0-9-]{0,61}[a-z0-9])"
)
_REPOSITORY_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
_REPOSITORY_PATH_COMPONENT = re.compile(_REPOSITORY_COMPONENT)
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ParsedOCIReference:
    """Canonical immutable OCI reference and its embedded digest."""

    image_reference: str
    digest: str


def is_canonical_oci_digest(value: object) -> bool:
    return isinstance(value, str) and _OCI_DIGEST.fullmatch(value) is not None


def _validate_registry(registry: str) -> None:
    if not registry or registry.count(":") > 1:
        raise ValueError("OCI image reference registry is invalid")

    host = registry
    if ":" in registry:
        host, port_text = registry.rsplit(":", 1)
        if (
            not port_text
            or not port_text.isascii()
            or not port_text.isdigit()
            or port_text.startswith("0")
        ):
            raise ValueError("OCI image reference registry port is invalid")
        port = int(port_text)
        if port < 1 or port > 65_535:
            raise ValueError("OCI image reference registry port is invalid")

    if not host or len(host) > 253:
        raise ValueError("OCI image reference registry host is invalid")
    labels = host.split(".")
    if any(_REGISTRY_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("OCI image reference registry host is invalid")

    if len(labels) == 4 and all(label.isdigit() for label in labels):
        if any(int(label) > 255 for label in labels):
            raise ValueError("OCI image reference IPv4 host is invalid")


def _validate_repository(repository: str) -> None:
    if not repository or len(repository) > 255:
        raise ValueError("OCI image reference repository is invalid")
    components = repository.split("/")
    if any(
        component in {"", ".", ".."}
        or len(component) > 128
        or _REPOSITORY_PATH_COMPONENT.fullmatch(component) is None
        for component in components
    ):
        raise ValueError("OCI image reference repository is invalid")


def parse_immutable_oci_reference(image_reference: object) -> ParsedOCIReference:
    """Parse exactly the immutable reference grammar Orchestra authorizes."""
    if not isinstance(image_reference, str) or image_reference.count("@") != 1:
        raise ValueError("OCI image reference must be complete and immutable")

    name, digest = image_reference.split("@", 1)
    if not is_canonical_oci_digest(digest) or "/" not in name:
        raise ValueError(
            "OCI image reference must be registry/repository@sha256:"
            "<64 lowercase hex>"
        )

    registry, repository = name.split("/", 1)
    _validate_registry(registry)
    _validate_repository(repository)
    return ParsedOCIReference(image_reference=image_reference, digest=digest)
