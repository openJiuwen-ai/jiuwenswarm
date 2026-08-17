# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""三层权限配置（Global / User / Session）加载、合成与落盘。

Global: ``config/config.yaml`` 的 ``permissions:`` 段
User:   ``config/user_permissions.yaml``
Session: ``agent/sessions/<session_id>/session_permissions.yaml``

合成与迁移委托 agent-core ``PermissionModeController`` /
``compose_effective_permissions``。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

logger = logging.getLogger(__name__)

_VALID_MODES = frozenset({"full_access", "auto", "strict"})

# 与 agent-core MODE_PRESETS 对齐的最小子集，供 CI / 旧 openjiuwen 回退合成。
_FALLBACK_MODE_PRESETS: dict[str, dict[str, Any]] = {
    "full_access": {
        "sandbox_intent": "optional",
        "defaults": {"*": "allow"},
        "file_guard": {"enabled": False},
    },
    "auto": {
        "sandbox_intent": "required",
        "defaults": {"*": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "allow", "write": "ask", "exec": "ask"},
            "workspace": {"read": "allow", "write": "allow", "exec": "allow"},
        },
    },
    "strict": {
        "sandbox_intent": "required",
        "defaults": {"*": "ask"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "workspace": {"read": "allow", "write": "ask", "exec": "ask"},
        },
    },
}


def _import_core_compose() -> Callable[..., Any] | None:
    try:
        from openjiuwen.harness.security import compose_effective_permissions
    except ImportError:
        return None
    return compose_effective_permissions


def _import_mode_controller() -> type | None:
    try:
        from openjiuwen.harness.security.mode_controller import PermissionModeController
    except ImportError:
        return None
    return PermissionModeController


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in names:
            names.append(item.strip())
    return names


def _normalize_tools_to_lists(cfg: dict[str, Any]) -> None:
    tools = cfg.get("tools")
    if not isinstance(tools, dict):
        return
    ask = list(_as_str_list(cfg.get("ask_tools")))
    deny = list(_as_str_list(cfg.get("deny_tools")))
    allow = list(_as_str_list(cfg.get("allow_tools")))
    for name, level in tools.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(level, str):
            continue
        tool = name.strip()
        normalized = level.strip().lower()
        if normalized == "deny":
            if tool not in deny:
                deny.append(tool)
            if tool in ask:
                ask.remove(tool)
            if tool in allow:
                allow.remove(tool)
        elif normalized == "ask":
            if tool not in deny and tool not in ask:
                ask.append(tool)
            if tool in allow:
                allow.remove(tool)
        elif normalized == "allow":
            if tool not in deny and tool not in ask and tool not in allow:
                allow.append(tool)
    cfg.pop("tools", None)
    if ask:
        cfg["ask_tools"] = ask
    else:
        cfg.pop("ask_tools", None)
    if deny:
        cfg["deny_tools"] = deny
    else:
        cfg.pop("deny_tools", None)
    if allow:
        cfg["allow_tools"] = allow
    else:
        cfg.pop("allow_tools", None)


def _project_tool_lists(cfg: dict[str, Any]) -> None:
    deny = set(_as_str_list(cfg.get("deny_tools")))
    ask = [name for name in _as_str_list(cfg.get("ask_tools")) if name not in deny]
    ask_set = set(ask)
    allow = [
        name
        for name in _as_str_list(cfg.get("allow_tools"))
        if name not in deny and name not in ask_set
    ]
    tools: dict[str, str] = {}
    for name in sorted(deny):
        tools[name] = "deny"
    for name in ask:
        tools[name] = "ask"
    for name in allow:
        tools[name] = "allow"
    if tools:
        cfg["tools"] = tools
    else:
        cfg.pop("tools", None)


_FALLBACK_PATH_TOOLS = frozenset({
    "read_file",
    "write_file",
    "edit_file",
    "read_text_file",
    "write_text_file",
    "write",
    "read",
    "glob_file_search",
    "glob",
    "list_dir",
    "list_files",
    "grep",
    "search_replace",
    "send_file_to_user",
})


def _inject_fallback_path_tool_allows(cfg: dict[str, Any]) -> None:
    deny = set(_as_str_list(cfg.get("deny_tools")))
    ask = set(_as_str_list(cfg.get("ask_tools")))
    allow = list(_as_str_list(cfg.get("allow_tools")))
    changed = False
    for name in sorted(_FALLBACK_PATH_TOOLS):
        if name in deny or name in ask or name in allow:
            continue
        allow.append(name)
        changed = True
    if changed:
        cfg["allow_tools"] = allow


