# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load and persist User / Session permission overlays."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_LEVELS = frozenset({"allow", "ask", "deny"})
_LIST_KEYS = ("allow_tools", "ask_tools", "deny_tools")
_OVERLAY_KEYS = frozenset(
    {
        "allow_tools",
        "ask_tools",
        "deny_tools",
        "approval_overrides",
        "file_guard",
        "tools",
        "rules",
        "net_guard",
        "shell_guard",
    }
)
_ENGINE_ONLY_KEYS = frozenset({"net_guard", "shell_guard", "defaults", "tools", "enabled"})


def user_permissions_path() -> Path:
    from jiuwenswarm.common.utils import get_config_dir

    return get_config_dir() / "user_permissions.yaml"


def session_permissions_path(session_id: str) -> Path:
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return get_agent_sessions_dir() / sid / "session_permissions.yaml"


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        from jiuwenswarm.common.config import _load_yaml_round_trip

        data = _load_yaml_round_trip(path)
    except Exception:
        logger.warning("[permissions.layers] load failed path=%s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _dump_yaml_dict(path: Path, data: dict[str, Any]) -> bool:
    try:
        from jiuwenswarm.common.config import _dump_yaml_round_trip

        path.parent.mkdir(parents=True, exist_ok=True)
        _dump_yaml_round_trip(path, data)
        return True
    except Exception:
        logger.warning("[permissions.layers] dump failed path=%s", path, exc_info=True)
        return False


def _as_permissions_section(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("permissions")
    if isinstance(inner, dict) and not any(key in raw for key in _OVERLAY_KEYS):
        return dict(inner)
    return dict(raw)


def _scalar_level(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("*")
    if not isinstance(raw, str):
        return None
    level = raw.strip().lower()
    return level if level in _TOOL_LEVELS else None


def _str_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _tool_names_at_level(layer: dict[str, Any], level: str) -> set[str]:
    names: set[str] = set()
    tools = layer.get("tools")
    if isinstance(tools, dict):
        for name, raw in tools.items():
            if isinstance(name, str) and name.strip() and _scalar_level(raw) == level:
                names.add(name.strip())
    key = {"allow": "allow_tools", "ask": "ask_tools", "deny": "deny_tools"}[level]
    names.update(_str_names(layer.get(key)))
    return names


def _override_ids(raw: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(raw, list):
        return ids
    for item in raw:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("id") or "").strip()
        if oid:
            ids.add(oid)
    return ids


def _path_key(item: dict[str, Any]) -> tuple[str, str]:
    path = str(item.get("path") or "").replace("\\", "/").rstrip("/").casefold()
    match = str(item.get("match") or "prefix").strip().lower() or "prefix"
    return (path, match)


def _path_keys(layer: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    fg = layer.get("file_guard")
    if not isinstance(fg, dict):
        return keys
    for item in fg.get("paths") or []:
        if isinstance(item, dict):
            keys.add(_path_key(item))
    return keys


def load_global_permissions() -> dict[str, Any]:
    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    perms = cfg.get("permissions") if isinstance(cfg, dict) else {}
    return dict(perms) if isinstance(perms, dict) else {}


def load_user_permissions() -> dict[str, Any]:
    return _as_permissions_section(_load_yaml_dict(user_permissions_path()))


def load_session_permissions(session_id: str | None) -> dict[str, Any]:
    if not session_id or not str(session_id).strip():
        return {}
    try:
        return _as_permissions_section(
            _load_yaml_dict(session_permissions_path(str(session_id).strip()))
        )
    except ValueError:
        return {}


def overlay_from_effective(
    effective: dict[str, Any],
    *,
    session: bool,
    global_permissions: dict[str, Any] | None = None,
    user_permissions: dict[str, Any] | None = None,
    session_permissions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a User/Session overlay: Engine snapshot minus lower layers."""
    src = effective if isinstance(effective, dict) else {}
    global_layer = _as_permissions_section(
        global_permissions if global_permissions is not None else load_global_permissions()
    )
    if session:
        user_layer = _as_permissions_section(
            user_permissions if user_permissions is not None else load_user_permissions()
        )
        skip_layer = {}
    else:
        user_layer = {}
        skip_layer = _as_permissions_section(session_permissions or {})

    base_allow = _tool_names_at_level(global_layer, "allow") | _tool_names_at_level(
        user_layer, "allow"
    ) | _tool_names_at_level(skip_layer, "allow")
    base_ask = _tool_names_at_level(global_layer, "ask")
    base_deny = _tool_names_at_level(global_layer, "deny")
    base_override_ids = (
        _override_ids(global_layer.get("approval_overrides"))
        | _override_ids(user_layer.get("approval_overrides"))
        | _override_ids(skip_layer.get("approval_overrides"))
    )
    base_path_keys = _path_keys(global_layer) | _path_keys(user_layer) | _path_keys(skip_layer)

    out: dict[str, Any] = {}
    allow = [name for name in _str_names(src.get("allow_tools")) if name not in base_allow]
    allow = list(dict.fromkeys(allow))
    if allow:
        out["allow_tools"] = allow

    if not session:
        ask = [name for name in _str_names(src.get("ask_tools")) if name not in base_ask]
        deny = [name for name in _str_names(src.get("deny_tools")) if name not in base_deny]
        ask = list(dict.fromkeys(ask))
        deny = list(dict.fromkeys(deny))
        if ask:
            out["ask_tools"] = ask
        if deny:
            out["deny_tools"] = deny

    overrides = []
    for item in src.get("approval_overrides") or []:
        if not isinstance(item, dict):
            continue
        override_id = str(item.get("id") or "").strip()
        if not override_id or override_id in base_override_ids:
            continue
        overrides.append(item)
    if overrides:
        out["approval_overrides"] = overrides

    fg = src.get("file_guard")
    if isinstance(fg, dict):
        paths = []
        for item in fg.get("paths") or []:
            if not isinstance(item, dict) or item.get("layer") == "builtin":
                continue
            if _path_key(item) in base_path_keys:
                continue
            paths.append(item)
        if paths:
            out["file_guard"] = {"paths": paths}
    return out


def _merge_overlay_into_current(
    current: dict[str, Any],
    overlay: dict[str, Any],
    *,
    session: bool,
) -> dict[str, Any]:
    out = dict(current) if isinstance(current, dict) else {}
    for key in _ENGINE_ONLY_KEYS:
        out.pop(key, None)
    for key in _LIST_KEYS:
        out.pop(key, None)
    out.pop("approval_overrides", None)
    fg = out.get("file_guard")
    if isinstance(fg, dict):
        fg = dict(fg)
        fg.pop("paths", None)
        if fg:
            out["file_guard"] = fg
        else:
            out.pop("file_guard", None)
    out.update(overlay)
    if session:
        out.pop("deny_tools", None)
        out.pop("ask_tools", None)
        out.pop("enabled", None)
    return out


def persist_user_overlay_from_effective(
    effective: dict[str, Any],
    session_id: str | None = None,
) -> bool:
    overlay = overlay_from_effective(
        effective,
        session=False,
        session_permissions=load_session_permissions(session_id) if session_id else {},
    )
    current = _merge_overlay_into_current(load_user_permissions(), overlay, session=False)
    return _dump_yaml_dict(user_permissions_path(), current)


def persist_session_overlay_from_effective(session_id: str, effective: dict[str, Any]) -> bool:
    if not session_id or not str(session_id).strip():
        return False
    overlay = overlay_from_effective(effective, session=True)
    current = _merge_overlay_into_current(
        load_session_permissions(session_id), overlay, session=True
    )
    return _dump_yaml_dict(session_permissions_path(str(session_id).strip()), current)


def save_user_permissions(data: dict[str, Any]) -> bool:
    return _dump_yaml_dict(user_permissions_path(), dict(data) if isinstance(data, dict) else {})


__all__ = [
    "load_global_permissions",
    "load_session_permissions",
    "load_user_permissions",
    "overlay_from_effective",
    "persist_session_overlay_from_effective",
    "persist_user_overlay_from_effective",
    "save_user_permissions",
    "session_permissions_path",
    "user_permissions_path",
]
