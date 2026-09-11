# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only, transport-neutral MCP catalog contracts.

This module deliberately accepts already-loaded configuration mappings.  It
does not read files, resolve credentials, create a Session or Agent, probe an
MCP endpoint, or touch the process-wide MCP ``Runner`` registry.  Runtime hosts
can therefore provide their existing configuration source while keeping the
public result independent from WebSocket and command wire payloads.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

_MCP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_KNOWN_TRANSPORTS = frozenset({"stdio", "sse", "http", "streamable-http"})
_KNOWN_STATES = frozenset(
    {"configured", "registered", "connecting", "connected", "disconnected"}
)
_KNOWN_INTEGRATION_TYPES = frozenset({"remote-mcp", "stdio-mcp", "cli", "skill-only"})


class McpCatalogError(ValueError):
    """Stable MCP catalog failure with no transport-specific representation."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class McpCatalogListInput:
    """Options for reading configured MCP server descriptors."""

    enabled_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class McpCatalogShowInput:
    """The configured MCP server to inspect."""

    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class McpServerDescriptor:
    """Safe MCP facts; commands, paths, endpoints and credentials are absent."""

    name: str
    transport: str
    default_enabled: bool
    connection_state: str
    integration_type: str = ""
    timeout_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the complete safe public descriptor shape."""
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class McpCatalogListResult:
    """An immutable, deterministic snapshot of configured MCP servers."""

    servers: tuple[McpServerDescriptor, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the immutable result without source mappings."""
        return {"servers": [server.to_dict() for server in self.servers]}


@dataclass(frozen=True, slots=True, kw_only=True)
class McpCatalogShowResult:
    """One safe configured MCP server descriptor."""

    server: McpServerDescriptor

    def to_dict(self) -> dict[str, Any]:
        """Serialize the safe public descriptor only."""
        return {"server": self.server.to_dict()}


def _normalize_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or _MCP_NAME_PATTERN.fullmatch(name) is None:
        return ""
    return name


def _normalize_transport(value: Any) -> str:
    transport = (
        value.strip().lower().replace("_", "-") if isinstance(value, str) else ""
    )
    if transport not in _KNOWN_TRANSPORTS:
        return "unknown"
    return transport


def _normalize_state(entry: Mapping[str, Any]) -> str:
    value = entry.get("connection_state", entry.get("state", "configured"))
    state = value.strip().lower() if isinstance(value, str) else ""
    return state if state in _KNOWN_STATES else "unknown"


def _normalize_integration_type(value: Any) -> str:
    integration_type = (
        value.strip().lower().replace("_", "-") if isinstance(value, str) else ""
    )
    if integration_type not in _KNOWN_INTEGRATION_TYPES:
        return ""
    return integration_type


def _normalize_timeout(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, float) or not math.isfinite(value):
        return None
    seconds = int(value)
    return seconds if seconds > 0 else None


def _descriptor(entry: Mapping[str, Any]) -> McpServerDescriptor | None:
    name = _normalize_name(entry.get("name"))
    if not name:
        return None
    return McpServerDescriptor(
        name=name,
        transport=_normalize_transport(entry.get("transport")),
        default_enabled=bool(entry.get("enabled", True)),
        connection_state=_normalize_state(entry),
        integration_type=_normalize_integration_type(entry.get("integration_type")),
        timeout_seconds=_normalize_timeout(
            entry.get("timeout_s", entry.get("timeout"))
        ),
    )


def list_mcp_servers(
    entries: Iterable[Mapping[str, Any]],
    catalog_input: McpCatalogListInput | None = None,
) -> McpCatalogListResult:
    """Build a safe catalog without retaining or exposing source mappings."""

    options = catalog_input or McpCatalogListInput()
    by_name: dict[str, McpServerDescriptor] = {}
    for entry in entries:
        descriptor = _descriptor(entry)
        if descriptor is None:
            continue
        if options.enabled_only and not descriptor.default_enabled:
            continue
        # The established merged MCP source gives state entries precedence over
        # legacy config entries.  Last-value-wins preserves that order while
        # still protecting callers that accidentally supply duplicates.
        by_name[descriptor.name] = descriptor
    return McpCatalogListResult(
        servers=tuple(
            sorted(by_name.values(), key=lambda item: (item.name.casefold(), item.name))
        )
    )


def show_mcp_server(
    entries: Iterable[Mapping[str, Any]],
    show_input: McpCatalogShowInput,
) -> McpCatalogShowResult:
    """Return one safe descriptor, consuming the supplied iterable once."""

    name = _normalize_name(show_input.name)
    if not name:
        raise McpCatalogError(
            "MCP server name must contain only letters, numbers, hyphens, "
            "and underscores",
            code="BAD_REQUEST",
        )
    catalog = list_mcp_servers(entries)
    for descriptor in catalog.servers:
        if descriptor.name == name:
            return McpCatalogShowResult(server=descriptor)
    raise McpCatalogError(f"MCP server '{name}' not found", code="NOT_FOUND")


__all__ = [
    "McpCatalogError",
    "McpCatalogListInput",
    "McpCatalogListResult",
    "McpCatalogShowInput",
    "McpCatalogShowResult",
    "McpServerDescriptor",
    "list_mcp_servers",
    "show_mcp_server",
]
