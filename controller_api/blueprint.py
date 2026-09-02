from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any, Iterable

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode


API_VERSION = "orchestra.dev/v1"
KIND = "SandboxProfile"
SOURCE_FORMAT = "blueprint-v1"
MAX_SOURCE_BYTES = 256 * 1024
MAX_SOURCE_LINES = 8192
MAX_NODES = 10000
MAX_DEPTH = 32
MAX_DIAGNOSTICS = 100
MAX_SCALAR_CHARS = 16384

NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.:~=@/_-]*$")
IMAGE_PATTERN = re.compile(r"^[a-z0-9]+(?:(?:[._-]|/{1,2})[a-z0-9]+)*$")
DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
DURATION_PATTERN = re.compile(r"^([1-9][0-9]*)(ms|s|m|h)$")
QUANTITY_PATTERN = re.compile(r"^([1-9][0-9]*)(?:\.([0-9]+))?(KiB|MiB|GiB|TiB)$")
HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTH|CREDENTIAL|PASSWORD|PRIVATE_?KEY|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"-{5}BEGIN [A-Z0-9 ]*PRIVATE KEY-{5}"),
    re.compile(r"(?i)\b(?:token|password|secret|api[_-]?key)\s*="),
    re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]+@"),
)
SHELL_NAMES = {
    "ash", "bash", "cmd", "dash", "fish", "ksh",
    "powershell", "pwsh", "sh", "zsh",
}
PROTECTED_PATHS = (
    PurePosixPath("/"),
    PurePosixPath("/bin"),
    PurePosixPath("/boot"),
    PurePosixPath("/dev"),
    PurePosixPath("/etc"),
    PurePosixPath("/lib"),
    PurePosixPath("/lib64"),
    PurePosixPath("/proc"),
    PurePosixPath("/root"),
    PurePosixPath("/run"),
    PurePosixPath("/sbin"),
    PurePosixPath("/sys"),
    PurePosixPath("/usr"),
    PurePosixPath("/var/run"),
)


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    path: str
    message: str
    documentation: str = "specs/blueprint-v1.schema.json"

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "documentation": self.documentation,
        }


@dataclass(frozen=True)
class BlueprintResult:
    name: str
    source_format: str
    api_version: str
    source_sha256: str
    canonical_sha256: str
    canonical: dict[str, Any]
    canonical_bytes: bytes
    diagnostics: tuple[Diagnostic, ...]

    def metadata(self) -> dict[str, Any]:
        return {
            "valid": True,
            "name": self.name,
            "source_format": self.source_format,
            "api_version": self.api_version,
            "source_sha256": self.source_sha256,
            "canonical_sha256": self.canonical_sha256,
            "canonical_size": len(self.canonical_bytes),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True)
class BlueprintReport:
    valid: bool
    diagnostics: tuple[Diagnostic, ...]
    result: BlueprintResult | None = None

    def as_dict(self, *, include_canonical: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "valid": self.valid,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }
        if self.result is not None:
            payload.update(self.result.metadata())
            if include_canonical:
                payload["canonical"] = self.result.canonical
        return payload


