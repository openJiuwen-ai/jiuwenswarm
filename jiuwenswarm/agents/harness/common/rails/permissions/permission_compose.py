# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose Global ⊕ User ⊕ Session into effective permissions for the Engine."""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_DROP = frozenset({"deny_tools", "ask_tools", "enabled"})
_TOOL_LEVELS = frozenset({"allow", "ask", "deny"})
FILE_GUARD_AXES = ("read", "write", "exec")
# The Engine treats path rules as literal strings (it never folds case); only
# Windows paths are case-insensitive by platform contract.
_CASE_INSENSITIVE = os.name == "nt"
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
    if session_lv == "allow":
        return "allow"
    if user_lv == "allow":
        return "allow"
    for lv in (global_lv, user_lv):
        if lv == "ask":
            return "ask"
    for lv in (global_lv, user_lv, session_lv):
        if lv == "allow":
            return "allow"
    return global_lv or user_lv or session_lv


def path_axis_level(item: dict[str, Any], key: str) -> str | None:
    """One path axis of a ``file_guard.paths`` entry.

    ``None`` means the layer holds no opinion about that axis; it must stay
    distinct from ``"ask"`` so a per-axis increment never tightens a lower
    layer's explicit ``allow``.
    """
    raw = item.get(key)
    if not isinstance(raw, str):
        return None
    level = raw.strip().lower()
    return level if level in _TOOL_LEVELS else None


def _expand_user(path: str) -> str:
    """``os.path.expanduser`` seam: tests stand in for another host's home."""
    return os.path.expanduser(path)


def path_rule_scope(item: dict[str, Any]) -> tuple[str, str]:
    """``(path, match)`` identity of one ``file_guard.paths`` entry.

    Follows the Engine's compile step: separators normalized, trailing slash
    dropped for prefix rules, ``~`` expanded for prefix rules, and ``match``
    collapsed to the two values the Engine understands. Glob paths are kept
    verbatim because the Engine matches them literally (``/data/**/`` and
    ``/data/**`` are two different rules). ``resolve()`` is deliberately not
    applied, so ``..``, symlinks and ``$VAR`` spellings can still yield two
    entries that compile to the same prefix.

    Case folding follows the Engine too: only prefix rules are merged
    case-insensitively, because the Engine's prefix match leans on
    ``contains_path`` (``normcase``) while its glob match is a case-sensitive
    regular expression on every platform.
    """
    match = "glob" if str(item.get("match") or "").strip().lower() == "glob" else "prefix"
    raw = item.get("path")
    path = raw.strip() if isinstance(raw, str) else ""
    path = path.replace("\\", "/")
    if match == "prefix":
        path = path.rstrip("/")
        if path:
            # expanduser brings separators of its own (backslashes on Windows)
            # and may keep a trailing one; normalize both so "~" and the
            # equivalent explicit path compile to the same prefix.
            path = _expand_user(path).replace("\\", "/").rstrip("/")
        path = path.casefold() if _CASE_INSENSITIVE else path
    return (path, match)


def _layer_path_axes(layer: dict[str, Any]) -> dict[tuple[str, str], dict[str, str]]:
    """Per-scope axis union inside one layer: ``deny`` > ``allow`` > ``ask``."""
    axes: dict[tuple[str, str], dict[str, str]] = {}
    fg = layer.get("file_guard")
    if not isinstance(fg, dict):
        return axes
    entries = fg.get("paths")
    if not isinstance(entries, list):
        return axes
    for item in entries:
        if not isinstance(item, dict):
            continue
        merged = axes.setdefault(path_rule_scope(item), {})
        for key in FILE_GUARD_AXES:
            level = path_axis_level(item, key)
            if level is None:
                continue
            current = merged.get(key)
            if current == "deny" or level == "deny":
                merged[key] = "deny"
            elif current == "allow" or level == "allow":
                merged[key] = "allow"
            else:
                merged[key] = "ask"
    return axes


def _compose_path_axis(
    global_lv: str | None, user_lv: str | None, session_lv: str | None,
) -> str | None:
    """One path axis across layers; ``None`` when no layer holds an opinion.

    A User-layer ``ask`` on a path rule is the fill value the approval flow
    writes for the actions it did not grant (``_axes_for_file_guard_action``
    in the Engine); the settings RPC only edits tool lists, so a path ask
    arrives from an approval landing in the User layer, or from a hand-edited
    YAML. It must not turn a Global ``allow`` on the same axis back into a
    prompt, so it is dropped there.

    It is dropped *only* against an explicit Global ``allow``. With no opinion
    above it, the ask is the only one on that axis, and dropping it hands the
    axis to the Engine's own default: legacy mode compiles an absent axis of a
    path rule as ``read``/``write`` allow (``exec`` ask), so a grant of read
    alone would silently allow writes to the same directory. The asymmetry
    with the tool-level merge -- where a User ``ask`` does tighten a Global
    ``allow`` -- follows from the data source: tool lists are user-editable,
    path rules are not.
    """
    if user_lv == "ask" and global_lv == "allow":
        user_lv = None
    return _pick_level(global_lv, user_lv, session_lv)


