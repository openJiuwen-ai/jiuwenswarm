# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Permissions 配置加载：Agent template 槽位优先，否则回落 config.yaml。"""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import asyncio
import contextvars
import copy
import logging
import os
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

PersistScope = Literal["session", "base"]


_cached_permissions: dict[str, Any] | None = None
_cache_source: str | None = None
_session_overlays: dict[str, dict[str, Any]] = {}

PERMISSIONS_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jiuwenclaw_permissions_session_id",
    default=None,
)

# Agent 级权限基线（来自 template_ref.permissions 模板 body）；优先于进程级 yaml/DB。
PERMISSIONS_AGENT_BASE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "jiuwenclaw_permissions_agent_base",
    default=None,
)


def setup_permissions_session_scope(session_id: str | None) -> contextvars.Token:
    """绑定当前 asyncio Task 的 permissions 会话 scope（供 overlay 读写）。"""
    normalized = (session_id or "").strip() or None
    return PERMISSIONS_SESSION_ID.set(normalized)


def reset_permissions_session_scope(token: contextvars.Token) -> None:
    PERMISSIONS_SESSION_ID.reset(token)


def get_permissions_session_id() -> str | None:
    return PERMISSIONS_SESSION_ID.get()


def setup_permissions_agent_base(body: dict[str, Any] | None) -> contextvars.Token:
    """绑定当前 Task 的 Agent 级 permissions 基线（企业模板 body）。"""
    if isinstance(body, dict):
        return PERMISSIONS_AGENT_BASE.set(copy.deepcopy(body))
    return PERMISSIONS_AGENT_BASE.set(None)


def reset_permissions_agent_base(token: contextvars.Token) -> None:
    PERMISSIONS_AGENT_BASE.reset(token)


def get_permissions_agent_base() -> dict[str, Any] | None:
    body = PERMISSIONS_AGENT_BASE.get()
    if isinstance(body, dict):
        return copy.deepcopy(body)
    return None


def clear_permissions_config_cache() -> None:
    global _cached_permissions, _cache_source
    _cached_permissions = None
    _cache_source = None


def clear_session_permissions_overlay(session_id: str | None = None) -> None:
    """清除企业版会话级 runtime overlay（可选：单会话或全部）。"""
    if session_id is None:
        _session_overlays.clear()
        return
    normalized = session_id.strip()
    if normalized:
        _session_overlays.pop(normalized, None)


def _load_permissions_from_yaml() -> dict[str, Any]:
    from jiuwenswarm.common.config import get_config

    raw = (get_config() or {}).get("permissions")
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    return {}


def _set_cache(body: dict[str, Any], source: str) -> None:
    global _cached_permissions, _cache_source
    _cached_permissions = copy.deepcopy(body)
    _cache_source = source


def _resolve_session_id(session_id: str | None = None) -> str | None:
    if session_id is not None:
        normalized = session_id.strip()
        return normalized or None
    ctx = get_permissions_session_id()
    if ctx:
        return ctx.strip() or None
    return None


def _approval_override_fingerprint(item: dict[str, Any]) -> tuple[str, str]:
    pattern = str(item.get("pattern") or "").strip()
    action = str(item.get("action") or "").strip().lower()
    return pattern, action


