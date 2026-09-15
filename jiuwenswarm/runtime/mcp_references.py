# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only, transport-neutral validation of Runtime MCP references."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias


_MCP_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

McpConfigLoader: TypeAlias = Callable[[], Iterable[Mapping[str, Any]]]
McpStateLoader: TypeAlias = Callable[[], Mapping[str, Any]]


class McpReferenceStatus(str, Enum):
    """Safe, SDK-facing readiness state for one MCP reference."""

    READY = "ready"
    NOT_READY = "not_ready"
    MISSING = "missing"


class McpReferenceValidationError(ValueError):
    """Raised when a caller supplies a malformed MCP reference list."""

    code = "MCP_BAD_REQUEST"
    retryable = False


class McpReferenceInventoryError(RuntimeError):
    """Raised when the local, read-only MCP inventory cannot be loaded."""

    code = "MCP_INTERNAL"
    retryable = True


@dataclass(frozen=True, slots=True, kw_only=True)
class McpReferenceValidationInput:
    """Normalized, deduplicated references for one validation operation."""

    references: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.references, tuple):
            raise TypeError("references must be a tuple")
        seen: set[str] = set()
        for index, name in enumerate(self.references):
            if (
                not isinstance(name, str)
                or _MCP_REFERENCE_PATTERN.fullmatch(name) is None
            ):
                raise McpReferenceValidationError(
                    f"MCP reference at index {index} has an invalid name"
                )
            if name in seen:
                raise McpReferenceValidationError("MCP references must be unique")
            seen.add(name)

    @classmethod
    def from_iterable(
        cls,
        references: Iterable[str],
    ) -> McpReferenceValidationInput:
        """Materialize *references* exactly once and validate every name."""

        if isinstance(references, (str, bytes)):
            raise McpReferenceValidationError(
                "MCP references must be an iterable of names"
            )
        try:
            materialized = tuple(references)
        except TypeError as exc:
            raise McpReferenceValidationError(
                "MCP references must be an iterable of names"
            ) from exc

        normalized: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(materialized):
            if not isinstance(value, str):
                raise McpReferenceValidationError(
                    f"MCP reference at index {index} must be a string"
                )
            name = value.strip()
            if _MCP_REFERENCE_PATTERN.fullmatch(name) is None:
                raise McpReferenceValidationError(
                    f"MCP reference at index {index} has an invalid name"
                )
            if name not in seen:
                normalized.append(name)
                seen.add(name)
        return cls(references=tuple(normalized))


@dataclass(frozen=True, slots=True, kw_only=True)
class McpReferenceResult:
    """Secret-free status of one requested MCP reference."""

    name: str
    status: McpReferenceStatus

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _MCP_REFERENCE_PATTERN.fullmatch(self.name) is None
        ):
            raise ValueError("result name is invalid")
        if not isinstance(self.status, McpReferenceStatus):
            raise TypeError("result status must be a McpReferenceStatus")

    def to_dict(self) -> dict[str, str]:
        """Return the safe machine representation of this result."""

        return {"name": self.name, "status": self.status.value}


@dataclass(frozen=True, slots=True, kw_only=True)
class McpReferenceValidationResult:
    """Aggregate result preserving the caller's first-occurrence order."""

    references: tuple[McpReferenceResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.references, tuple):
            raise TypeError("references must be a tuple")
        if not all(isinstance(item, McpReferenceResult) for item in self.references):
            raise TypeError("references must contain McpReferenceResult values")

    @property
    def valid(self) -> bool:
        """Return whether every requested reference is ready."""

        return all(item.status is McpReferenceStatus.READY for item in self.references)

    def names_with_status(self, status: McpReferenceStatus) -> tuple[str, ...]:
        """Return names carrying *status* in request order."""

        if not isinstance(status, McpReferenceStatus):
            raise TypeError("status must be a McpReferenceStatus")
        return tuple(item.name for item in self.references if item.status is status)

    def to_dict(self) -> dict[str, object]:
        """Return a secret-free, JSON-compatible representation."""

        return {
            "valid": self.valid,
            "references": [item.to_dict() for item in self.references],
        }


