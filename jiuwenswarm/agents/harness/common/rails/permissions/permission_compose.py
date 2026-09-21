# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose Global ⊕ User ⊕ Session into effective permissions for the Engine."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_DROP = frozenset({"deny_tools", "ask_tools", "enabled"})
_TOOL_LEVELS = frozenset({"allow", "ask", "deny"})
_NET_ACTIONS = frozenset({"allow", "deny"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


def _package_builtin_rules_enabled(layer: dict[str, Any]) -> bool:
    """Global-only host fact. Missing / invalid defaults to True."""
    try:
        from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import (
            package_builtin_rules_enabled,
        )
    except ImportError:
        value = layer.get("package_builtin_rules")
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _FALSE_STRINGS:
            return False
        return True
    return package_builtin_rules_enabled(layer)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _scalar_level(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("*")
    if not isinstance(raw, str):
        return None
    level = raw.strip().lower()
    return level if level in _TOOL_LEVELS else None


def _named_levels(layer: dict[str, Any], *, drop_tighten: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    tools = layer.get("tools")
    if isinstance(tools, dict):
        for name, raw in tools.items():
            if not isinstance(name, str) or not name.strip():
                continue
            level = _scalar_level(raw)
            if level is None:
                continue
            if drop_tighten and level in ("deny", "ask"):
                continue
            out[name.strip()] = level
    mapping = (
        ("deny_tools", "deny"),
        ("ask_tools", "ask"),
        ("allow_tools", "allow"),
    )
    if drop_tighten:
        mapping = (("allow_tools", "allow"),)
    for key, level in mapping:
        names = layer.get(key)
        if not isinstance(names, list):
            continue
        for name in names:
            if isinstance(name, str) and name.strip():
                out[name.strip()] = level
    return out


def _pick_level(global_lv: str | None, user_lv: str | None, session_lv: str | None) -> str | None:
    for lv in (global_lv, user_lv):
        if lv == "deny":
            return "deny"
    for lv in (global_lv, user_lv):
        if lv == "ask":
            return "ask"
    if session_lv == "allow" and global_lv not in ("deny", "ask") and user_lv not in ("deny", "ask"):
        return "allow"
    for lv in (global_lv, user_lv, session_lv):
        if lv == "allow":
            return "allow"
    return global_lv or user_lv or session_lv


def _merge_list_of_dicts(*layers: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, list):
            continue
        for item in layer:
            if isinstance(item, dict):
                out.append(deepcopy(item))
    return out


# P1 product table. Later modes overlay action from a mode-specific map.
# Catalog builtins already carry YAML default action; this only fills host rules.
_P1_SEVERITY_TO_ACTION = {
    "LOW": "allow",
    "MEDIUM": "allow",
    "HIGH": "ask",
    "CRITICAL": "deny",
}


def _apply_product_p1_rule_actions(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filled: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        item = deepcopy(rule)
        action = item.get("action")
        if isinstance(action, str) and action.strip():
            filled.append(item)
            continue
        sev = str(item.get("severity") or "").strip().upper()
        mapped = _P1_SEVERITY_TO_ACTION.get(sev)
        if mapped:
            item["action"] = mapped
        filled.append(item)
    return filled


def _net_action(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("action") if raw.get("action") is not None else raw.get("fetch")
    if not isinstance(raw, str):
        return None
    action = raw.strip().lower()
    return action if action in _NET_ACTIONS else None


def _merge_net_urls(*layers: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for pattern, value in layer.items():
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            action = _net_action(value)
            if action is None:
                continue
            key = pattern.strip()
            existing = merged.get(key)
            if existing == "deny" and action == "allow":
                continue
            merged[key] = "deny" if existing == "deny" or action == "deny" else "allow"
    return merged


def _as_shell_guard_flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _compose_shell_guard(global_perms: dict[str, Any], user_perms: dict[str, Any]) -> dict[str, Any]:
    """Global ⊕ User only. Session is ignored. Later layer wins; missing/illegal → true."""
    g_sg = global_perms.get("shell_guard")
    u_sg = user_perms.get("shell_guard")
    g_sg = g_sg if isinstance(g_sg, dict) else {}
    u_sg = u_sg if isinstance(u_sg, dict) else {}

    def _flag(key: str) -> bool:
        user_flag = _as_shell_guard_flag(u_sg.get(key))
        if user_flag is not None:
            return user_flag
        global_flag = _as_shell_guard_flag(g_sg.get(key))
        if global_flag is not None:
            return global_flag
        return True

    return {
        "unknown_structure": _flag("unknown_structure"),
        "interpreter_sink": _flag("interpreter_sink"),
    }


def _compose_net_guard(global_perms: dict[str, Any], user_perms: dict[str, Any]) -> dict[str, Any]:
    """Global ⊕ User only. Session is ignored. User cannot disable or widen deny."""
    g_ng = global_perms.get("net_guard")
    u_ng = user_perms.get("net_guard")
    g_ng = g_ng if isinstance(g_ng, dict) else {}
    u_ng = u_ng if isinstance(u_ng, dict) else {}

    enabled = bool(g_ng["enabled"]) if "enabled" in g_ng else True
    g_defaults = _net_action(g_ng.get("defaults"))
    u_defaults = _net_action(u_ng.get("defaults"))
    if g_defaults == "deny" and u_defaults == "allow":
        defaults = "deny"
    elif u_defaults is not None:
        defaults = u_defaults
    elif g_defaults is not None:
        defaults = g_defaults
    else:
        defaults = "allow"

    return {
        "enabled": enabled,
        "defaults": defaults,
        "urls": _merge_net_urls(g_ng.get("urls"), u_ng.get("urls")),
    }


def _fail_closed(reason: str, enabled: bool) -> dict[str, Any]:
    logger.error("[permissions.compose] fail-closed reason=%s", reason)
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
        shell_tool_names,
    )

    return {
        "enabled": enabled,
        "schema": "tiered_policy",
        "permission_mode": "normal",
        "defaults": {"*": "ask"},
        "tools": {},
        "rules": [],
        "approval_overrides": [],
        "categories": {"shell": shell_tool_names()},
        "package_builtin_rules": True,
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [],
        },
        "shell_guard": {"unknown_structure": True, "interpreter_sink": True},
    }


def compose_host_effective_permissions(
    *,
    global_permissions: dict[str, Any] | None = None,
    user_permissions: dict[str, Any] | None = None,
    session_permissions: dict[str, Any] | None = None,
    session_id: str | None = None,
    agent_permissions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Effective = Global ⊕ User ⊕ Session. ``agent_permissions`` is ignored in P1."""
    _ = (session_id, agent_permissions)
    try:
        g = _as_dict(global_permissions)
        u = _as_dict(user_permissions)
        s = {
            k: v
            for k, v in _as_dict(session_permissions).items()
            if k not in _SESSION_DROP
        }

        g_lv = _named_levels(g)
        u_lv = _named_levels(u)
        s_lv = _named_levels(s, drop_tighten=True)

        names = set(g_lv) | set(u_lv) | set(s_lv)
        tools: dict[str, str] = {}
        deny_tools: list[str] = []
        ask_tools: list[str] = []
        allow_tools: list[str] = []
        for name in sorted(names):
            level = _pick_level(g_lv.get(name), u_lv.get(name), s_lv.get(name))
            if level is None:
                continue
            tools[name] = level
            if level == "deny":
                deny_tools.append(name)
            elif level == "ask":
                ask_tools.append(name)
            else:
                allow_tools.append(name)

        out = deepcopy(g)
        out["tools"] = tools
        out["deny_tools"] = deny_tools
        out["ask_tools"] = ask_tools
        out["allow_tools"] = allow_tools
        if "enabled" not in out:
            out["enabled"] = False
        out["rules"] = _apply_product_p1_rule_actions(
            _merge_list_of_dicts(g.get("rules"), u.get("rules"), s.get("rules"))
        )
        out["approval_overrides"] = _merge_list_of_dicts(
            g.get("approval_overrides"),
            u.get("approval_overrides"),
            s.get("approval_overrides"),
        )

        fg = deepcopy(g.get("file_guard")) if isinstance(g.get("file_guard"), dict) else {}
        for layer in (u, s):
            overlay_fg = layer.get("file_guard")
            if not isinstance(overlay_fg, dict):
                continue
            extra = overlay_fg.get("paths")
            if not isinstance(extra, list):
                continue
            paths = fg.get("paths")
            if not isinstance(paths, list):
                paths = []
            paths.extend(deepcopy(p) for p in extra if isinstance(p, dict))
            fg["paths"] = paths
        if fg:
            out["file_guard"] = fg

        out["net_guard"] = _compose_net_guard(g, u)
        out["shell_guard"] = _compose_shell_guard(g, u)

        from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
            shell_tool_names,
        )

        # Host facts：User / Session 不得覆盖 shell 工具名单 / 包内 builtin_rules
        out.pop("categories", None)
        out["categories"] = {"shell": shell_tool_names()}
        use_package = _package_builtin_rules_enabled(g)
        out["package_builtin_rules"] = use_package
        if not use_package:
            return out

        from openjiuwen.harness.security.permission_engine.fileguard.sensitive_paths import (
            merge_package_sensitive_paths,
        )
        from openjiuwen.harness.security.permission_engine.netguard.net_urls import (
            merge_package_net_urls,
        )
        from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import (
            inline_package_command_rules,
        )

        return merge_package_net_urls(
            merge_package_sensitive_paths(inline_package_command_rules(out))
        )
    except Exception as exc:
        enabled = bool(_as_dict(global_permissions).get("enabled"))
        logger.exception("[permissions.compose] failed: %s", exc)
        return _fail_closed(str(exc), enabled=enabled)


__all__ = ["compose_host_effective_permissions"]
