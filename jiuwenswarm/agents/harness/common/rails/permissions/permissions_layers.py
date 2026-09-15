# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load and persist User / Session permission overlays."""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@contextmanager
def permission_storage_lock(session_id: str | None = None, *, lock_timeout: float = 10.0):
    """Lock cooperative writers in Global, User, Session order; never reenter."""
    import portalocker

    from jiuwenswarm.common.config import _config_lock_path, config_write_lock

    with config_write_lock(lock_timeout=lock_timeout), ExitStack() as stack:
        paths = [user_permissions_path()]
        if session_id and str(session_id).strip():
            paths.append(session_permissions_path(session_id))
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(portalocker.Lock(str(_config_lock_path(path)), timeout=lock_timeout))
        yield

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


def _load_yaml_dict(path: Path, *, strict: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        from jiuwenswarm.common.config import _load_yaml_round_trip

        data = _load_yaml_round_trip(path)
    except Exception:
        if strict:
            raise
        logger.warning("[permissions.layers] load failed path=%s", path, exc_info=True)
        return {}
    if strict and data is not None and not isinstance(data, dict):
        raise ValueError(f"permission file must contain a mapping: {path}")
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


def read_permission_layers_locked(session_id: str | None = None) -> tuple[dict[str, Any], ...]:
    """Read strict, fresh layers while the caller owns permission_storage_lock."""
    from copy import deepcopy

    from jiuwenswarm.common import config

    global_data = _load_yaml_dict(config.CONFIG_YAML_PATH, strict=True)
    global_perms = global_data.get("permissions")
    if not isinstance(global_perms, dict):
        raise ValueError("Global permissions must contain a mapping")
    user = _as_permissions_section(_load_yaml_dict(user_permissions_path(), strict=True))
    session = (
        _as_permissions_section(_load_yaml_dict(session_permissions_path(session_id), strict=True))
        if session_id else {}
    )
    return deepcopy(global_perms), deepcopy(user), deepcopy(session)


def capture_permission_layers(session_id: str | None = None) -> tuple[dict[str, Any], ...]:
    """Return independent Global/User/Session/effective dictionaries from one locked read."""
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )

    with permission_storage_lock(session_id):
        global_perms, user, session = read_permission_layers_locked(session_id)
        effective = compose_host_effective_permissions(
            global_permissions=global_perms, user_permissions=user, session_permissions=session,
        )
        return global_perms, user, session, effective


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
    with permission_storage_lock(session_id):
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
    with permission_storage_lock(session_id):
        overlay = overlay_from_effective(effective, session=True)
        current = _merge_overlay_into_current(
            load_session_permissions(session_id), overlay, session=True
        )
        return _dump_yaml_dict(session_permissions_path(str(session_id).strip()), current)


def save_user_permissions(data: dict[str, Any]) -> bool:
    return update_user_permissions(lambda _: dict(data) if isinstance(data, dict) else {})


def save_user_permissions_locked(data: dict[str, Any]) -> bool:
    """Save a validated User overlay; caller must hold permission_storage_lock."""
    return _dump_yaml_dict(user_permissions_path(), data)


def update_user_permissions(mutator) -> bool:
    """Apply a User overlay mutation to the latest document under the shared locks."""
    try:
        with permission_storage_lock():
            current = load_user_permissions()
            updated = mutator(current)
            return _dump_yaml_dict(user_permissions_path(), updated)
    except Exception:
        logger.warning("[permissions.layers] user update failed", exc_info=True)
        return False


__all__ = [
    "capture_permission_layers",
    "load_global_permissions",
    "load_session_permissions",
    "load_user_permissions",
    "overlay_from_effective",
    "permission_storage_lock",
    "persist_session_overlay_from_effective",
    "persist_user_overlay_from_effective",
    "save_user_permissions",
    "save_user_permissions_locked",
    "update_user_permissions",
    "session_permissions_path",
    "user_permissions_path",
]