class _StrictLoader(yaml.SafeLoader):
    """YAML 1.2-oriented loader with duplicate-key and alias rejection."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "aliases are not supported",
                event.start_mark,
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[str, Any]:
        if not isinstance(node, MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "expected a mapping",
                node.start_mark,
            )
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be strings",
                    key_node.start_mark,
                )
            if key == "<<":
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "merge keys are not supported",
                    key_node.start_mark,
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "duplicate mapping key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


_StrictLoader.yaml_implicit_resolvers = copy.deepcopy(
    yaml.SafeLoader.yaml_implicit_resolvers
)
for first, resolvers in list(_StrictLoader.yaml_implicit_resolvers.items()):
    _StrictLoader.yaml_implicit_resolvers[first] = [
        entry
        for entry in resolvers
        if entry[0] not in {
            "tag:yaml.org,2002:bool",
            "tag:yaml.org,2002:timestamp",
        }
    ]
_StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


class _Validator:
    def __init__(self) -> None:
        self.diagnostics: list[Diagnostic] = []

    def add(self, code: str, path: str, message: str, *, severity: str = "error") -> None:
        if len(self.diagnostics) >= MAX_DIAGNOSTICS:
            return
        self.diagnostics.append(
            Diagnostic(
                severity=severity,
                code=code,
                path=path or "/",
                message=message,
            )
        )

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.diagnostics)

    @staticmethod
    def child(path: str, key: str | int) -> str:
        value = str(key).replace("~", "~0").replace("/", "~1")
        return f"{path}/{value}" if path else f"/{value}"

    def exact_keys(
        self,
        value: Any,
        path: str,
        *,
        required: Iterable[str],
        optional: Iterable[str] = (),
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.add("invalid_type", path, "Expected an object.")
            return None
        required_set = set(required)
        allowed = required_set | set(optional)
        for key in sorted(required_set - set(value)):
            self.add("required_field_missing", self.child(path, key), "Required field is missing.")
        for key in sorted(set(value) - allowed):
            self.add("unknown_field", self.child(path, key), "Unknown fields are not accepted.")
        return value

    def string(
        self,
        value: Any,
        path: str,
        *,
        minimum: int = 1,
        maximum: int,
        pattern: re.Pattern[str] | None = None,
        enum: set[str] | None = None,
        allow_newline: bool = False,
    ) -> str | None:
        if not isinstance(value, str):
            self.add("invalid_type", path, "Expected a string.")
            return None
        if not minimum <= len(value) <= maximum:
            self.add("invalid_length", path, "String length is outside the allowed bounds.")
            return None
        if any(
            ord(character) < 32
            and character not in (("\n", "\t") if allow_newline else ())
            for character in value
        ) or "\x7f" in value:
            self.add("control_character_forbidden", path, "Control characters are not accepted.")
        if not allow_newline and ("\n" in value or "\r" in value):
            self.add("multiline_value_forbidden", path, "Multiline values are not accepted here.")
        if pattern is not None and pattern.fullmatch(value) is None:
            self.add("invalid_format", path, "Value does not match the required format.")
        if enum is not None and value not in enum:
            self.add("unsupported_value", path, "Value is not supported by Blueprint v1.")
        return value

    def boolean(self, value: Any, path: str, *, expected: bool | None = None) -> bool | None:
        if type(value) is not bool:
            self.add("invalid_type", path, "Expected a boolean.")
            return None
        if expected is not None and value is not expected:
            self.add("security_invariant_violation", path, "This security value cannot be weakened.")
        return value

    def integer(
        self,
        value: Any,
        path: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int | None:
        if type(value) is not int:
            self.add("invalid_type", path, "Expected an integer.")
            return None
        if not minimum <= value <= maximum:
            self.add("invalid_numeric_range", path, "Numeric value is outside the allowed bounds.")
        return value

    def number(
        self,
        value: Any,
        path: str,
        *,
        minimum_exclusive: float,
        maximum: float,
    ) -> int | float | None:
        if type(value) not in {int, float}:
            self.add("invalid_type", path, "Expected a finite number.")
            return None
        if isinstance(value, float) and not math.isfinite(value):
            self.add("non_finite_number", path, "Non-finite numbers are not accepted.")
            return None
        if not minimum_exclusive < value <= maximum:
            self.add("invalid_numeric_range", path, "Numeric value is outside the allowed bounds.")
        return value

    def string_list(
        self,
        value: Any,
        path: str,
        *,
        minimum: int = 0,
        maximum: int,
        item_maximum: int,
        pattern: re.Pattern[str] | None = None,
    ) -> list[str] | None:
        if not isinstance(value, list):
            self.add("invalid_type", path, "Expected an array.")
            return None
        if not minimum <= len(value) <= maximum:
            self.add("invalid_item_count", path, "Array length is outside the allowed bounds.")
        result: list[str] = []
        for index, item in enumerate(value):
            child = self.child(path, index)
            parsed = self.string(
                item,
                child,
                maximum=item_maximum,
                pattern=pattern,
            )
            if parsed is not None:
                result.append(parsed)
        if len(result) == len(value) and len(set(result)) != len(result):
            self.add("duplicate_item", path, "Duplicate array items are not accepted.")
        return result


def _parse_duration(value: str) -> int:
    match = DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("invalid duration")
    number = int(match.group(1))
    multiplier = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}[match.group(2)]
    milliseconds = number * multiplier
    if milliseconds > 365 * 24 * 3_600_000:
        raise ValueError("duration too large")
    return milliseconds


def _canonical_duration(value: str) -> str:
    milliseconds = _parse_duration(value)
    for unit, multiplier in (
        ("h", 3_600_000),
        ("m", 60_000),
        ("s", 1000),
        ("ms", 1),
    ):
        if milliseconds % multiplier == 0:
            return f"{milliseconds // multiplier}{unit}"
    raise AssertionError("unreachable")


def _parse_quantity(value: str) -> int:
    match = QUANTITY_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("invalid quantity")
    whole, fractional, unit = match.groups()
    number = Decimal(whole + (f".{fractional}" if fractional else ""))
    multiplier = {
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }[unit]
    bytes_value = number * multiplier
    if bytes_value != bytes_value.to_integral_value():
        raise ValueError("fractional byte")
    result = int(bytes_value)
    if result <= 0 or result > 16 * 1024**4:
        raise ValueError("quantity out of range")
    return result


def _canonical_quantity(value: str) -> str:
    bytes_value = _parse_quantity(value)
    for unit, multiplier in (
        ("TiB", 1024**4),
        ("GiB", 1024**3),
        ("MiB", 1024**2),
        ("KiB", 1024),
    ):
        if bytes_value % multiplier == 0:
            return f"{bytes_value // multiplier}{unit}"
    raise ValueError("quantity cannot be represented canonically")


def _is_protected_path(path: PurePosixPath) -> bool:
    for item in PROTECTED_PATHS:
        if item == PurePosixPath("/"):
            if path == item:
                return True
            continue
        if path == item or item in path.parents:
            return True
    return False


def _container_path(value: str) -> PurePosixPath:
    if not value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError("non-canonical absolute path")
    pieces = value.split("/")[1:]
    if not pieces or any(piece in {"", ".", ".."} for piece in pieces):
        raise ValueError("unsafe path component")
    path = PurePosixPath(value)
    if _is_protected_path(path):
        raise ValueError("protected path")
    return path


def _paths_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    return left == right or left in right.parents or right in left.parents


def _network_entry(value: str) -> bool:
    if any(marker in value for marker in ("://", "@", "?", "#", "/", "\\")):
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
        return True
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        pass
    if len(value) > 253 or value.endswith(".") or value.startswith("."):
        return False
    labels = value.split(".")
    return bool(labels) and all(HOST_LABEL_PATTERN.fullmatch(label) for label in labels)


def _secret_like(value: str) -> bool:
    return "${" in value or any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def _validate_graph(value: Any, validator: _Validator, path: str = "", depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_NODES:
        validator.add("document_too_complex", "/", "Blueprint contains too many data nodes.")
        return
    if depth > MAX_DEPTH:
        validator.add("document_too_deep", path or "/", "Blueprint nesting is too deep.")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                validator.add("non_string_key", path or "/", "Mapping keys must be strings.")
                continue
            if len(key) > 256:
                validator.add("mapping_key_too_long", _Validator.child(path, key[:32]), "Mapping key is too long.")
            _validate_graph(item, validator, _Validator.child(path, key), depth + 1, counter)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_graph(item, validator, _Validator.child(path, index), depth + 1, counter)
    elif value is None or type(value) in {bool, int}:
        return
    elif type(value) is float:
        if not math.isfinite(value):
            validator.add("non_finite_number", path or "/", "Non-finite numbers are not accepted.")
    elif isinstance(value, str):
        if len(value) > MAX_SCALAR_CHARS:
            validator.add("scalar_too_large", path or "/", "Scalar value exceeds the allowed bound.")
        if "\x00" in value:
            validator.add("nul_character_forbidden", path or "/", "NUL characters are not accepted.")
    else:
        validator.add("unsupported_yaml_type", path or "/", "YAML value type is not supported.")


def _validate_command(
    validator: _Validator,
    value: Any,
    path: str,
    *,
    require_expect: bool,
) -> None:
    required = {"name", "run", "timeout"}
    if require_expect:
        required.add("expectExitCode")
    table = validator.exact_keys(
        value,
        path,
        required=required,
        optional={"workingDirectory"},
    )
    if table is None:
        return
    validator.string(table.get("name"), validator.child(path, "name"), maximum=63, pattern=NAME_PATTERN)
    argv = validator.string_list(
        table.get("run"),
        validator.child(path, "run"),
        minimum=1,
        maximum=64,
        item_maximum=4096,
    )
    if argv:
        executable = PurePosixPath(argv[0]).name.lower()
        shell_passthrough = executable in SHELL_NAMES
        if executable == "busybox" and len(argv) >= 2:
            shell_passthrough = PurePosixPath(argv[1]).name.lower() in SHELL_NAMES
        if executable == "env" and len(argv) >= 2:
            shell_passthrough = PurePosixPath(argv[1]).name.lower() in SHELL_NAMES
        if shell_passthrough:
            validator.add(
                "shell_execution_forbidden",
                validator.child(path, "run"),
                "Shell pass-through commands are not accepted in Blueprint v1.",
            )
        if sum(len(item.encode("utf-8")) for item in argv) > 32768:
            validator.add(
                "command_too_large",
                validator.child(path, "run"),
                "Command argument data exceeds the allowed bound.",
            )
    timeout = validator.string(
        table.get("timeout"),
        validator.child(path, "timeout"),
        maximum=32,
        pattern=DURATION_PATTERN,
    )
    if timeout is not None:
        try:
            _parse_duration(timeout)
        except ValueError:
            validator.add("invalid_duration", validator.child(path, "timeout"), "Duration is invalid or too large.")
    if "workingDirectory" in table:
        working = validator.string(
            table["workingDirectory"],
            validator.child(path, "workingDirectory"),
            maximum=4096,
        )
        if working is not None:
            try:
                _container_path(working)
            except ValueError:
                validator.add(
                    "unsafe_container_path",
                    validator.child(path, "workingDirectory"),
                    "Working directory must be a safe canonical container path.",
                )
    if require_expect:
        validator.integer(
            table.get("expectExitCode"),
            validator.child(path, "expectExitCode"),
            minimum=0,
            maximum=255,
        )


def _validate_string_packages(
    validator: _Validator,
    value: Any,
    path: str,
    *,
    ecosystem: str,
) -> None:
    packages = validator.string_list(
        value,
        path,
        maximum=256,
        item_maximum=300,
        pattern=PACKAGE_PATTERN,
    )
    if not packages:
        return
    for index, package in enumerate(packages):
        pinned = True
        if ecosystem == "apt":
            pinned = "=" in package
        elif ecosystem == "python":
            pinned = "==" in package or " @ " in package
        elif ecosystem == "node":
            tail = package.rsplit("/", 1)[-1]
            pinned = "@" in tail[1:] if tail.startswith("@") else "@" in tail
        if not pinned:
            validator.add(
                "package_not_version_pinned",
                validator.child(path, index),
                "Package is not version-pinned; the resolved build record must preserve its exact version.",
                severity="warning",
            )


def _validate_document(document: Any) -> _Validator:
    validator = _Validator()
    _validate_graph(document, validator)
    root = validator.exact_keys(
        document,
        "",
        required={"apiVersion", "kind", "metadata", "spec"},
    )
    if root is None:
        return validator

    api_version = validator.string(root.get("apiVersion"), "/apiVersion", maximum=64)
    if api_version is not None and api_version != API_VERSION:
        validator.add(
            "unsupported_api_version",
            "/apiVersion",
            "Blueprint must use apiVersion orchestra.dev/v1.",
        )
    kind = validator.string(root.get("kind"), "/kind", maximum=64)
    if kind is not None and kind != KIND:
        validator.add("unsupported_kind", "/kind", "Blueprint v1 accepts SandboxProfile only.")

    metadata = validator.exact_keys(
        root.get("metadata"),
        "/metadata",
        required={"name"},
        optional={"displayName", "description", "labels"},
    )
    if metadata is not None:
        validator.string(metadata.get("name"), "/metadata/name", maximum=63, pattern=NAME_PATTERN)
        if "displayName" in metadata:
            validator.string(metadata["displayName"], "/metadata/displayName", maximum=120)
        if "description" in metadata:
            validator.string(
                metadata["description"],
                "/metadata/description",
                minimum=0,
                maximum=1000,
                allow_newline=True,
            )
        if "labels" in metadata:
            labels = metadata["labels"]
            if not isinstance(labels, dict):
                validator.add("invalid_type", "/metadata/labels", "Expected an object.")
            else:
                if len(labels) > 64:
                    validator.add("too_many_labels", "/metadata/labels", "Too many labels.")
                for key, value in labels.items():
                    validator.string(key, _Validator.child("/metadata/labels", key), maximum=63, pattern=LABEL_PATTERN)
                    validator.string(value, _Validator.child("/metadata/labels", key), minimum=0, maximum=120)

    spec = validator.exact_keys(
        root.get("spec"),
        "/spec",
        required={"base", "workspace", "runtime", "network", "security", "validation"},
        optional={"build", "mounts"},
    )
    if spec is None:
        return validator

    base = validator.exact_keys(
        spec.get("base"),
        "/spec/base",
        required={"image", "digest"},
        optional={"registry", "tag"},
    )
    if base is not None:
        if "registry" in base:
            registry = validator.string(base["registry"], "/spec/base/registry", maximum=253)
            if registry is not None and (
                "://" in registry or "@" in registry or "/" in registry or _secret_like(registry)
            ):
                validator.add("invalid_registry", "/spec/base/registry", "Registry must be a credential-free registry host.")
        validator.string(base.get("image"), "/spec/base/image", maximum=255, pattern=IMAGE_PATTERN)
        if "tag" in base:
            validator.string(base["tag"], "/spec/base/tag", maximum=128)
        validator.string(base.get("digest"), "/spec/base/digest", maximum=71, pattern=DIGEST_PATTERN)

    build = spec.get("build", {})
    build_table = validator.exact_keys(
        build,
        "/spec/build",
        required=set(),
        optional={"apt", "python", "node", "environment", "steps"},
    )
    if build_table is not None:
        if "apt" in build_table:
            apt = validator.exact_keys(
                build_table["apt"],
                "/spec/build/apt",
                required=set(),
                optional={"update", "packages"},
            )
            if apt is not None:
                if "update" in apt:
                    validator.boolean(apt["update"], "/spec/build/apt/update")
                if "packages" in apt:
                    _validate_string_packages(validator, apt["packages"], "/spec/build/apt/packages", ecosystem="apt")
        if "python" in build_table:
            python = validator.exact_keys(
                build_table["python"],
                "/spec/build/python",
                required=set(),
                optional={"interpreter", "packages"},
            )
            if python is not None:
                if "interpreter" in python:
                    validator.string(
                        python["interpreter"],
                        "/spec/build/python/interpreter",
                        maximum=16,
                        enum={"python3", "python"},
                    )
                if "packages" in python:
                    _validate_string_packages(validator, python["packages"], "/spec/build/python/packages", ecosystem="python")
        if "node" in build_table:
            node = validator.exact_keys(
                build_table["node"],
                "/spec/build/node",
                required=set(),
                optional={"packageManager", "packages"},
            )
            if node is not None:
                if "packageManager" in node:
                    validator.string(
                        node["packageManager"],
                        "/spec/build/node/packageManager",
                        maximum=16,
                        enum={"npm", "pnpm", "yarn"},
                    )
                if "packages" in node:
                    _validate_string_packages(validator, node["packages"], "/spec/build/node/packages", ecosystem="node")
        if "environment" in build_table:
            environment = build_table["environment"]
            if not isinstance(environment, dict):
                validator.add("invalid_type", "/spec/build/environment", "Expected an object.")
            else:
                if len(environment) > 128:
                    validator.add("too_many_environment_variables", "/spec/build/environment", "Too many environment variables.")
                for key, value in environment.items():
                    path = _Validator.child("/spec/build/environment", key)
                    validator.string(key, path, maximum=128, pattern=ENV_PATTERN)
                    parsed = validator.string(value, path, minimum=0, maximum=4096, allow_newline=True)
                    if SECRET_KEY_PATTERN.search(key):
                        validator.add("secret_environment_key_forbidden", path, "Secret-like environment keys are forbidden in Blueprint v1.")
                    if parsed is not None and _secret_like(parsed):
                        validator.add("secret_like_value_forbidden", path, "Secret references and credential-like values are forbidden.")
        if "steps" in build_table:
            steps = build_table["steps"]
            if not isinstance(steps, list):
                validator.add("invalid_type", "/spec/build/steps", "Expected an array.")
            else:
                if len(steps) > 128:
                    validator.add("invalid_item_count", "/spec/build/steps", "Too many build steps.")
                names: list[str] = []
                for index, step in enumerate(steps):
                    _validate_command(validator, step, _Validator.child("/spec/build/steps", index), require_expect=False)
                    if isinstance(step, dict) and isinstance(step.get("name"), str):
                        names.append(step["name"])
                if len(names) != len(set(names)):
                    validator.add("duplicate_step_name", "/spec/build/steps", "Build step names must be unique.")

    workspace = validator.exact_keys(
        spec.get("workspace"),
        "/spec/workspace",
        required={"user", "group", "directory", "sourceMode"},
    )
    workspace_path: PurePosixPath | None = None
    if workspace is not None:
        for key in ("user", "group"):
            parsed = validator.string(
                workspace.get(key),
                f"/spec/workspace/{key}",
                maximum=32,
                pattern=USER_PATTERN,
            )
            if parsed == "root":
                validator.add("root_identity_forbidden", f"/spec/workspace/{key}", "Sandbox identity must not be root.")
        directory = validator.string(workspace.get("directory"), "/spec/workspace/directory", maximum=4096)
        if directory is not None:
            try:
                workspace_path = _container_path(directory)
            except ValueError:
                validator.add("unsafe_container_path", "/spec/workspace/directory", "Workspace directory must be a safe canonical container path.")
        validator.string(
            workspace.get("sourceMode"),
            "/spec/workspace/sourceMode",
            maximum=16,
            enum={"worktree", "clone", "readOnly"},
        )

    runtime = validator.exact_keys(
        spec.get("runtime"),
        "/spec/runtime",
        required={"cpu", "memory", "pids", "timeout", "stopGracePeriod"},
        optional={"tmpfsSize"},
    )
    if runtime is not None:
        validator.number(runtime.get("cpu"), "/spec/runtime/cpu", minimum_exclusive=0, maximum=256)
        for key in ("memory", "tmpfsSize"):
            if key not in runtime:
                continue
            quantity = validator.string(runtime[key], f"/spec/runtime/{key}", maximum=32, pattern=QUANTITY_PATTERN)
            if quantity is not None:
                try:
                    _parse_quantity(quantity)
                except (ValueError, InvalidOperation):
                    validator.add("invalid_quantity", f"/spec/runtime/{key}", "IEC quantity is invalid or outside the allowed bound.")
        validator.integer(runtime.get("pids"), "/spec/runtime/pids", minimum=16, maximum=65536)
        for key in ("timeout", "stopGracePeriod"):
            duration = validator.string(runtime.get(key), f"/spec/runtime/{key}", maximum=32, pattern=DURATION_PATTERN)
            if duration is not None:
                try:
                    _parse_duration(duration)
                except ValueError:
                    validator.add("invalid_duration", f"/spec/runtime/{key}", "Duration is invalid or too large.")

    network = validator.exact_keys(
        spec.get("network"),
        "/spec/network",
        required={"build", "runtime"},
    )
    if network is not None:
        for phase in ("build", "runtime"):
            path = f"/spec/network/{phase}"
            policy = validator.exact_keys(network.get(phase), path, required={"mode", "allow"})
            if policy is None:
                continue
            mode = validator.string(policy.get("mode"), f"{path}/mode", maximum=16, enum={"none", "allowlist", "full"})
            allow = validator.string_list(policy.get("allow"), f"{path}/allow", maximum=256, item_maximum=253)
            if mode == "none" and allow:
                validator.add("network_allowlist_forbidden", f"{path}/allow", "Network mode none requires an empty allow list.")
            if mode == "allowlist" and not allow:
                validator.add("network_allowlist_required", f"{path}/allow", "Allowlist mode requires at least one destination.")
            if mode == "full":
                validator.add(
                    "full_network_requires_policy",
                    f"{path}/mode",
                    "Full network access requires external policy approval.",
                    severity="warning",
                )
            for index, entry in enumerate(allow or []):
                if not _network_entry(entry) or _secret_like(entry):
                    validator.add("invalid_network_destination", _Validator.child(f"{path}/allow", index), "Network destination must be a credential-free DNS name or CIDR.")

    security = validator.exact_keys(
        spec.get("security"),
        "/spec/security",
        required={
            "privileged", "noNewPrivileges", "readOnlyRoot", "capabilities",
            "seccompProfile", "secrets", "allowDockerSocket", "allowDeviceAccess",
        },
    )
    if security is not None:
        validator.boolean(security.get("privileged"), "/spec/security/privileged", expected=False)
        validator.boolean(security.get("noNewPrivileges"), "/spec/security/noNewPrivileges", expected=True)
        validator.boolean(security.get("readOnlyRoot"), "/spec/security/readOnlyRoot")
        validator.boolean(security.get("secrets"), "/spec/security/secrets", expected=False)
        validator.boolean(security.get("allowDockerSocket"), "/spec/security/allowDockerSocket", expected=False)
        validator.boolean(security.get("allowDeviceAccess"), "/spec/security/allowDeviceAccess", expected=False)
        validator.string(security.get("seccompProfile"), "/spec/security/seccompProfile", maximum=32, enum={"default"})
        capabilities = validator.exact_keys(
            security.get("capabilities"),
            "/spec/security/capabilities",
            required={"drop", "add"},
        )
        if capabilities is not None:
            drop = validator.string_list(capabilities.get("drop"), "/spec/security/capabilities/drop", minimum=1, maximum=64, item_maximum=64)
            add = validator.string_list(capabilities.get("add"), "/spec/security/capabilities/add", maximum=0, item_maximum=64)
            if drop is not None and "ALL" not in drop:
                validator.add("security_invariant_violation", "/spec/security/capabilities/drop", "Capability drop list must contain ALL.")
            if add:
                validator.add("security_invariant_violation", "/spec/security/capabilities/add", "Added Linux capabilities are forbidden.")

    mounts = spec.get("mounts", [])
    mount_paths: list[tuple[int, str, PurePosixPath]] = []
    if not isinstance(mounts, list):
        validator.add("invalid_type", "/spec/mounts", "Expected an array.")
    else:
        if len(mounts) > 64:
            validator.add("invalid_item_count", "/spec/mounts", "Too many logical mounts.")
        names: list[str] = []
        workspace_mount_count = 0
        for index, mount in enumerate(mounts):
            path = _Validator.child("/spec/mounts", index)
            table = validator.exact_keys(
                mount,
                path,
                required={"name", "type", "target", "readOnly"},
                optional={"size"},
            )
            if table is None:
                continue
            name = validator.string(table.get("name"), f"{path}/name", maximum=63, pattern=NAME_PATTERN)
            if name is not None:
                names.append(name)
            mount_type = validator.string(table.get("type"), f"{path}/type", maximum=16, enum={"workspace", "cache", "tmpfs", "artifact"})
            target = validator.string(table.get("target"), f"{path}/target", maximum=4096)
            validator.boolean(table.get("readOnly"), f"{path}/readOnly")
            if "size" in table:
                size = validator.string(table["size"], f"{path}/size", maximum=32, pattern=QUANTITY_PATTERN)
                if size is not None:
                    try:
                        _parse_quantity(size)
                    except (ValueError, InvalidOperation):
                        validator.add("invalid_quantity", f"{path}/size", "IEC quantity is invalid or outside the allowed bound.")
            if mount_type == "tmpfs" and "size" not in table:
                validator.add("tmpfs_size_required", f"{path}/size", "tmpfs mounts require a bounded size.")
            if mount_type == "workspace":
                workspace_mount_count += 1
            if target is not None:
                try:
                    parsed_target = _container_path(target)
                    mount_paths.append((index, mount_type or "", parsed_target))
                except ValueError:
                    validator.add("unsafe_container_path", f"{path}/target", "Mount target must be a safe canonical container path.")
        if len(names) != len(set(names)):
            validator.add("duplicate_mount_name", "/spec/mounts", "Mount names must be unique.")
        if workspace_mount_count > 1:
            validator.add("duplicate_workspace_mount", "/spec/mounts", "At most one explicit workspace mount is accepted.")
        for left_index, left_type, left in mount_paths:
            if left_type == "workspace" and workspace_path is not None and left != workspace_path:
                validator.add(
                    "workspace_mount_mismatch",
                    f"/spec/mounts/{left_index}/target",
                    "Workspace mount target must match spec.workspace.directory.",
                )
            for right_index, _, right in mount_paths:
                if right_index <= left_index:
                    continue
                if _paths_overlap(left, right):
                    validator.add(
                        "overlapping_mount_targets",
                        f"/spec/mounts/{right_index}/target",
                        "Logical mount targets must not overlap.",
                    )
            if workspace_path is not None and left_type != "workspace" and _paths_overlap(left, workspace_path):
                validator.add(
                    "workspace_mount_overlap",
                    f"/spec/mounts/{left_index}/target",
                    "Additional mounts must not overlap the workspace directory.",
                )

    validation = validator.exact_keys(
        spec.get("validation"),
        "/spec/validation",
        required={"commands"},
    )
    if validation is not None:
        commands = validation.get("commands")
        if not isinstance(commands, list):
            validator.add("invalid_type", "/spec/validation/commands", "Expected an array.")
        else:
            if not 1 <= len(commands) <= 128:
                validator.add("invalid_item_count", "/spec/validation/commands", "Validation command count must be between 1 and 128.")
            names: list[str] = []
            for index, command in enumerate(commands):
                path = _Validator.child("/spec/validation/commands", index)
                _validate_command(validator, command, path, require_expect=True)
                if isinstance(command, dict) and isinstance(command.get("name"), str):
                    names.append(command["name"])
            if len(names) != len(set(names)):
                validator.add("duplicate_validation_name", "/spec/validation/commands", "Validation command names must be unique.")

    return validator


def _canonicalize(document: dict[str, Any]) -> dict[str, Any]:
    canonical = copy.deepcopy(document)
    spec = canonical["spec"]

    base = spec["base"]
    base.setdefault("registry", "docker.io")

    build = spec.setdefault("build", {})
    build.setdefault("environment", {})
    build.setdefault("steps", [])
    if "apt" in build:
        build["apt"].setdefault("update", True)
        build["apt"].setdefault("packages", [])
    if "python" in build:
        build["python"].setdefault("interpreter", "python3")
        build["python"].setdefault("packages", [])
    if "node" in build:
        build["node"].setdefault("packageManager", "npm")
        build["node"].setdefault("packages", [])

    spec.setdefault("mounts", [])

    runtime = spec["runtime"]
    runtime["memory"] = _canonical_quantity(runtime["memory"])
    if "tmpfsSize" in runtime:
        runtime["tmpfsSize"] = _canonical_quantity(runtime["tmpfsSize"])
    runtime["timeout"] = _canonical_duration(runtime["timeout"])
    runtime["stopGracePeriod"] = _canonical_duration(runtime["stopGracePeriod"])
    if isinstance(runtime["cpu"], float) and runtime["cpu"].is_integer():
        runtime["cpu"] = int(runtime["cpu"])

    for step in build["steps"]:
        step["timeout"] = _canonical_duration(step["timeout"])
    for command in spec["validation"]["commands"]:
        command["timeout"] = _canonical_duration(command["timeout"])
    for mount in spec["mounts"]:
        if "size" in mount:
            mount["size"] = _canonical_quantity(mount["size"])

    return canonical


def validate_source(source: bytes | str) -> BlueprintReport:
    raw = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    diagnostics: list[Diagnostic] = []
    if len(raw) > MAX_SOURCE_BYTES:
        diagnostics.append(Diagnostic("error", "source_too_large", "/", "Blueprint source exceeds 256 KiB."))
        return BlueprintReport(False, tuple(diagnostics))
    if raw.count(b"\n") + 1 > MAX_SOURCE_LINES:
        diagnostics.append(Diagnostic("error", "source_too_many_lines", "/", "Blueprint source has too many lines."))
        return BlueprintReport(False, tuple(diagnostics))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        diagnostics.append(Diagnostic("error", "invalid_utf8", "/", "Blueprint source must be valid UTF-8."))
        return BlueprintReport(False, tuple(diagnostics))
    if text.startswith("\ufeff"):
        diagnostics.append(Diagnostic("error", "utf8_bom_forbidden", "/", "UTF-8 BOM is not accepted."))
        return BlueprintReport(False, tuple(diagnostics))
    if "\x00" in text:
        diagnostics.append(Diagnostic("error", "nul_character_forbidden", "/", "NUL characters are not accepted."))
        return BlueprintReport(False, tuple(diagnostics))
    try:
        documents = list(yaml.load_all(text, Loader=_StrictLoader))
    except yaml.YAMLError as error:
        path = "/"
        mark = getattr(error, "problem_mark", None)
        message = "Blueprint YAML could not be parsed safely."
        if mark is not None:
            message = f"Blueprint YAML is invalid at line {mark.line + 1}, column {mark.column + 1}."
        diagnostics.append(Diagnostic("error", "yaml_parse_failed", path, message))
        return BlueprintReport(False, tuple(diagnostics))
    if len(documents) != 1:
        diagnostics.append(Diagnostic("error", "multiple_yaml_documents", "/", "Exactly one YAML document is required."))
        return BlueprintReport(False, tuple(diagnostics))
    document = documents[0]
    validator = _validate_document(document)
    diagnostics.extend(validator.diagnostics)
    if validator.has_errors:
        return BlueprintReport(False, tuple(diagnostics))
    assert isinstance(document, dict)
    canonical = _canonicalize(document)
    canonical_bytes = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    result = BlueprintResult(
        name=str(canonical["metadata"]["name"]),
        source_format=SOURCE_FORMAT,
        api_version=API_VERSION,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha256,
        canonical=canonical,
        canonical_bytes=canonical_bytes,
        diagnostics=tuple(diagnostics),
    )
    return BlueprintReport(True, tuple(diagnostics), result)


def validate_path(path: str | Any) -> BlueprintReport:
    from pathlib import Path

    source_path = Path(path)
    try:
        if source_path.is_symlink() or not source_path.is_file():
            raise OSError("not a regular file")
        raw = source_path.read_bytes()
    except OSError:
        return BlueprintReport(
            False,
            (
                Diagnostic(
                    "error",
                    "source_unavailable",
                    "/",
                    "Blueprint source must be a readable regular file and not a symlink.",
                ),
            ),
        )
    return validate_source(raw)