def _load_config_servers() -> Iterable[Mapping[str, Any]]:
    from jiuwenswarm.common.config import (  # pylint: disable=import-outside-toplevel
        get_config_yaml_mcp_servers,
    )

    return get_config_yaml_mcp_servers()


def _load_runtime_state() -> Mapping[str, Any]:
    import json  # pylint: disable=import-outside-toplevel

    from jiuwenswarm.common.utils import (  # pylint: disable=import-outside-toplevel
        get_workspace_dir,
    )

    state_path = get_workspace_dir() / "mcp" / "state.json"
    try:
        with state_path.open("r", encoding="utf-8-sig") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return {"mcp": {}}
    if not isinstance(state, Mapping):
        raise ValueError("invalid MCP state inventory")
    return state


def _safe_inventory_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if _MCP_REFERENCE_PATTERN.fullmatch(name) is None:
        return None
    return name


def _read_inventory(
    config_loader: McpConfigLoader,
    state_loader: McpStateLoader,
) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    try:
        config_servers = tuple(config_loader())
        runtime_state = state_loader()
    except Exception:  # noqa: BLE001 - stable Runtime inventory boundary
        raise McpReferenceInventoryError(
            "MCP reference inventory is unavailable"
        ) from None
    if not isinstance(runtime_state, Mapping):
        raise McpReferenceInventoryError("MCP reference inventory is unavailable")
    return config_servers, runtime_state


def _inventory_statuses(
    config_servers: tuple[Mapping[str, Any], ...],
    runtime_state: Mapping[str, Any],
) -> dict[str, McpReferenceStatus]:
    statuses: dict[str, McpReferenceStatus] = {}
    for entry in config_servers:
        if not isinstance(entry, Mapping):
            continue
        name = _safe_inventory_name(entry.get("name"))
        if name is not None:
            statuses.setdefault(name, McpReferenceStatus.READY)

    state_records = runtime_state.get("mcp", {})
    if not isinstance(state_records, Mapping):
        raise McpReferenceInventoryError("MCP reference inventory is unavailable")
    for raw_name, record in state_records.items():
        name = _safe_inventory_name(raw_name)
        if name is None:
            continue
        state = record.get("state") if isinstance(record, Mapping) else None
        statuses[name] = (
            McpReferenceStatus.READY
            if isinstance(state, str) and state == "connected"
            else McpReferenceStatus.NOT_READY
        )
    return statuses


def validate_mcp_references(
    references: Iterable[str],
    *,
    config_loader: McpConfigLoader | None = None,
    state_loader: McpStateLoader | None = None,
) -> McpReferenceValidationResult:
    """Validate MCP names against local configuration and Runtime state.

    This operation is deliberately read-only. It never probes a connection,
    starts an MCP process, resolves credentials, or mutates Runtime state.
    A Runtime state record overrides a same-named YAML entry. Only
    ``state == "connected"`` is ready; any other persisted state is known but
    not ready. A YAML-only entry is ready for explicit use regardless of its
    TUI-only ``enabled`` setting.
    """

    validation_input = McpReferenceValidationInput.from_iterable(references)
    if not validation_input.references:
        return McpReferenceValidationResult(references=())
    config_servers, runtime_state = _read_inventory(
        config_loader if config_loader is not None else _load_config_servers,
        state_loader if state_loader is not None else _load_runtime_state,
    )
    statuses = _inventory_statuses(config_servers, runtime_state)
    results = tuple(
        McpReferenceResult(
            name=name,
            status=statuses.get(name, McpReferenceStatus.MISSING),
        )
        for name in validation_input.references
    )
    return McpReferenceValidationResult(references=results)


__all__ = [
    "McpConfigLoader",
    "McpReferenceInventoryError",
    "McpReferenceResult",
    "McpReferenceStatus",
    "McpReferenceValidationError",
    "McpReferenceValidationInput",
    "McpReferenceValidationResult",
    "McpStateLoader",
    "validate_mcp_references",
]
