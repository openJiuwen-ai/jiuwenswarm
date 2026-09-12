# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Permission tool-name helpers shared by config and runtime policy code."""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

PERMISSION_TOOL_ALIASES = {
    "exec_command": "mcp_exec_command",
    "fetch_webpage": "mcp_fetch_webpage",
    "free_search": "mcp_free_search",
    "paid_search": "mcp_paid_search",
}
PERMISSION_LEVEL_RANK = {
    "allow": 0,
    "ask": 1,
    "deny": 2,
}


@dataclass(frozen=True)
class PermissionToolNameResolution:
    """Canonical alias resolution without permission or semantic authority."""

    registered_name: str
    canonical_name: str
    aliases: tuple[str, ...]
    conflict: bool


def resolve_permission_tool_name(
    tool_name: Any,
    *,
    canonical_names: Collection[str] = (),
) -> PermissionToolNameResolution:
    """Resolve one name and fail closed when the alias graph is ambiguous."""
    try:
        registered_name = str(tool_name or "").strip()
    except Exception:  # A caller-controlled name must not escape fail-closed lookup.
        return PermissionToolNameResolution(
            registered_name="",
            canonical_name="",
            aliases=(),
            conflict=True,
        )
    canonical_name = PERMISSION_TOOL_ALIASES.get(registered_name, registered_name)
    alias_targets = frozenset(PERMISSION_TOOL_ALIASES.values())
    conflict = (
        (
            registered_name in canonical_names
            and registered_name in PERMISSION_TOOL_ALIASES
            and canonical_name != registered_name
        )
        or (
            registered_name in alias_targets
            and registered_name in PERMISSION_TOOL_ALIASES
            and canonical_name != registered_name
        )
        or (
            canonical_name in PERMISSION_TOOL_ALIASES
            and PERMISSION_TOOL_ALIASES[canonical_name] != canonical_name
        )
    )
    if conflict:
        canonical_name = ""
        aliases: tuple[str, ...] = ()
    else:
        aliases = tuple(
            sorted(
                alias
                for alias, target in PERMISSION_TOOL_ALIASES.items()
                if target == canonical_name and alias != canonical_name
            )
        )
    return PermissionToolNameResolution(
        registered_name=registered_name,
        canonical_name=canonical_name,
        aliases=aliases,
        conflict=conflict,
    )


def normalize_permission_tool_name(tool_name: Any) -> str:
    """Return the canonical permission tool name used by runtime checks."""
    return resolve_permission_tool_name(tool_name).canonical_name


def stricter_permission_level(left: str, right: str) -> str:
    """Return the stricter permission level, defaulting unknown input to ask."""
    left_level = str(left or "ask").strip().lower()
    right_level = str(right or "ask").strip().lower()
    left_rank = PERMISSION_LEVEL_RANK.get(left_level, PERMISSION_LEVEL_RANK["ask"])
    right_rank = PERMISSION_LEVEL_RANK.get(right_level, PERMISSION_LEVEL_RANK["ask"])
    return left_level if left_rank >= right_rank else right_level
