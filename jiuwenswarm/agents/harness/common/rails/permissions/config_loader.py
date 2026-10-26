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

# 标准版 yaml 保留键：按 agent_id 分桶的 permissions 覆盖段（稀疏），不是工具名。
PERMISSIONS_AGENTS_KEY = "agents"
# 种子 id：未建桶时专属落盘仍可写入 agents[id]（按需稀疏，非整段克隆全局）。
SEED_PERMISSIONS_AGENT_IDS: tuple[str, ...] = ("office-excel",)

_cached_global: dict[str, Any] | None = None
_cached_agents: dict[str, dict[str, Any]] = {}
_cached_template_permissions: dict[str, Any] | None = None
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
    """绑定当前 Task 的 Agent 级 permissions 基线（企业模板或标准版 yaml agents[id]）。"""
    if isinstance(body, dict):
        return PERMISSIONS_AGENT_BASE.set(sanitize_agent_permissions_body(body))
    return PERMISSIONS_AGENT_BASE.set(None)


def reset_permissions_agent_base(token: contextvars.Token) -> None:
    PERMISSIONS_AGENT_BASE.reset(token)


def get_permissions_agent_base() -> dict[str, Any] | None:
    body = PERMISSIONS_AGENT_BASE.get()
    if isinstance(body, dict):
        return sanitize_agent_permissions_body(body)
    return None


def clear_permissions_config_cache() -> None:
    global _cached_global, _cached_agents, _cached_template_permissions, _cache_source
    _cached_global = None
    _cached_agents = {}
    _cached_template_permissions = None
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
    from jiuwenswarm.common.utils import load_yaml_dict

    raw = (get_config() or {}).get("permissions")
    result = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    # 稀疏 override 与模板 deep-merge 会丢掉模板中不存在的键；
    # ``permissions.agents`` 只写在用户 config.yaml，必须从原文件补回。
    user_permissions = load_yaml_dict(_permissions_yaml_path()).get("permissions")
    if isinstance(user_permissions, dict):
        if not is_enterprise():
            agents_table = user_permissions.get(PERMISSIONS_AGENTS_KEY)
            if isinstance(agents_table, dict):
                result[PERMISSIONS_AGENTS_KEY] = copy.deepcopy(agents_table)
        user_tools = user_permissions.get("tools")
        if isinstance(user_tools, dict) and user_tools:
            merged_tools = dict(result.get("tools") or {})
            merged_tools.update(copy.deepcopy(user_tools))
            result["tools"] = merged_tools
    return result


def normalize_permissions_agent_id(agent_id: str | None) -> str | None:
    if agent_id is None:
        return None
    text = str(agent_id).strip()
    return text or None


def strip_permissions_agents(raw: dict[str, Any] | None) -> dict[str, Any]:
    """剥掉保留键 ``agents``，得到可进引擎的全局策略段。"""
    if not isinstance(raw, dict):
        return {}
    body = copy.deepcopy(raw)
    body.pop(PERMISSIONS_AGENTS_KEY, None)
    return body


def sanitize_agent_permissions_body(body: dict[str, Any] | None) -> dict[str, Any]:
    """Agent body 不得再嵌套 ``agents``；内层忽略且不按普通字段采用。"""
    if not isinstance(body, dict):
        return {}
    sanitized = copy.deepcopy(body)
    sanitized.pop(PERMISSIONS_AGENTS_KEY, None)
    return sanitized


def _is_seed_permissions_agent_id(agent_id: str | None) -> bool:
    normalized = normalize_permissions_agent_id(agent_id)
    return bool(normalized) and normalized in SEED_PERMISSIONS_AGENT_IDS


