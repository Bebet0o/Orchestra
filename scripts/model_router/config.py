"""Strict production configuration loader for deterministic model routing."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .contract import ModelRouteRule, ModelRouterError, ModelRoutingPolicy


_SECTION_FIELDS = {"version", "rules"}
_RULE_FIELDS = {"id", "model", "role_id", "runtime_role", "runtime_kind", "task_kind"}


def _optional_string(table: dict[str, Any], key: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelRouterError(f"model_router rule {key} must be a string")
    return value


def load_model_routing_policy(path: Path) -> ModelRoutingPolicy:
    """Load one bounded routing policy from Orchestra's TOML configuration."""
    if not isinstance(path, Path):
        raise TypeError("Model routing configuration path must be a Path")
    if not path.is_file():
        raise ModelRouterError(f"Model routing configuration is absent: {path}")
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ModelRouterError("Model routing configuration is unreadable") from error

    section = document.get("model_router", {})
    if not isinstance(section, dict):
        raise ModelRouterError("model_router configuration must be a table")
    unknown = set(section) - _SECTION_FIELDS
    if unknown:
        raise ModelRouterError(
            f"Unknown model_router fields: {sorted(unknown)!r}"
        )

    version = section.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ModelRouterError("model_router version must be an integer")
    raw_rules = section.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ModelRouterError("model_router rules must be an array")

    rules: list[ModelRouteRule] = []
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise ModelRouterError(f"model_router rule {index} must be a table")
        unknown_rule = set(raw) - _RULE_FIELDS
        if unknown_rule:
            raise ModelRouterError(
                f"Unknown model_router rule fields: {sorted(unknown_rule)!r}"
            )
        if "id" not in raw or "model" not in raw:
            raise ModelRouterError("model_router rules require id and model")
        try:
            rule = ModelRouteRule(
                rule_id=raw["id"],
                model_id=raw["model"],
                role_id=_optional_string(raw, "role_id"),
                runtime_role=_optional_string(raw, "runtime_role"),
                runtime_kind=_optional_string(raw, "runtime_kind"),
                task_kind=_optional_string(raw, "task_kind"),
            )
        except (TypeError, ValueError) as error:
            raise ModelRouterError(f"Invalid model_router rule at index {index}") from error
        rules.append(rule)

    try:
        return ModelRoutingPolicy(version=version, rules=tuple(rules))
    except (TypeError, ValueError) as error:
        raise ModelRouterError("Invalid model_router policy") from error