def migrate_legacy_permissions(
    raw: dict[str, Any] | None,
    *,
    is_overlay: bool = False,
) -> dict[str, Any]:
    """迁移旧字段；优先走 agent-core，缺失时用本地回退。"""
    controller = _import_mode_controller()
    if controller is not None:
        return controller.migrate_legacy(raw, is_overlay=is_overlay)

    cfg = deepcopy(raw) if isinstance(raw, dict) else {}
    if not cfg:
        return {"enabled": True} if not is_overlay else {}

    had_mode = isinstance(cfg.get("mode"), str) and bool(str(cfg.get("mode")).strip())
    legacy_pm = cfg.pop("permission_mode", None)
    if cfg.get("enabled") is False and not is_overlay:
        cfg["enabled"] = True
        if not had_mode:
            cfg["mode"] = "full_access"
    if not had_mode:
        if isinstance(legacy_pm, str) and legacy_pm.strip().lower() == "strict":
            cfg["mode"] = "strict"
        elif not is_overlay and "mode" not in cfg:
            cfg["mode"] = "auto"
    _normalize_tools_to_lists(cfg)
    cfg.pop("defaults", None)
    return cfg


def _compose_effective_local(
    global_cfg: dict[str, Any] | None,
    user_cfg: dict[str, Any] | None = None,
    session_cfg: dict[str, Any] | None = None,
) -> SimpleNamespace:
    global_migrated = migrate_legacy_permissions(global_cfg, is_overlay=False)
    user_migrated = migrate_legacy_permissions(user_cfg, is_overlay=True) if user_cfg else {}
    session_migrated = (
        migrate_legacy_permissions(session_cfg, is_overlay=True) if session_cfg else {}
    )
    session_migrated.pop("deny_tools", None)
    session_migrated.pop("ask_tools", None)
    session_migrated.pop("tools", None)

    raw_mode = user_migrated.get("mode") if isinstance(user_migrated.get("mode"), str) else None
    if not raw_mode:
        raw_mode = (
            global_migrated.get("mode")
            if isinstance(global_migrated.get("mode"), str)
            else None
        )
    mode = normalize_permission_mode(raw_mode)
    preset = deepcopy(_FALLBACK_MODE_PRESETS[mode])

    deny = _as_str_list(global_migrated.get("deny_tools")) + _as_str_list(
        user_migrated.get("deny_tools")
    )
    ask = _as_str_list(global_migrated.get("ask_tools")) + _as_str_list(
        user_migrated.get("ask_tools")
    )
    allow = (
        _as_str_list(global_migrated.get("allow_tools"))
        + _as_str_list(user_migrated.get("allow_tools"))
        + _as_str_list(session_migrated.get("allow_tools"))
    )
    deny_unique = list(dict.fromkeys(deny))
    deny_set = set(deny_unique)
    ask_unique = [name for name in dict.fromkeys(ask) if name not in deny_set]
    ask_set = set(ask_unique)
    allow_unique = [
        name for name in dict.fromkeys(allow) if name not in deny_set and name not in ask_set
    ]

    out: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "defaults": deepcopy(preset["defaults"]),
        "file_guard": deepcopy(preset["file_guard"]),
        "sandbox_intent": preset["sandbox_intent"],
    }
    if deny_unique:
        out["deny_tools"] = deny_unique
    if ask_unique:
        out["ask_tools"] = ask_unique
    if allow_unique:
        out["allow_tools"] = allow_unique
    if mode == "strict":
        _inject_fallback_path_tool_allows(out)
    _project_tool_lists(out)
    return SimpleNamespace(
        permissions=out,
        sandbox_intent=preset["sandbox_intent"],
        mode=mode,
    )


def get_user_permissions_path() -> Path:
    from jiuwenswarm.common.utils import get_config_dir

    return get_config_dir() / "user_permissions.yaml"


