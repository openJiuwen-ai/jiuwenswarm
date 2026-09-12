# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime normalization for task-level auto permission mode."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.agents.harness.common.rails.permissions.protected_paths import (
    JIUWENCLAW_PROTECTED_WRITE_PATHS,
    merge_protected_write_paths,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
    classify_tool,
)
from jiuwenswarm.common.permission_tools import (
    normalize_permission_tool_name,
    stricter_permission_level,
)

logger = logging.getLogger(__name__)

MANUAL_PERMISSION_MODE = "manual"
AUTO_PERMISSION_MODE = "auto"
_VALID_RUNTIME_MODES = {MANUAL_PERMISSION_MODE, AUTO_PERMISSION_MODE}
_STRUCTURED_PERMISSION_RULE_MAP_KEYS = frozenset({"paths", "commands", "patterns"})
AUTO_PERMISSION_DEFAULT_OPTIONS = {
    "reviewer_timeout_ms": 60000,
    "reviewer_min_confidence": 0.7,
    "persistent_audit_enabled": False,
    "bounded_write_max_files": 3,
    "bounded_write_excluded_paths": JIUWENCLAW_PROTECTED_WRITE_PATHS,
}


def resolve_declared_auto_workspace(
    params: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> Path | None:
    """Return one canonical explicit workspace, rejecting conflicting roots."""

    def canonical(value: Any) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("auto_permission_workspace_invalid") from exc

    metadata = metadata or {}
    workspace = canonical(params.get("workspace_dir"))
    project = canonical(params.get("project_dir") or metadata.get("project_dir"))
    if workspace is not None and project is not None and workspace != project:
        raise ValueError("auto_permission_workspace_conflict:new_session_required")
    return workspace or project


def supports_phase_auto_root(params: Mapping[str, Any]) -> bool:
    """Return whether the request belongs to the supported independent Deep root."""

    runtime_mode = str(params.get("mode") or "agent").strip().lower()
    work_mode = str(params.get("work_mode") or "").strip().lower()
    return bool(
        not params.get("team")
        and not is_team_mode(runtime_mode)
        and runtime_mode != "auto_harness"
        and runtime_mode.split(".", 1)[0] == "agent"
        and work_mode in {"", "work"}
    )


def resolve_permission_runtime_mode(permission_config: Mapping[str, Any]) -> str:
    """Return the task permission runtime mode."""
    raw_mode = (
        str(permission_config.get("mode") or MANUAL_PERMISSION_MODE).strip().lower()
    )
    if raw_mode in _VALID_RUNTIME_MODES:
        return raw_mode
    logger.warning("Unknown permissions.mode %r; falling back to manual", raw_mode)
    return MANUAL_PERMISSION_MODE


def is_auto_permission_mode(permission_config: Mapping[str, Any]) -> bool:
    """Return whether task-level auto permission mode is selected."""
    return resolve_permission_runtime_mode(permission_config) == AUTO_PERMISSION_MODE


def is_permission_boundary_enabled(permission_config: Mapping[str, Any]) -> bool:
    """Return whether the permission boundary is explicitly enabled."""
    return permission_config.get("enabled") is True


def is_auto_permission_enabled(permission_config: Mapping[str, Any]) -> bool:
    """Return whether task-level auto permission is eligible to activate."""
    return is_permission_boundary_enabled(permission_config) and is_auto_permission_mode(
        permission_config
    )


def normalize_permissions_for_runtime(
    permission_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a runtime-safe permissions config without mutating user config."""
    normalized = copy.deepcopy(dict(permission_config))
    if not is_auto_permission_mode(permission_config):
        return normalized

    normalized["mode"] = AUTO_PERMISSION_MODE
    normalized["auto"] = normalize_auto_permission_options(normalized.get("auto"))

    defaults = normalized.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    else:
        defaults = copy.deepcopy(defaults)
    defaults["*"] = _normalize_auto_default_level(defaults.get("*"))
    normalized["defaults"] = defaults

    tools = normalized.get("tools")
    if isinstance(tools, dict):
        normalized["tools"] = _normalize_tools_for_auto_mode(tools)

    return normalized


def normalize_auto_permission_options(raw_options: Any) -> dict[str, Any]:
    """Return runtime-safe `permissions.auto` options with auto-mode defaults."""
    options = copy.deepcopy(raw_options) if isinstance(raw_options, Mapping) else {}
    normalized = copy.deepcopy(AUTO_PERMISSION_DEFAULT_OPTIONS)
    for key, default_value in AUTO_PERMISSION_DEFAULT_OPTIONS.items():
        if key not in options:
            continue
        raw_value = options[key]
        if isinstance(default_value, bool):
            normalized[key] = bool(raw_value)
        elif isinstance(default_value, int) and not isinstance(default_value, bool):
            normalized[key] = _positive_int(raw_value, default_value)
        elif isinstance(default_value, float):
            normalized[key] = _bounded_float(raw_value, default_value)
        elif key == "bounded_write_excluded_paths":
            normalized[key] = merge_protected_write_paths(
                default_value,
                _normalize_string_tuple(raw_value, ()),
            )
        else:
            normalized[key] = raw_value
    return normalized


def _normalize_tools_for_auto_mode(tools: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for raw_tool_name, raw_level in tools.items():
        tool_name = normalize_permission_tool_name(raw_tool_name)
        if not tool_name:
            continue
        entry = _normalize_tool_entry_for_auto_mode(tool_name, raw_level)
        if tool_name in normalized:
            entry = _merge_tool_entries_for_auto_mode(
                tool_name, normalized[tool_name], entry
            )
        normalized[tool_name] = entry
    return normalized


def _normalize_auto_default_level(value: Any) -> str:
    level = _normalize_level(value)
    if level == "deny":
        return "deny"
    return "ask"


def _normalize_tool_entry_for_auto_mode(tool_name: str, entry: Any) -> Any:
    if not isinstance(entry, Mapping):
        return _normalize_tool_level_for_auto_mode(tool_name, entry)

    normalized_entry = copy.deepcopy(dict(entry))
    for key in ("*", "default", "level", "permission"):
        if key in normalized_entry:
            normalized_entry[key] = _normalize_tool_level_for_auto_mode(
                tool_name, normalized_entry[key]
            )
    return normalized_entry


def _normalize_tool_level_for_auto_mode(tool_name: str, value: Any) -> str:
    level = _normalize_level(value)
    if level == "allow" and _tool_requires_ask_in_auto(tool_name):
        return "ask"
    return level


def _merge_tool_entries_for_auto_mode(
    tool_name: str, existing: Any, candidate: Any
) -> Any:
    strict_level = stricter_permission_level(
        _tool_entry_default_level(tool_name, existing),
        _tool_entry_default_level(tool_name, candidate),
    )
    if not isinstance(existing, Mapping) and not isinstance(candidate, Mapping):
        return strict_level

    merged = _merge_structured_tool_rules(existing, candidate)
    merged["*"] = strict_level
    return merged


def _tool_entry_default_level(tool_name: str, entry: Any) -> str:
    if isinstance(entry, Mapping):
        for key in ("*", "default", "level", "permission"):
            if key in entry:
                return _normalize_tool_level_for_auto_mode(tool_name, entry[key])
        return "ask"
    return _normalize_tool_level_for_auto_mode(tool_name, entry)


def _merge_structured_tool_rules(left: Any, right: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for entry in (left, right):
        if not isinstance(entry, Mapping):
            continue
        for key, value in entry.items():
            if key in {"*", "default", "level", "permission"}:
                continue
            if key in merged and key in _STRUCTURED_PERMISSION_RULE_MAP_KEYS:
                merged[key] = _merge_structured_rule_values(
                    key,
                    merged[key],
                    value,
                )
            elif (
                key in merged
                and isinstance(merged[key], Mapping)
                and isinstance(value, Mapping)
            ):
                merged[key] = _merge_structured_rule_map(key, merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
    return merged


def _merge_structured_rule_values(rule_key: str, left: Any, right: Any) -> Any:
    left_rules = _structured_rule_map_from_value(left)
    right_rules = _structured_rule_map_from_value(right)
    if not left_rules and not right_rules:
        return copy.deepcopy(right)
    return _merge_structured_rule_map(rule_key, left_rules, right_rules)


def _merge_structured_rule_map(
    rule_key: str, left: Mapping[Any, Any], right: Mapping[Any, Any]
) -> dict[Any, Any]:
    merged = copy.deepcopy(dict(left))
    for item_key, item_value in right.items():
        if item_key in merged and rule_key in _STRUCTURED_PERMISSION_RULE_MAP_KEYS:
            merged[item_key] = stricter_permission_level(
                _normalize_level(merged[item_key]),
                _normalize_level(item_value),
            )
            continue
        merged[item_key] = copy.deepcopy(item_value)
    return merged


def _structured_rule_map_from_value(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {
            str(pattern): _normalize_level(level) for pattern, level in value.items()
        }
    if not isinstance(value, list | tuple):
        return {}

    rules: dict[str, str] = {}
    for item in value:
        if isinstance(item, str):
            rules[item] = stricter_permission_level(rules.get(item, "allow"), "deny")
            continue
        if not isinstance(item, Mapping):
            continue
        pattern = item.get("pattern") or item.get("path") or item.get("command")
        if pattern is None:
            continue
        level = _structured_rule_item_level(item)
        if level is None:
            continue
        rules[str(pattern)] = stricter_permission_level(
            rules.get(str(pattern), "allow"),
            level,
        )
    return rules


def _structured_rule_item_level(item: Mapping[Any, Any]) -> str | None:
    for key in ("level", "permission", "action"):
        if key in item:
            return _normalize_level(item[key])
    return None


def _normalize_level(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("*")
    level = str(value or "ask").strip().lower()
    if level in {"allow", "ask", "deny"}:
        return level
    return "ask"


def _positive_int(value: Any, default_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default_value
    return parsed if parsed > 0 else default_value


def _bounded_float(value: Any, default_value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default_value
    if 0.0 <= parsed <= 1.0:
        return parsed
    return default_value


def _normalize_string_tuple(
    value: Any, default_value: tuple[str, ...]
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return default_value
    normalized: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return tuple(normalized)


def _tool_requires_ask_in_auto(tool_name: str) -> bool:
    capability = classify_tool(tool_name)
    return capability.high_flex or capability.risk_tier == "high"