def compose_path_axes(
    global_permissions: dict[str, Any] | None = None,
    user_permissions: dict[str, Any] | None = None,
    session_permissions: dict[str, Any] | None = None,
) -> dict[tuple[str, str], dict[str, str | None]]:
    """Global ⊕ User ⊕ Session view of ``file_guard.paths``, one row per scope.

    Axes follow ``_pick_level``: a Global/User deny wins, a User allow relaxes
    a Global ask, and a Session allow relaxes an ask. A User ``ask`` is kept
    unless a Global allow already covers the axis -- see ``_compose_path_axis``.
    A ``None`` axis means no layer holds an opinion, so callers must not
    materialize it: the Engine's own default differs between native (``ask``)
    and legacy (``allow`` for ``read``/``write``) mode.
    """
    global_axes = _layer_path_axes(global_permissions) if isinstance(
        global_permissions, dict
    ) else {}
    user_axes = _layer_path_axes(user_permissions) if isinstance(user_permissions, dict) else {}
    session_axes = _layer_path_axes(session_permissions) if isinstance(
        session_permissions, dict
    ) else {}
    scopes = list(global_axes)
    scopes += [scope for scope in user_axes if scope not in global_axes]
    scopes += [
        scope for scope in session_axes if scope not in global_axes and scope not in user_axes
    ]
    composed: dict[tuple[str, str], dict[str, str | None]] = {}
    for scope in scopes:
        composed[scope] = {
            key: _compose_path_axis(
                global_axes.get(scope, {}).get(key),
                user_axes.get(scope, {}).get(key),
                session_axes.get(scope, {}).get(key),
            )
            for key in FILE_GUARD_AXES
        }
    return composed


def _merge_file_guard_paths(*layers: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Collapse ``file_guard.paths`` into one entry per ``(path, match)``.

    The Engine resolves a path through its longest matching prefix and keeps
    the first entry on a tie, so a second entry for the same path would hide
    every later layer's axis. Entries without a usable ``path`` are not
    keyable; they are passed through and ignored by the Engine as before.
    """
    axes = compose_path_axes(*layers)
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for layer in layers:
        fg = layer.get("file_guard") if isinstance(layer, dict) else None
        entries = fg.get("paths") if isinstance(fg, dict) else None
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                passthrough.append(deepcopy(item))
                continue
            merged.setdefault(path_rule_scope(item), deepcopy(item))
    out: list[dict[str, Any]] = []
    for scope, entry in merged.items():
        # Never materialize an axis no layer spoke about: the Engine's default
        # for an absent axis is ``ask`` in native mode but ``allow`` in legacy
        # mode, and legacy configs rely on that.
        for key, level in (axes.get(scope) or {}).items():
            if level is not None:
                entry[key] = level
        out.append(entry)
    out.extend(passthrough)
    return out


def _merge_list_of_dicts(*layers: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, list):
            continue
        for item in layer:
            if isinstance(item, dict):
                out.append(deepcopy(item))
    return out


def _apply_product_p1_rule_actions(
    rules: list[dict[str, Any]],
    *,
    permission_mode: Any = None,
) -> list[dict[str, Any]]:
    """Fill host rules missing ``action`` via severity + ``permission_mode``.

    Catalog builtins already carry YAML default action; this only fills host
    rules. Mapping lives in agent-core ``severity_to_action`` (required by the
    openjiuwen pin); ImportError must surface rather than silently widen.
    """
    from openjiuwen.harness.security.permission_engine.toolguard.severity_mapping import (
        severity_to_action,
    )

    filled: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        item = deepcopy(rule)
        action = item.get("action")
        if isinstance(action, str) and action.strip():
            filled.append(item)
            continue
        mapped = severity_to_action(item.get("severity"), permission_mode)
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
    auto_mode: bool = False,
) -> dict[str, Any]:
    """Effective = Global ⊕ User ⊕ Session. ``agent_permissions`` is ignored in P1.

    Whole-tool deny still wins. User allow relaxes Global ask; Session allow
    relaxes User/Global ask. Session deny/ask fields are dropped.
    """
    _ = (session_id, agent_permissions)
    try:
        g = _as_dict(global_permissions)
        if auto_mode:
            from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
                normalize_permissions_for_runtime,
            )

            g = normalize_permissions_for_runtime(compose_host_effective_permissions(
                global_permissions=g,
            ))
            # The engine projection is authoritative after normalization. Old
            # list mirrors must not turn its ASK back into ALLOW during compose.
            for key in ("allow_tools", "ask_tools", "deny_tools"):
                g.pop(key, None)
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
            _merge_list_of_dicts(g.get("rules"), u.get("rules"), s.get("rules")),
            permission_mode=out.get("permission_mode"),
        )
        out["approval_overrides"] = _merge_list_of_dicts(
            g.get("approval_overrides"),
            u.get("approval_overrides"),
            s.get("approval_overrides"),
        )

        fg = deepcopy(g.get("file_guard")) if isinstance(g.get("file_guard"), dict) else {}
        merged_paths = _merge_file_guard_paths(g, u, s)
        if merged_paths:
            fg["paths"] = merged_paths
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


__all__ = [
    "FILE_GUARD_AXES",
    "compose_host_effective_permissions",
    "compose_path_axes",
    "path_axis_level",
    "path_rule_scope",
]
