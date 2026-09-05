"""Deterministic model-routing domain contract."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any


_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_MAX_RULES = 64


class ModelRouterError(RuntimeError):
    """Raised when routing configuration or input is invalid."""


class ModelRouteReason(str, Enum):
    RULE_MATCH = "rule_match"
    CONFIGURED_DEFAULT = "configured_default"
    RUNTIME_MANAGED = "runtime_managed"


def _require_token(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value.encode("utf-8")) > maximum or not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _require_optional_token(value: object, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_token(value, name, maximum)


def _require_model_id(value: object, name: str) -> str:
    # Model IDs are deliberately opaque and must remain compatible with the
    # ModelProvider contract (for example "org/model" identifiers).
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if (
        not value.strip()
        or len(value) > 256
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ModelRouterError("Model routing value is not canonical JSON") from error


@dataclass(frozen=True, slots=True)
class ModelRouteRequest:
    runtime_request_id: str
    role_id: str
    runtime_role: str
    runtime_kind: str
    configured_model_id: str
    task_kind: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "runtime_request_id",
            _require_token(self.runtime_request_id, "runtime_request_id", 256),
        )
        object.__setattr__(self, "role_id", _require_token(self.role_id, "role_id", 128))
        role = _require_token(self.runtime_role, "runtime_role", 32)
        if role not in {"planner", "worker", "reviewer"}:
            raise ValueError("runtime_role must be planner, worker, or reviewer")
        object.__setattr__(self, "runtime_role", role)
        kind = _require_token(self.runtime_kind, "runtime_kind", 32)
        if kind not in {"hermes", "native"}:
            raise ValueError("runtime_kind must be hermes or native")
        object.__setattr__(self, "runtime_kind", kind)
        object.__setattr__(
            self,
            "configured_model_id",
            _require_model_id(self.configured_model_id, "configured_model_id"),
        )
        object.__setattr__(
            self,
            "task_kind",
            _require_optional_token(self.task_kind, "task_kind", 64),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime_request_id": self.runtime_request_id,
            "role_id": self.role_id,
            "runtime_role": self.runtime_role,
            "runtime_kind": self.runtime_kind,
            "configured_model_id": self.configured_model_id,
            "task_kind": self.task_kind,
        }


@dataclass(frozen=True, slots=True)
class ModelRouteRule:
    rule_id: str
    model_id: str
    role_id: str | None = None
    runtime_role: str | None = None
    runtime_kind: str | None = None
    task_kind: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _require_token(self.rule_id, "rule_id", 128))
        object.__setattr__(self, "model_id", _require_model_id(self.model_id, "model_id"))
        object.__setattr__(self, "role_id", _require_optional_token(self.role_id, "role_id", 128))
        role = _require_optional_token(self.runtime_role, "runtime_role", 32)
        if role is not None and role not in {"planner", "worker", "reviewer"}:
            raise ValueError("runtime_role rule selector is invalid")
        object.__setattr__(self, "runtime_role", role)
        kind = _require_optional_token(self.runtime_kind, "runtime_kind", 32)
        if kind is not None and kind not in {"hermes", "native"}:
            raise ValueError("runtime_kind rule selector is invalid")
        object.__setattr__(self, "runtime_kind", kind)
        object.__setattr__(self, "task_kind", _require_optional_token(self.task_kind, "task_kind", 64))
        if all(
            value is None
            for value in (self.role_id, self.runtime_role, self.runtime_kind, self.task_kind)
        ):
            raise ValueError("A model route rule must contain at least one selector")

    def matches(self, request: ModelRouteRequest) -> bool:
        return all(
            selector is None or selector == actual
            for selector, actual in (
                (self.role_id, request.role_id),
                (self.runtime_role, request.runtime_role),
                (self.runtime_kind, request.runtime_kind),
                (self.task_kind, request.task_kind),
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "model_id": self.model_id,
            "role_id": self.role_id,
            "runtime_role": self.runtime_role,
            "runtime_kind": self.runtime_kind,
            "task_kind": self.task_kind,
        }


@dataclass(frozen=True, slots=True)
class ModelRoutingPolicy:
    version: int
    rules: tuple[ModelRouteRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("Model routing policy version must be a positive integer")
        if not isinstance(self.rules, tuple) or len(self.rules) > _MAX_RULES:
            raise ValueError("Model routing policy rules must be a bounded tuple")
        if any(type(rule) is not ModelRouteRule for rule in self.rules):
            raise TypeError("Model routing policy contains an invalid rule")
        identifiers = [rule.rule_id for rule in self.rules]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Model routing policy rule IDs must be unique")

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "rules": [rule.as_dict() for rule in self.rules]}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelRouteDecision:
    selected_model_id: str
    policy_version: int
    policy_sha256: str
    rule_id: str
    reason: ModelRouteReason

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_model_id",
            _require_model_id(self.selected_model_id, "selected_model_id"),
        )
        if not isinstance(self.policy_version, int) or isinstance(self.policy_version, bool) or self.policy_version < 1:
            raise ValueError("Model route policy version is invalid")
        digest = _require_token(self.policy_sha256, "policy_sha256", 64)
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Model route policy digest is invalid")
        object.__setattr__(self, "policy_sha256", digest)
        object.__setattr__(self, "rule_id", _require_token(self.rule_id, "rule_id", 128))
        if type(self.reason) is not ModelRouteReason:
            raise TypeError("Model route reason is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected_model_id": self.selected_model_id,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "rule_id": self.rule_id,
            "reason": self.reason.value,
        }
