# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only, transport-neutral permission catalog contracts.

This module exposes permission configuration as immutable presentation data.
It deliberately does not acquire the permission writer lock because that lock
creates sidecar files and parent directories.  It also does not persist
configuration, reload agents, or execute an approval decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

PermissionLevel = Literal["allow", "ask", "deny"]
_PERMISSION_LEVELS = frozenset({"allow", "ask", "deny"})
_SEVERITY_ACTIONS: dict[str, PermissionLevel] = {
    "LOW": "allow",
    "MEDIUM": "allow",
    "HIGH": "ask",
    "CRITICAL": "deny",
}


class PermissionCatalogError(RuntimeError):
    """Stable permission-catalog failure without a wire representation."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionSnapshotInput:
    """Select the host snapshot or one syntactically safe Session overlay."""

    channel_id: str = ""
    session_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionToolSnapshot:
    """One named tool and its configured permission level."""

    name: str
    level: PermissionLevel

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "level": self.level}


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionRuleSnapshot:
    """Display-safe fields from one permission rule."""

    rule_id: str
    tools: tuple[str, ...]
    pattern: str
    action: PermissionLevel | None = None
    severity: str = ""
    description: str = ""
    match_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "tools": list(self.tools),
            "pattern": self.pattern,
            "action": self.action,
            "severity": self.severity,
            "description": self.description,
            "match_type": self.match_type,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionLayerSnapshot:
    """Immutable, display-safe view of one permission layer."""

    enabled: bool | None
    tools: tuple[PermissionToolSnapshot, ...]
    rules: tuple[PermissionRuleSnapshot, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tools": [item.to_dict() for item in self.tools],
            "rules": [item.to_dict() for item in self.rules],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionSnapshotResult:
    """Global, User, Session, and effective permission views."""

    scope: Literal["host", "session"]
    session_id: str
    global_layer: PermissionLayerSnapshot
    user_layer: PermissionLayerSnapshot
    session_layer: PermissionLayerSnapshot
    effective: PermissionLayerSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "session_id": self.session_id,
            "global": self.global_layer.to_dict(),
            "user": self.user_layer.to_dict(),
            "session": self.session_layer.to_dict(),
            "effective": self.effective.to_dict(),
        }


def _permission_level(raw: Any) -> PermissionLevel | None:
    if isinstance(raw, dict):
        raw = raw.get("*")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized not in _PERMISSION_LEVELS:
        return None
    return cast(PermissionLevel, normalized)


def _named_tool_levels(layer: dict[str, Any]) -> dict[str, PermissionLevel]:
    """Normalize current and legacy tool forms with composer precedence."""

    levels: dict[str, PermissionLevel] = {}
    tools = layer.get("tools")
    if isinstance(tools, dict):
        for raw_name, raw_level in tools.items():
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            configured_level = _permission_level(raw_level)
            if name and configured_level is not None:
                levels[name] = configured_level

    legacy_levels: tuple[tuple[str, PermissionLevel], ...] = (
        ("deny_tools", "deny"),
        ("ask_tools", "ask"),
        ("allow_tools", "allow"),
    )
    for key, level in legacy_levels:
        names = layer.get(key)
        if not isinstance(names, list):
            continue
        for raw_name in names:
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if name:
                levels[name] = level
    return levels


def _rule_tools(raw: Any) -> tuple[str, ...]:
    values = raw if isinstance(raw, (list, tuple)) else (raw,)
    tools: list[str] = []
    for value in values:
        name = value.strip() if isinstance(value, str) else ""
        if name and name not in tools:
            tools.append(name)
    return tuple(tools)


def _rule_action(rule: dict[str, Any]) -> PermissionLevel | None:
    action = _permission_level(rule.get("action"))
    if action is not None:
        return action
    severity = str(rule.get("severity") or "").strip().upper()
    return _SEVERITY_ACTIONS.get(severity)


def _safe_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _rule_snapshots(layer: dict[str, Any]) -> tuple[PermissionRuleSnapshot, ...]:
    rules = layer.get("rules")
    if not isinstance(rules, list):
        return ()
    snapshots: list[PermissionRuleSnapshot] = []
    for raw_rule in rules:
        if not isinstance(raw_rule, dict):
            continue
        snapshots.append(
            PermissionRuleSnapshot(
                rule_id=_safe_text(raw_rule.get("id")),
                tools=_rule_tools(raw_rule.get("tools")),
                pattern=_safe_text(raw_rule.get("pattern")),
                action=_rule_action(raw_rule),
                severity=_safe_text(raw_rule.get("severity")).upper(),
                description=_safe_text(raw_rule.get("description")),
                match_type=_safe_text(raw_rule.get("match_type")),
            )
        )
    return tuple(snapshots)


def _layer_snapshot(layer: dict[str, Any]) -> PermissionLayerSnapshot:
    source = layer if isinstance(layer, dict) else {}
    enabled = source.get("enabled")
    tool_levels = _named_tool_levels(source)
    return PermissionLayerSnapshot(
        enabled=enabled if isinstance(enabled, bool) else None,
        tools=tuple(
            PermissionToolSnapshot(name=name, level=level)
            for name, level in sorted(tool_levels.items())
        ),
        rules=_rule_snapshots(source),
    )


def _read_permission_layers(session_id: str) -> tuple[dict[str, Any], ...]:
    """Read existing files without entering the writer-lock boundary."""

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        load_global_permissions,
        load_session_permissions,
        load_user_permissions,
    )

    return (
        load_global_permissions(),
        load_user_permissions(),
        load_session_permissions(session_id) if session_id else {},
    )


def _normalize_session_id(raw: str | None) -> str:
    session_id = str(raw or "").strip()
    if not session_id:
        return ""

    from jiuwenswarm.server.runtime.session.session_history import (
        is_valid_session_id,
    )

    if not is_valid_session_id(session_id):
        raise PermissionCatalogError("invalid session id", code="BAD_REQUEST")
    return session_id


def read_permission_snapshot(
    request: PermissionSnapshotInput,
) -> PermissionSnapshotResult:
    """Read a safe permission snapshot without writes, reloads, or approvals.

    Session existence and Channel ownership remain Runtime-service concerns.
    This catalog validates the identifier as a safe path component only.
    """

    if not isinstance(request, PermissionSnapshotInput):
        raise PermissionCatalogError(
            "permission snapshot input is required",
            code="BAD_REQUEST",
        )
    session_id = _normalize_session_id(request.session_id)
    try:
        global_layer, user_layer, session_layer = _read_permission_layers(session_id)
        from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
            compose_host_effective_permissions,
        )

        effective = compose_host_effective_permissions(
            global_permissions=global_layer,
            user_permissions=user_layer,
            session_permissions=session_layer,
            session_id=session_id or None,
        )
    except PermissionCatalogError:
        raise
    except Exception as exc:
        raise PermissionCatalogError(
            "failed to read permissions",
            code="READ_FAILED",
        ) from exc

    return PermissionSnapshotResult(
        scope="session" if session_id else "host",
        session_id=session_id,
        global_layer=_layer_snapshot(global_layer),
        user_layer=_layer_snapshot(user_layer),
        session_layer=_layer_snapshot(session_layer),
        effective=_layer_snapshot(effective),
    )


__all__ = [
    "PermissionCatalogError",
    "PermissionLayerSnapshot",
    "PermissionLevel",
    "PermissionRuleSnapshot",
    "PermissionSnapshotInput",
    "PermissionSnapshotResult",
    "PermissionToolSnapshot",
    "read_permission_snapshot",
]
