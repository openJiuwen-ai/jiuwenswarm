# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Permissions 配置：E2A / AgentRequest 入口，返回 AgentResponse."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from jiuwenclaw.schema.agent import AgentRequest, AgentResponse
from jiuwenclaw.schema.message import ReqMethod

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_PERMISSIONS_CFG_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.PERMISSIONS_ENABLED_GET,
        ReqMethod.PERMISSIONS_ENABLED_SET,
        ReqMethod.PERMISSIONS_TOOLS_GET,
        ReqMethod.PERMISSIONS_TOOLS_SET,
        ReqMethod.PERMISSIONS_TOOLS_UPDATE,
        ReqMethod.PERMISSIONS_TOOLS_DELETE,
        ReqMethod.PERMISSIONS_RULES_GET,
        ReqMethod.PERMISSIONS_RULES_CREATE,
        ReqMethod.PERMISSIONS_RULES_UPDATE,
        ReqMethod.PERMISSIONS_RULES_DELETE,
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE,
        ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_GET,
        ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_SET,
    }
)


def get_permissions_config_req_methods() -> frozenset[ReqMethod]:
    return _PERMISSIONS_CFG_METHODS


def _normalize_permissions_config_params(params: dict[str, Any]) -> dict[str, Any]:
    """Accept nested relay-claw style payloads and map them to permissions RPC params."""
    normalized = dict(params)
    permissions = params.get("permissions")
    if not isinstance(permissions, dict):
        return normalized

    if "enabled" not in normalized and "enabled" in permissions:
        normalized["enabled"] = permissions.get("enabled")
    if "tools" not in normalized and "tools" in permissions:
        normalized["tools"] = permissions.get("tools")

    fg = permissions.get("file_guard")
    if isinstance(fg, dict):
        ws = fg.get("workspace")
        if isinstance(ws, dict) and "rw_enabled" not in normalized and "rw_enabled" in ws:
            normalized["rw_enabled"] = ws.get("rw_enabled")

    return normalized


def _hot_reload_permission_engine_from_config() -> None:
    from jiuwenclaw.config import get_config
    from jiuwenclaw.agentserver.permissions.core import get_permission_engine

    perm_cfg = get_config().get("permissions", {})
    get_permission_engine().update_config(perm_cfg)


def _err(request: AgentRequest, message: str, *, code: str = "BAD_REQUEST") -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=False,
        payload={"error": message, "code": code},
        metadata=request.metadata,
    )


def _ok(request: AgentRequest, payload: dict[str, Any] | None) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload or {},
        metadata=request.metadata,
    )


def dispatch_permissions_config_request(request: AgentRequest) -> AgentResponse:
    """执行一条 permissions 配置 RPC（与原先 WebSocket register_method 语义一致）。"""
    from jiuwenclaw.config import (
        get_config,
        get_permissions_approval_overrides,
        get_permissions_rules,
        get_permissions_tools,
        create_permissions_rule_in_config,
        delete_permissions_approval_override_in_config,
        delete_permissions_rule_in_config,
        delete_permissions_tool_in_config,
        replace_permissions_tools_in_config,
        update_permissions_enabled_in_config,
        update_permissions_file_guard_workspace_rw_enabled_in_config,
        get_permissions_file_guard_workspace_rw_enabled,
        update_permissions_rule_in_config,
        update_permissions_tool_in_config,
    )

    m = request.req_method
    params = request.params if isinstance(request.params, dict) else {}
    params = _normalize_permissions_config_params(params)
    tag = m.value if m is not None else ""

    try:
        if m == ReqMethod.PERMISSIONS_ENABLED_GET:
            enabled = bool((get_config().get("permissions") or {}).get("enabled", True))
            return _ok(request, {"enabled": enabled})

        if m == ReqMethod.PERMISSIONS_ENABLED_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            value = params.get("enabled")
            if not isinstance(value, bool):
                return _err(request, "enabled must be boolean")
            update_permissions_enabled_in_config(value)
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"enabled": value})

        if m == ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_GET:
            rw_enabled = get_permissions_file_guard_workspace_rw_enabled()
            return _ok(request, {"rw_enabled": rw_enabled})

        if m == ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            value = params.get("rw_enabled")
            if not isinstance(value, bool):
                return _err(request, "rw_enabled must be boolean")
            update_permissions_file_guard_workspace_rw_enabled_in_config(value)
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"rw_enabled": value})

        if m == ReqMethod.PERMISSIONS_TOOLS_GET:
            return _ok(request, dict(get_permissions_tools()))

        if m == ReqMethod.PERMISSIONS_TOOLS_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tools = params.get("tools")
            replace_permissions_tools_in_config(tools)
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"ok": True})

        if m == ReqMethod.PERMISSIONS_TOOLS_UPDATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tool = str(params.get("tool") or params.get("name") or "").strip()
            if not tool:
                return _err(request, "tool is required")
            if "level" not in params:
                return _err(request, "level is required")
            payload = update_permissions_tool_in_config(tool, params.get("level"))
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, dict(payload))

        if m == ReqMethod.PERMISSIONS_TOOLS_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tool = str(params.get("tool") or params.get("name") or "").strip()
            if not tool:
                return _err(request, "tool is required")
            ok_del = delete_permissions_tool_in_config(tool)
            if not ok_del:
                return _err(request, "tool not found in permissions.tools", code="NOT_FOUND")
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, dict(get_permissions_tools()))

        if m == ReqMethod.PERMISSIONS_RULES_GET:
            return _ok(request, dict(get_permissions_rules()))

        if m == ReqMethod.PERMISSIONS_RULES_CREATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            rule = params.get("rule")
            if not isinstance(rule, dict):
                return _err(request, "rule must be object")
            stored = create_permissions_rule_in_config(rule)
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"rule": stored})

        if m == ReqMethod.PERMISSIONS_RULES_UPDATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            rid = params.get("id")
            patch = params.get("patch")
            if not isinstance(patch, dict):
                return _err(request, "patch must be object")
            merged = update_permissions_rule_in_config(str(rid or ""), patch)
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"rule": merged})

        if m == ReqMethod.PERMISSIONS_RULES_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            ok_del = delete_permissions_rule_in_config(str(params.get("id") or ""))
            if not ok_del:
                return _err(request, "rule not found", code="NOT_FOUND")
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"ok": True})

        if m == ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET:
            return _ok(request, dict(get_permissions_approval_overrides()))

        if m == ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            ok_del = delete_permissions_approval_override_in_config(str(params.get("id") or ""))
            if not ok_del:
                return _err(request, "approval_override not found", code="NOT_FOUND")
            try:
                _hot_reload_permission_engine_from_config()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"ok": True})

    except ValueError as e:
        return _err(request, str(e))
    except Exception as e:
        logger.exception("[%s] %s", tag, e)
        return _err(request, str(e), code="INTERNAL_ERROR")

    return _err(request, "unknown permissions req_method", code="BAD_REQUEST")


__all__ = [
    "dispatch_permissions_config_request",
    "get_permissions_config_req_methods",
]