def _parse_agents_table(raw: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    table = raw.get(PERMISSIONS_AGENTS_KEY)
    if not isinstance(table, dict):
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for key, value in table.items():
        agent_id = normalize_permissions_agent_id(str(key) if key is not None else None)
        if not agent_id or not isinstance(value, dict):
            continue
        parsed[agent_id] = sanitize_agent_permissions_body(value)
    return parsed


def _ingest_permissions_raw(raw: dict[str, Any], source: str) -> dict[str, Any]:
    """拆桶写入进程缓存，返回剥掉 ``agents`` 后的全局段。"""
    global _cached_global, _cached_agents, _cache_source
    _cached_global = strip_permissions_agents(raw)
    if is_enterprise():
        _cached_agents = {}
    else:
        _cached_agents = _parse_agents_table(raw)
    _cache_source = source
    return copy.deepcopy(_cached_global)


def _load_yaml_cache(*, force_reload: bool = False) -> dict[str, Any]:
    """加载 yaml 缓存，返回剥掉 ``agents`` 后的全局段。

    副作用：标准版同步填充 ``_cached_agents``。调用方若需要 ``agents[id]``
    覆盖表，必须读 ``_cached_agents``，**禁止**对返回值做 ``.get("agents")``
    （返回值永远不含该键）。
    """
    global _cached_global
    if not force_reload and _cached_global is not None:
        return copy.deepcopy(_cached_global)
    raw = _load_permissions_from_yaml()
    return _ingest_permissions_raw(raw, "yaml")


def _ensure_yaml_permissions_cache(*, force_reload: bool = False) -> None:
    """确保 ``_cached_global`` / ``_cached_agents`` 已从 yaml 加载。"""
    _load_yaml_cache(force_reload=force_reload)


def get_global_permissions_config(*, force_reload: bool = False) -> dict[str, Any]:
    """标准版全局策略段（yaml 去掉 ``agents``）。不读 ``PERMISSIONS_AGENT_BASE``。

    该段为「包内模板 ∪ 用户 override」的生效全局，可被 UI 改写。
    Agent 稀疏覆盖的缺省底请用 ``get_shipped_template_permissions_config``。
    """
    return _load_yaml_cache(force_reload=force_reload)


def get_shipped_template_permissions_config(*, force_reload: bool = False) -> dict[str, Any]:
    """包内 ``resources/config.yaml`` 的 permissions 全局段（剥掉 ``agents``）。

    不受用户全局 permissions 改动影响；供 ``agents[id]`` 稀疏覆盖的缺省底。
    """
    global _cached_template_permissions
    if not force_reload and _cached_template_permissions is not None:
        return copy.deepcopy(_cached_template_permissions)
    from jiuwenswarm.common.utils import load_yaml_dict, resolve_shipped_template_config_path

    raw = load_yaml_dict(resolve_shipped_template_config_path()).get("permissions")
    _cached_template_permissions = strip_permissions_agents(
        raw if isinstance(raw, dict) else {}
    )
    return copy.deepcopy(_cached_template_permissions)


def merge_agent_permissions_overlay(
    base: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    """以 base（通常为包内模板）为底，agent 稀疏覆盖叠加上去。

    ``tools`` / ``file_guard`` 深合并，其余键覆盖。
    """
    merged = strip_permissions_agents(base if isinstance(base, dict) else {})
    if not isinstance(overlay, dict) or not overlay:
        return merged

    overlay_clean = sanitize_agent_permissions_body(overlay)

    overlay_tools = overlay_clean.get("tools")
    if isinstance(overlay_tools, dict):
        tools = merged.get("tools")
        if not isinstance(tools, dict):
            tools = {}
            merged["tools"] = tools
        tools.update(copy.deepcopy(overlay_tools))

    overlay_overrides = overlay_clean.get("approval_overrides")
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

    overlay_fg = overlay_clean.get("file_guard")
    if isinstance(overlay_fg, dict):
        fg = merged.get("file_guard")
        if not isinstance(fg, dict):
            fg = {}
            merged["file_guard"] = fg
        _deep_merge_file_guard(fg, overlay_fg)

    for key, value in overlay_clean.items():
        if key in ("tools", "approval_overrides", "file_guard"):
            continue
        merged[key] = copy.deepcopy(value)

    return merged


def get_yaml_agent_permissions_overlay(agent_id: str | None) -> dict[str, Any] | None:
    """标准版返回 yaml ``permissions.agents[id]`` 稀疏覆盖段；企业版 / 未命中为 ``None``。

    不与模板/全局合并；供落盘 mutate。内层再嵌套 ``agents`` 已剥离。

    实现注意：全局缓存 API（``_load_yaml_cache`` / ``get_global_permissions_config``）
    返回值已剥掉 ``agents``，不能对返回 dict 做 ``.get("agents")``。覆盖表只存在于
    ``_cached_agents``（由 ``_ingest_permissions_raw`` 在加载时填充）。
    """
    if is_enterprise():
        return None
    normalized = normalize_permissions_agent_id(agent_id)
    if not normalized:
        return None
    _ensure_yaml_permissions_cache()
    body = _cached_agents.get(normalized)
    if not isinstance(body, dict):
        return None
    return copy.deepcopy(body)


def resolve_yaml_agent_permissions_body(agent_id: str | None) -> dict[str, Any] | None:
    """标准版解析 ``permissions.agents[agent_id]``；企业版不解析 yaml ``agents``。

    - 命中稀疏覆盖，或种子 id（如 ``office-excel``）尚未建桶：返回
      「包内模板 ∪ 覆盖」生效 body（缺省不受用户全局改动影响）。
    - 其它 id 未命中：``None``（调用方回落用户全局）。
    """
    if is_enterprise():
        return None
    normalized = normalize_permissions_agent_id(agent_id)
    if not normalized:
        return None
    overlay = get_yaml_agent_permissions_overlay(normalized)
    if overlay is None and not _is_seed_permissions_agent_id(normalized):
        return None
    return merge_agent_permissions_overlay(
        get_shipped_template_permissions_config(),
        overlay or {},
    )


def resolve_permissions_persist_target(agent_id: str | None) -> str | None:
    """命中 yaml ``agents[id]`` 时返回该 id（写专属覆盖）；否则 ``None``（写全局）。

    企业版恒为 ``None``。种子 id（如 ``office-excel``）未命中仍返回该 id，persist 时
    按需建空覆盖桶并只写入本次 mutate 字段。其它 id 未命中不新建。
    """
    if is_enterprise():
        return None
    normalized = normalize_permissions_agent_id(agent_id)
    if not normalized:
        return None
    if get_yaml_agent_permissions_overlay(normalized) is not None:
        return normalized
    if _is_seed_permissions_agent_id(normalized):
        return normalized
    return None


def lookup_standard_permissions_agent_id(
    extracted_agent_id: str | None,
    env_agent_id: str | None,
) -> str | None:
    """标准版查找键：``extract_ids`` 优先；进池重映射后跟 Manager / ``_env_agent_id``。

    无明确 id 时返回 ``None``（回落全局），不合成 ``"default"``，以免误命中
    ``agents.default``。请求仍是 ``default`` 但池已选中其它 Manager 时，用 ``env_agent_id``。
    """
    extracted = normalize_permissions_agent_id(extracted_agent_id)
    env_id = normalize_permissions_agent_id(env_agent_id)
    extracted_is_unspecified = not extracted or extracted == "default"
    env_is_concrete = bool(env_id) and env_id != "default"
    if env_is_concrete and extracted_is_unspecified:
        return env_id
    return extracted or env_id


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

    若当前 Task 绑定了 Agent 级 body（``setup_permissions_agent_base``），
    优先返回该 body。否则回落 ``config.yaml`` 全局段（标准版剥掉 ``agents``；
    企业版不解析 yaml ``agents`` 分桶）。
    """
    agent_base = PERMISSIONS_AGENT_BASE.get()
    if isinstance(agent_base, dict):
        return sanitize_agent_permissions_body(agent_base)

    return get_global_permissions_config(force_reload=force_reload)


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
    old_effective = copy.deepcopy(_cached_global) if _cached_global is not None else None
    clear_permissions_config_cache()

    if not payload or payload.get("op") == "delete":
        effective = _load_yaml_cache(force_reload=True)
    elif isinstance(payload.get("body"), dict):
        effective = _ingest_permissions_raw(payload["body"], "memory")
    else:
        effective = _load_yaml_cache(force_reload=True)

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
    persist_target_agent_id: str | None = None,
) -> dict[str, Any]:
    """变更 permissions 并持久化。

    - 标准版：写 ``config.yaml``。``persist_target_agent_id`` 命中 yaml
      ``agents[id]`` 时只更新该稀疏覆盖段；种子 id 未命中则建空覆盖后只写入
      本次 mutate 字段（不整段克隆全局）。其它未命中写全局。全局 persist
      **不**自动 seed ``agents`` 桶。
    - 企业版 + ``persist_scope='session'``：仅更新指定会话的内存 overlay。
    - 企业版 + ``persist_scope='base'``：仅更新进程内存缓存（不再写 permissions_config 表；
      Agent 级策略请改 permissions_template）。不读写 yaml ``agents``。
    """
    global _cached_global, _cached_agents
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

    if is_enterprise():
        permissions = get_base_permissions_config()
        if not isinstance(permissions, dict):
            permissions = {}
        else:
            permissions = copy.deepcopy(permissions)

        old_permissions = copy.deepcopy(permissions)
        mutate_fn(permissions)
        _ingest_permissions_raw(permissions, "memory")
        logger.info(
            "[permissions_config] enterprise base persist kept in-memory only "
            "(source=%s); use permissions_template for Agent-level policy",
            source,
        )
        _sync_skill_grants(old_permissions, permissions)
        return permissions

    target_id = normalize_permissions_agent_id(persist_target_agent_id)
    agent_overlay = get_yaml_agent_permissions_overlay(target_id) if target_id else None
    if target_id and agent_overlay is not None:
        permissions = copy.deepcopy(agent_overlay)
        template_base = get_shipped_template_permissions_config()
        old_effective = merge_agent_permissions_overlay(template_base, permissions)
        mutate_fn(permissions)
        permissions = sanitize_agent_permissions_body(permissions)
        _persist_agent_permissions_to_yaml(target_id, permissions)
        _cached_agents[target_id] = copy.deepcopy(permissions)
        logger.info(
            "[permissions_config] standard agent persist agent_id=%s source=%s",
            target_id,
            source,
        )
        new_effective = merge_agent_permissions_overlay(template_base, permissions)
        _sync_skill_grants(old_effective, new_effective)
        return new_effective

    if target_id and _is_seed_permissions_agent_id(target_id):
        permissions: dict[str, Any] = {}
        template_base = get_shipped_template_permissions_config()
        old_effective = copy.deepcopy(template_base)
        mutate_fn(permissions)
        permissions = sanitize_agent_permissions_body(permissions)
        _persist_agent_permissions_to_yaml(target_id, permissions)
        _cached_agents[target_id] = copy.deepcopy(permissions)
        logger.info(
            "[permissions_config] standard agent persist sparse-create "
            "agent_id=%s source=%s keys=%s",
            target_id,
            source,
            sorted(permissions.keys()),
        )
        new_effective = merge_agent_permissions_overlay(template_base, permissions)
        _sync_skill_grants(old_effective, new_effective)
        return new_effective

    permissions = get_global_permissions_config()
    if not isinstance(permissions, dict):
        permissions = {}
    else:
        permissions = copy.deepcopy(permissions)

    old_permissions = copy.deepcopy(permissions)
    mutate_fn(permissions)
    permissions = strip_permissions_agents(permissions)
    _persist_global_permissions_to_yaml(permissions)
    _cached_global = copy.deepcopy(permissions)
    logger.info(
        "[permissions_config] standard global persist source=%s",
        source,
    )
    _sync_skill_grants(old_permissions, permissions)
    return permissions


def _sync_skill_grants(old_permissions: dict[str, Any], permissions: dict[str, Any]) -> None:
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


def _permissions_yaml_path():
    from jiuwenswarm.common.config import CONFIG_YAML_PATH

    return CONFIG_YAML_PATH


def _persist_global_permissions_to_yaml(permissions: dict[str, Any]) -> None:
    from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip

    global _cached_agents
    yaml_path = _permissions_yaml_path()
    data = load_yaml_round_trip(yaml_path)
    existing = data.get("permissions") if isinstance(data, dict) else None
    agents_table: dict[str, Any] | None = None
    if isinstance(existing, dict):
        raw_agents = existing.get(PERMISSIONS_AGENTS_KEY)
        if isinstance(raw_agents, dict):
            agents_table = copy.deepcopy(raw_agents)

    section = copy.deepcopy(permissions)
    section.pop(PERMISSIONS_AGENTS_KEY, None)
    if agents_table is not None:
        section[PERMISSIONS_AGENTS_KEY] = agents_table
        if not is_enterprise():
            for agent_id, body in agents_table.items():
                normalized = normalize_permissions_agent_id(
                    str(agent_id) if agent_id is not None else None
                )
                if normalized and isinstance(body, dict):
                    _cached_agents[normalized] = sanitize_agent_permissions_body(body)
    if not isinstance(data, dict):
        data = {}
    data["permissions"] = section
    dump_yaml_round_trip(yaml_path, data)


def _persist_agent_permissions_to_yaml(agent_id: str, body: dict[str, Any]) -> None:
    from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip

    yaml_path = _permissions_yaml_path()
    data = load_yaml_round_trip(yaml_path)
    if not isinstance(data, dict):
        data = {}
    existing = data.get("permissions")
    if not isinstance(existing, dict):
        existing = {}
        data["permissions"] = existing
    agents = existing.get(PERMISSIONS_AGENTS_KEY)
    if not isinstance(agents, dict):
        agents = {}
        existing[PERMISSIONS_AGENTS_KEY] = agents
    stored = sanitize_agent_permissions_body(body)
    agents[agent_id] = stored
    dump_yaml_round_trip(yaml_path, data)


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