def _deep_merge_file_guard(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if key == "global" and isinstance(value, dict):
            global_dst = dst.setdefault("global", {})
            if not isinstance(global_dst, dict):
                global_dst = {}
                dst["global"] = global_dst
            for path, rules in value.items():
                if path in global_dst and isinstance(global_dst[path], dict) and isinstance(rules, dict):
                    global_dst[path] = {**global_dst[path], **copy.deepcopy(rules)}
                else:
                    global_dst[path] = copy.deepcopy(rules)
            continue
        if key == "trusted_exec_directory" and isinstance(value, list):
            existing = dst.setdefault("trusted_exec_directory", [])
            if not isinstance(existing, list):
                existing = []
                dst["trusted_exec_directory"] = existing
            seen = {str(x) for x in existing}
            for item in value:
                normalized = str(item)
                if normalized not in seen:
                    existing.append(copy.deepcopy(item))
                    seen.add(normalized)
            continue
        if key in dst and isinstance(dst[key], dict) and isinstance(value, dict):
            _deep_merge_file_guard(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)


def _file_guard_delta(base_fg: dict[str, Any], effective_fg: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    base_global = base_fg.get("global") if isinstance(base_fg.get("global"), dict) else {}
    eff_global = effective_fg.get("global") if isinstance(effective_fg.get("global"), dict) else {}
    global_delta: dict[str, Any] = {}
    for path, rules in eff_global.items():
        base_rules = base_global.get(path)
        if base_rules != rules:
            global_delta[path] = copy.deepcopy(rules)
    if global_delta:
        delta["global"] = global_delta

    base_ted_raw = base_fg.get("trusted_exec_directory")
    base_ted = base_ted_raw if isinstance(base_ted_raw, list) else []
    eff_ted_raw = effective_fg.get("trusted_exec_directory")
    eff_ted = eff_ted_raw if isinstance(eff_ted_raw, list) else []
    base_seen = {str(x) for x in base_ted}
    ted_delta = [copy.deepcopy(x) for x in eff_ted if str(x) not in base_seen]
    if ted_delta:
        delta["trusted_exec_directory"] = ted_delta

    for key in ("workspace", "tool_bindings"):
        base_val = base_fg.get(key)
        eff_val = effective_fg.get(key)
        if eff_val is not None and eff_val != base_val:
            delta[key] = copy.deepcopy(eff_val)
    return delta


def _merge_permissions_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    if not overlay:
        return copy.deepcopy(base)

    merged = copy.deepcopy(base)

    overlay_tools = overlay.get("tools")
    if isinstance(overlay_tools, dict):
        tools = merged.get("tools")
        if not isinstance(tools, dict):
            tools = {}
            merged["tools"] = tools
        tools.update(copy.deepcopy(overlay_tools))

    overlay_overrides = overlay.get("approval_overrides")
    if isinstance(overlay_overrides, list):
        base_list = list(merged.get("approval_overrides") or [])
        existing = {
            _approval_override_fingerprint(item)
            for item in base_list
            if isinstance(item, dict)
        }
        for item in overlay_overrides:
            if not isinstance(item, dict):
                continue
            fingerprint = _approval_override_fingerprint(item)
            if fingerprint in existing:
                continue
            base_list.append(copy.deepcopy(item))
            existing.add(fingerprint)
        merged["approval_overrides"] = base_list

    overlay_fg = overlay.get("file_guard")
    if isinstance(overlay_fg, dict):
        fg = merged.get("file_guard")
        if not isinstance(fg, dict):
            fg = {}
            merged["file_guard"] = fg
        _deep_merge_file_guard(fg, overlay_fg)

    return merged


def _extract_session_overlay(base: dict[str, Any], effective: dict[str, Any]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}

    base_tools = base.get("tools") if isinstance(base.get("tools"), dict) else {}
    eff_tools = effective.get("tools") if isinstance(effective.get("tools"), dict) else {}
    tool_delta = {k: v for k, v in eff_tools.items() if base_tools.get(k) != v}
    if tool_delta:
        overlay["tools"] = copy.deepcopy(tool_delta)

    base_overrides = [
        item for item in (base.get("approval_overrides") or []) if isinstance(item, dict)
    ]
    eff_overrides = [
        item for item in (effective.get("approval_overrides") or []) if isinstance(item, dict)
    ]
    base_fps = {_approval_override_fingerprint(item) for item in base_overrides}
    runtime_overrides = [
        copy.deepcopy(item)
        for item in eff_overrides
        if _approval_override_fingerprint(item) not in base_fps
    ]
    if runtime_overrides:
        overlay["approval_overrides"] = runtime_overrides

    base_fg = base.get("file_guard") if isinstance(base.get("file_guard"), dict) else {}
    eff_fg = effective.get("file_guard") if isinstance(effective.get("file_guard"), dict) else {}
    fg_delta = _file_guard_delta(base_fg, eff_fg)
    if fg_delta:
        overlay["file_guard"] = fg_delta

    return overlay


def merge_session_permissions_overlay(
    base_config: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """将 base 配置与会话 overlay 合并（供 PermissionInterruptRail 判定）。"""
    if not is_enterprise():
        return copy.deepcopy(base_config)
    sid = _resolve_session_id(session_id)
    if not sid:
        return copy.deepcopy(base_config)
    overlay = _session_overlays.get(sid)
    if not overlay:
        return copy.deepcopy(base_config)
    return _merge_permissions_config(base_config, overlay)


def resolve_permissions_body_from_enterprise(
    enterprise_config: Any,
) -> dict[str, Any] | None:
    """从企业配置 ``permissions`` 槽位取首个启用模板的 ``body``。

    不写进程级 base 缓存；调用方用返回值作为本请求/本 Agent 的权限基线。
    无模板或无 ``body`` 时返回 ``None``，由调用方回落 yaml。
    """
    if enterprise_config is None:
        return None
    templates = getattr(enterprise_config, "permissions", None)
    if not isinstance(templates, list):
        return None
    for tpl in templates:
        if not isinstance(tpl, dict):
            continue
        if tpl.get("enabled") is False:
            continue
        body = tpl.get("body")
        if isinstance(body, dict):
            return copy.deepcopy(body)
    return None


def get_base_permissions_config(*, force_reload: bool = False) -> dict[str, Any]:
    """返回 base ``permissions`` 段（不含企业版会话 overlay）。

    若当前 Task 绑定了 Agent 级模板 body（``setup_permissions_agent_base``），
    优先返回该 body。否则回落 ``config.yaml``（不再读取实例级 permissions_config 表）。
    """
    global _cached_permissions, _cache_source

    agent_base = PERMISSIONS_AGENT_BASE.get()
    if isinstance(agent_base, dict):
        return copy.deepcopy(agent_base)

    if not force_reload and _cached_permissions is not None:
        return copy.deepcopy(_cached_permissions)

    cfg = _load_permissions_from_yaml()
    _cached_permissions = cfg
    _cache_source = "yaml"
    return copy.deepcopy(cfg)


def get_effective_permissions_config(
    *,
    force_reload: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """返回生效的 ``permissions`` 段（企业版：base + 会话 overlay；其他：YAML/base）。"""
    base = get_base_permissions_config(force_reload=force_reload)
    if not is_enterprise():
        return base
    return merge_session_permissions_overlay(base, session_id=session_id)


def apply_permissions_config_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """刷新本进程 permissions base 缓存。

    实例级 ``permissions_config`` 表已移除；payload 仅用于显式注入 body 或回落 yaml。
    不清理各会话 runtime overlay。
    """
    old_effective = copy.deepcopy(_cached_permissions) if _cached_permissions is not None else None
    clear_permissions_config_cache()

    if not payload or payload.get("op") == "delete":
        effective = _load_permissions_from_yaml()
        _set_cache(effective, "yaml")
    elif isinstance(payload.get("body"), dict):
        effective = copy.deepcopy(payload["body"])
        _set_cache(effective, "memory")
    else:
        effective = _load_permissions_from_yaml()
        _set_cache(effective, "yaml")

    # Skill 动态授权联动：功能开关运行中关闭时清空全部 Grant；普通热更新不清。
    try:
        from openjiuwen.harness.security.skill_authorization import (
            sync_grants_on_permissions_reload,
        )

        sync_grants_on_permissions_reload(old_effective, effective)
    except Exception:  # noqa: BLE001 — Grant 同步失败不掩盖配置热更新结果
        logger.warning(
            "[permissions_config] skill_authorization grant sync failed",
            exc_info=True,
        )

    return copy.deepcopy(effective)


async def reload_permissions_from_gateway_db() -> dict[str, Any]:
    """冷启动：刷新 permissions 缓存（仅 yaml；Agent 模板在请求路径注入）。"""
    return apply_permissions_config_payload({"op": "delete"})


def persist_permissions_mutate(
    mutate_fn: Callable[[dict[str, Any]], None],
    *,
    session_id: str | None = None,
    persist_scope: PersistScope = "session",
    source: str = "runtime_persist",
) -> dict[str, Any]:
    """变更 permissions 并持久化。

    - 标准版：写 ``config.yaml``。
    - 企业版 + ``persist_scope='session'``：仅更新指定会话的内存 overlay。
    - 企业版 + ``persist_scope='base'``：仅更新进程内存缓存（不再写 permissions_config 表；
      Agent 级策略请改 permissions_template）。
    """
    if is_enterprise() and persist_scope == "session":
        sid = _resolve_session_id(session_id)
        if not sid:
            logger.warning(
                "[permissions_config] session persist skipped: no session_id",
            )
            return get_effective_permissions_config()

        base = get_base_permissions_config()
        overlay = _session_overlays.get(sid, {})
        effective = _merge_permissions_config(base, overlay)
        mutate_fn(effective)
        _session_overlays[sid] = _extract_session_overlay(base, effective)
        logger.info(
            "[permissions_config] session overlay updated session_id=%s",
            sid,
        )
        return copy.deepcopy(effective)

    permissions = get_base_permissions_config()
    if not isinstance(permissions, dict):
        permissions = {}
    else:
        permissions = copy.deepcopy(permissions)

    old_permissions = copy.deepcopy(permissions)
    mutate_fn(permissions)

    if is_enterprise():
        _set_cache(permissions, "memory")
        logger.info(
            "[permissions_config] enterprise base persist kept in-memory only "
            "(source=%s); use permissions_template for Agent-level policy",
            source,
        )
    else:
        _persist_permissions_to_yaml(permissions)

    # Skill 动态授权联动：功能开关运行中关闭时清空全部 Grant；普通热更新不清。
    # 所有 base 写路径（permissions_config_rpc / 权限 Rail 永久记住）都汇聚于此，
    # 只有这里能同时拿到变更前后的配置快照。
    try:
        from openjiuwen.harness.security.skill_authorization import (
            sync_grants_on_permissions_reload,
        )

        sync_grants_on_permissions_reload(old_permissions, permissions)
    except Exception:  # noqa: BLE001 — Grant 同步失败不掩盖配置变更结果
        logger.warning(
            "[permissions_config] skill_authorization grant sync failed",
            exc_info=True,
        )

    return permissions


def _persist_permissions_to_yaml(permissions: dict[str, Any]) -> None:
    from jiuwenswarm.common.config import (
        CONFIG_YAML_PATH,
        dump_yaml_round_trip,
        load_yaml_round_trip,
    )

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    data["permissions"] = permissions
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    clear_permissions_config_cache()


def _event_loop_is_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_async(awaitable: Any) -> Any:
    """仅在无运行中 event loop 的同步上下文中执行协程。"""
    if _event_loop_is_running():
        raise RuntimeError(
            "permissions config async operation invoked while event loop is running",
        )
    return asyncio.run(awaitable)