def get_session_permissions_path(session_id: str) -> Path:
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return get_agent_sessions_dir() / sid / "session_permissions.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        from jiuwenswarm.common.config import _load_yaml_round_trip

        data = _load_yaml_round_trip(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("[PermissionsLayers] load_yaml_failed path=%s", path, exc_info=True)
        return {}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    from jiuwenswarm.common.config import _dump_yaml_round_trip

    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_yaml_round_trip(path, data)


def _permissions_section(raw: dict[str, Any]) -> dict[str, Any]:
    """支持整文件为 permissions，或 ``{permissions: {...}}``。"""
    if not isinstance(raw, dict):
        return {}
    if "permissions" in raw and isinstance(raw.get("permissions"), dict):
        return deepcopy(raw["permissions"])
    # 整文件即 overlay（无外层键）
    overlay_keys = {
        "allow_tools",
        "ask_tools",
        "deny_tools",
        "rules",
        "approval_overrides",
        "file_guard",
        "network",
    }
    if overlay_keys & raw.keys():
        return deepcopy(raw)
    return {}


def load_global_permissions() -> dict[str, Any]:
    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    perms = cfg.get("permissions") if isinstance(cfg, dict) else None
    return deepcopy(perms) if isinstance(perms, dict) else {}


def load_user_permissions() -> dict[str, Any]:
    return _permissions_section(_load_yaml(get_user_permissions_path()))


def load_session_permissions(session_id: str | None) -> dict[str, Any]:
    if not session_id or not str(session_id).strip():
        return {}
    try:
        path = get_session_permissions_path(str(session_id).strip())
    except ValueError:
        return {}
    return _permissions_section(_load_yaml(path))


def compose_host_effective_permissions(
    *,
    session_id: str | None = None,
    global_permissions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合成 Host 侧 EffectivePermissions.permissions（含 mode 注入字段）。"""
    global_cfg = global_permissions if isinstance(global_permissions, dict) else load_global_permissions()
    user_cfg = load_user_permissions()
    session_cfg = load_session_permissions(session_id)
    compose_fn = _import_core_compose()
    if compose_fn is not None:
        effective = compose_fn(
            global_cfg,
            user_permissions=user_cfg or None,
            session_permissions=session_cfg or None,
        )
    else:
        logger.warning("[PermissionsLayers] compose_effective_permissions missing; using local fallback")
        effective = _compose_effective_local(
            global_cfg,
            user_cfg or None,
            session_cfg or None,
        )
    return deepcopy(effective.permissions)


def get_sandbox_intent(*, session_id: str | None = None) -> str:
    compose_fn = _import_core_compose()
    if compose_fn is not None:
        effective = compose_fn(
            load_global_permissions(),
            user_permissions=load_user_permissions() or None,
            session_permissions=load_session_permissions(session_id) or None,
        )
    else:
        effective = _compose_effective_local(
            load_global_permissions(),
            load_user_permissions() or None,
            load_session_permissions(session_id) or None,
        )
    return str(effective.sandbox_intent)


def normalize_permission_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in _VALID_MODES:
        return mode
    return "auto"


def migrate_and_write_global_permissions() -> dict[str, Any]:
    """读取 Global、迁移旧字段并写回 config.yaml（幂等）。

    - ``enabled: false`` → ``enabled: true`` + ``mode: full_access``
    - 旧 ``permission_mode`` → ``mode``
    - 顶层 ``defaults`` 删除；``tools`` allow/ask/deny 迁到 user_permissions.yaml
    """
    from jiuwenswarm.common.config import _dump_yaml_round_trip, _load_yaml_round_trip
    from jiuwenswarm.common.utils import get_config_file

    yaml_path = get_config_file()
    data = _load_yaml_round_trip(yaml_path)
    if not isinstance(data, dict):
        data = {}
    raw = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    migrated = migrate_legacy_permissions(deepcopy(raw), is_overlay=False)

    # 将 allow_tools / ask_tools / deny_tools 从 Global 迁到 User（设计：仅 User 写整工具名单）
    user_path = get_user_permissions_path()
    user_raw = _load_yaml(user_path)
    user_section = _permissions_section(user_raw) if user_raw else {}
    if not user_section and isinstance(user_raw.get("permissions"), dict):
        user_section = deepcopy(user_raw["permissions"])

    moved_allow = list(migrated.pop("allow_tools", None) or [])
    moved_ask = list(migrated.pop("ask_tools", None) or [])
    moved_deny = list(migrated.pop("deny_tools", None) or [])
    # Global 内旧 approval_overrides 迁 User
    global_overrides = migrated.pop("approval_overrides", None)
    if isinstance(global_overrides, list) and global_overrides:
        existing = user_section.get("approval_overrides")
        if not isinstance(existing, list):
            existing = []
        seen = {entry.get("id") for entry in existing if isinstance(entry, dict)}
        for item in global_overrides:
            if not isinstance(item, dict):
                continue
            oid = item.get("id")
            if isinstance(oid, str) and oid in seen:
                continue
            existing.append(deepcopy(item))
            if isinstance(oid, str):
                seen.add(oid)
        user_section["approval_overrides"] = existing

    if moved_allow or moved_ask or moved_deny:
        allow = list(user_section.get("allow_tools") or [])
        ask = list(user_section.get("ask_tools") or [])
        deny = list(user_section.get("deny_tools") or [])
        for name in moved_allow:
            if name not in allow and name not in ask and name not in deny:
                allow.append(name)
        for name in moved_ask:
            if name not in ask and name not in deny:
                ask.append(name)
            if name in allow:
                allow.remove(name)
        for name in moved_deny:
            if name not in deny:
                deny.append(name)
            if name in ask:
                ask.remove(name)
            if name in allow:
                allow.remove(name)
        if allow:
            user_section["allow_tools"] = allow
        if ask:
            user_section["ask_tools"] = ask
        if deny:
            user_section["deny_tools"] = deny

    # 产品 Global 不写顶层 defaults / tools allow 名单
    migrated.pop("defaults", None)
    migrated.pop("tools", None)
    migrated["enabled"] = True
    if "mode" not in migrated:
        migrated["mode"] = "auto"

    data["permissions"] = migrated
    _dump_yaml_round_trip(yaml_path, data)

    if user_section:
        _dump_yaml(user_path, {"permissions": user_section})

    return migrated


def update_permissions_mode(mode: str) -> str:
    """写入 Global ``permissions.mode``，并保证 ``enabled: true``。"""
    from jiuwenswarm.common.config import _dump_yaml_round_trip, _load_yaml_round_trip
    from jiuwenswarm.common.utils import get_config_file

    normalized = normalize_permission_mode(mode)
    yaml_path = get_config_file()
    data = _load_yaml_round_trip(yaml_path)
    if not isinstance(data, dict):
        data = {}
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms
    perms["enabled"] = True
    perms["mode"] = normalized
    # 旧字段不再作为产品源
    perms.pop("permission_mode", None)
    _dump_yaml_round_trip(yaml_path, data)
    return normalized


def get_permissions_mode() -> str:
    perms = load_global_permissions()
    if isinstance(perms.get("mode"), str) and perms["mode"].strip():
        return normalize_permission_mode(perms["mode"])
    # 兼容未迁移：enabled false ≈ full_access
    if perms.get("enabled") is False:
        return "full_access"
    legacy = str(perms.get("permission_mode") or "").strip().lower()
    if legacy == "strict":
        return "strict"
    return "auto"


def get_user_tools_map() -> dict[str, str]:
    """Return the User tool lists projected as ``{name: allow|ask|deny}``."""
    user_permissions = load_user_permissions()
    tools: dict[str, str] = {}
    for name in user_permissions.get("allow_tools") or []:
        if isinstance(name, str) and name.strip():
            tools[name.strip()] = "allow"
    for name in user_permissions.get("ask_tools") or []:
        if isinstance(name, str) and name.strip():
            tools[name.strip()] = "ask"
    for name in user_permissions.get("deny_tools") or []:
        if isinstance(name, str) and name.strip():
            tools[name.strip()] = "deny"
    return tools


def set_user_tool_level(tool_name: str, level: str) -> dict[str, str]:
    """Set one tool's User-level permission, removing it from other levels."""
    name = str(tool_name).strip()
    normalized_level = str(level).strip().lower()
    if normalized_level not in {"allow", "ask", "deny"}:
        raise ValueError("level must be allow|ask|deny")
    if not name:
        raise ValueError("tool name must be non-empty")

    path = get_user_permissions_path()
    section = _permissions_section(_load_yaml(path))
    for key in ("allow_tools", "ask_tools", "deny_tools"):
        values = [
            tool
            for tool in (section.get(key) or [])
            if isinstance(tool, str) and tool != name
        ]
        if values:
            section[key] = values
        else:
            section.pop(key, None)
    key = {
        "allow": "allow_tools",
        "ask": "ask_tools",
        "deny": "deny_tools",
    }[normalized_level]
    section[key] = list(section.get(key) or []) + [name]
    _dump_yaml(path, {"permissions": section})
    return get_user_tools_map()


def delete_user_tool(tool_name: str) -> bool:
    """Remove one tool from all User permission levels."""
    name = str(tool_name).strip()
    if not name:
        raise ValueError("tool name must be non-empty")

    path = get_user_permissions_path()
    section = _permissions_section(_load_yaml(path))
    found = False
    for key in ("allow_tools", "ask_tools", "deny_tools"):
        raw = section.get(key) or []
        if not isinstance(raw, list):
            continue
        if any(isinstance(tool, str) and tool == name for tool in raw):
            found = True
        values = [tool for tool in raw if isinstance(tool, str) and tool != name]
        if values:
            section[key] = values
        else:
            section.pop(key, None)
    if found:
        _dump_yaml(path, {"permissions": section})
    return found


def replace_user_tools_map(tools: dict[str, str]) -> dict[str, str]:
    """Full replace of User tool lists from ``{name: allow|ask|deny}``."""
    if not isinstance(tools, dict):
        raise ValueError("tools must be object")

    levels: dict[str, list[str]] = {
        "allow": [],
        "ask": [],
        "deny": [],
    }
    for tool_name, level in tools.items():
        name = str(tool_name).strip()
        normalized_level = str(level).strip().lower()
        if not name:
            raise ValueError("tool name must be non-empty")
        if normalized_level not in levels:
            raise ValueError("level must be allow|ask|deny")
        levels[normalized_level].append(name)

    path = get_user_permissions_path()
    section = _permissions_section(_load_yaml(path))
    for key in ("allow_tools", "ask_tools", "deny_tools"):
        section.pop(key, None)
    for level, key in (
        ("allow", "allow_tools"),
        ("ask", "ask_tools"),
        ("deny", "deny_tools"),
    ):
        if levels[level]:
            section[key] = levels[level]
    _dump_yaml(path, {"permissions": section})
    return get_user_tools_map()


def _mutate_allow_list(section: dict[str, Any], name: str) -> bool:
    """Append one normalized tool name to an overlay's ``allow_tools`` list."""
    tool_name = str(name or "").strip()
    if not tool_name:
        return False
    allow_tools = section.get("allow_tools")
    if not isinstance(allow_tools, list):
        allow_tools = []
        section["allow_tools"] = allow_tools
    if tool_name not in allow_tools:
        allow_tools.append(tool_name)
    ask_tools = section.get("ask_tools")
    if isinstance(ask_tools, list) and tool_name in ask_tools:
        section["ask_tools"] = [item for item in ask_tools if item != tool_name]
        if not section["ask_tools"]:
            section.pop("ask_tools")
    return True


def append_allow_tool(
    tool_name: str,
    *,
    scope: str,
    session_id: str | None = None,
) -> bool:
    """Persist one HITL tool allow to the requested User or Session overlay."""
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in {"user", "session"}:
        return False
    try:
        if normalized_scope == "user":
            path = get_user_permissions_path()
        else:
            if not session_id or not str(session_id).strip():
                return False
            path = get_session_permissions_path(str(session_id).strip())
        current = _permissions_section(_load_yaml(path))
        if normalized_scope == "session":
            current.pop("deny_tools", None)
            current.pop("ask_tools", None)
            current.pop("tools", None)
        if not _mutate_allow_list(current, tool_name):
            return False
        _dump_yaml(path, {"permissions": current})
        return True
    except Exception:
        logger.warning(
            "[PermissionsLayers] append_allow_tool_failed scope=%s",
            normalized_scope,
            exc_info=True,
        )
        return False


def _merge_file_guard_path_entries(
    section: dict[str, Any],
    entries: list[Any],
) -> None:
    """Merge incremental ``file_guard.paths`` entries into a layer overlay."""
    if not entries:
        return
    cur_fg = section.get("file_guard") if isinstance(section.get("file_guard"), dict) else {}
    cur_fg = dict(cur_fg)
    paths = cur_fg.get("paths")
    if not isinstance(paths, list):
        paths = []
    by_path: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for existing in paths:
        if not isinstance(existing, dict):
            continue
        key = str(existing.get("path") or "").replace("\\", "/").rstrip("/")
        if not key:
            continue
        by_path[key] = deepcopy(existing)
        order.append(key)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("path") or "").replace("\\", "/").rstrip("/")
        if not key:
            continue
        if key in by_path:
            prev = by_path[key]
            merged = dict(prev)
            for axis in ("read", "write", "exec"):
                new_v = str(entry.get(axis) or "").strip().lower()
                old_v = str(prev.get(axis) or "").strip().lower()
                if new_v == "allow" or (new_v and old_v != "allow"):
                    if new_v:
                        merged[axis] = new_v
            if entry.get("match"):
                merged["match"] = entry.get("match")
            by_path[key] = merged
        else:
            by_path[key] = deepcopy(entry)
            order.append(key)
    cur_fg["paths"] = [by_path[k] for k in order if k in by_path]
    section["file_guard"] = cur_fg


def persist_user_overlay_from_effective(effective: dict[str, Any]) -> bool:
    """将 HITL 永久允许的 pattern / file_guard 增量写入 user_permissions.yaml。"""
    try:
        path = get_user_permissions_path()
        allow_tools_added = effective.get("_allow_tools_added")
        if isinstance(allow_tools_added, list):
            for tool_name in allow_tools_added:
                if not append_allow_tool(tool_name, scope="user"):
                    return False
        current = _permissions_section(_load_yaml(path))
        overrides = effective.get("approval_overrides")
        if isinstance(overrides, list):
            current["approval_overrides"] = deepcopy(overrides)
        added_paths = effective.get("_file_guard_paths_added")
        if isinstance(added_paths, list) and added_paths:
            _merge_file_guard_path_entries(current, added_paths)
        # 同步 ask_tools / deny_tools（若 effective 仍带列表）
        for key in ("ask_tools", "deny_tools"):
            if isinstance(effective.get(key), list):
                current[key] = deepcopy(effective[key])
        _dump_yaml(path, {"permissions": current})
        return True
    except Exception:
        logger.warning("[PermissionsLayers] persist_user_failed", exc_info=True)
        return False


def persist_session_overlay_from_effective(
    effective: dict[str, Any],
    *,
    session_id: str | None,
) -> bool:
    """将会话内记住写入 session_permissions.yaml（pattern / paths / allow_tools）。"""
    sid = (session_id or "").strip() or str(effective.get("_persist_session_id") or "").strip()
    if not sid:
        logger.info("[PermissionsLayers] persist_session_skip reason=no_session_id")
        return False
    try:
        path = get_session_permissions_path(sid)
        allow_tools_added = effective.get("_allow_tools_added")
        if isinstance(allow_tools_added, list):
            for tool_name in allow_tools_added:
                if not append_allow_tool(
                    tool_name,
                    scope="session",
                    session_id=sid,
                ):
                    return False
        current = _permissions_section(_load_yaml(path))
        overrides = effective.get("approval_overrides")
        if isinstance(overrides, list):
            # Session 层只保留 allow overrides；不写 deny/ask tools
            current["approval_overrides"] = [
                deepcopy(entry)
                for entry in overrides
                if isinstance(entry, dict) and str(entry.get("action") or "allow").lower() == "allow"
            ]
        added_paths = effective.get("_file_guard_paths_added")
        if isinstance(added_paths, list) and added_paths:
            _merge_file_guard_path_entries(current, added_paths)
        current.pop("deny_tools", None)
        current.pop("ask_tools", None)
        current.pop("tools", None)
        _dump_yaml(path, {"permissions": current})
        logger.info(
            "[PermissionsLayers] persist_session_ok session_id=%s path=%s "
            "allow_tools=%s file_guard_paths_added=%s",
            sid,
            path,
            allow_tools_added if isinstance(allow_tools_added, list) else [],
            len(added_paths) if isinstance(added_paths, list) else 0,
        )
        return True
    except Exception:
        logger.warning("[PermissionsLayers] persist_session_failed", exc_info=True)
        return False


__all__ = [
    "append_allow_tool",
    "compose_host_effective_permissions",
    "delete_user_tool",
    "get_permissions_mode",
    "get_sandbox_intent",
    "get_session_permissions_path",
    "get_user_permissions_path",
    "get_user_tools_map",
    "load_global_permissions",
    "load_session_permissions",
    "load_user_permissions",
    "migrate_and_write_global_permissions",
    "normalize_permission_mode",
    "persist_session_overlay_from_effective",
    "persist_user_overlay_from_effective",
    "replace_user_tools_map",
    "set_user_tool_level",
    "update_permissions_mode",
]
