"""Permissions 配置 RPC（宿主侧）。

这是原 `jiuwenswarm.agents.harness.common.rails.permissions.config_rpc` 的新归档位置，
用于减少对 legacy permissions 包路径的依赖。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.config.permissions.access import (
    create_permissions_rule_in_config,
    delete_permissions_approval_override_in_config,
    delete_permissions_rule_in_config,
    delete_permissions_tool_in_config,
    get_permissions_body_in_config,
    replace_permissions_tools_in_config,
    update_permissions_enabled_in_config,
    update_permissions_file_guard_workspace_rw_enabled_in_config,
    update_permissions_rule_in_config,
    update_permissions_tool_in_config,
)
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

logger = logging.getLogger(__name__)

_PERMISSIONS_CFG_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.PERMISSIONS_ENABLED_GET,
        ReqMethod.PERMISSIONS_ENABLED_SET,
        ReqMethod.PERMISSIONS_TOOLS_GET,
        ReqMethod.PERMISSIONS_TOOLS_LIST,
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
        ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET,
        ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET,
    }
)


def get_permissions_config_req_methods() -> frozenset[ReqMethod]:
    return _PERMISSIONS_CFG_METHODS


def _normalize_permissions_config_params(params: dict[str, Any]) -> dict[str, Any]:
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


def _hot_reload_permissions_config_cache() -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        clear_permissions_config_cache,
    )

    clear_permissions_config_cache()


def _rpc_agent_id(request: AgentRequest) -> str | None:
    raw = getattr(request, "agent_id", None)
    if raw is not None and str(raw).strip():
        try:
            from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

            return TenantAgentPool.extract_ids(request)[0]
        except Exception:  # noqa: BLE001
            return str(raw).strip()
    channel = str(getattr(request, "channel_id", "") or "").strip()
    if channel == "acp":
        return "acp"
    return None


def _rpc_persist_target(request: AgentRequest) -> str | None:
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        resolve_permissions_persist_target,
    )

    return resolve_permissions_persist_target(_rpc_agent_id(request))


def _validate_tools_payload(tools: Any) -> dict[str, str]:
    from jiuwenswarm.common.config import _validate_tools_map

    return _validate_tools_map(tools)


def _normalize_rule_for_create(rule: dict[str, Any]) -> dict[str, Any]:
    from jiuwenswarm.common.config import (
        _RULE_MUTABLE_KEYS,
        _normalize_rule_severity_action,
        _normalize_rule_tools,
    )

    rid = str(rule.get("id") or "").strip() or f"ui_rule_{uuid.uuid4().hex[:12]}"
    stored: dict[str, Any] = {"id": rid}
    for key in _RULE_MUTABLE_KEYS:
        if key in rule and rule[key] is not None:
            stored[key] = rule[key]
    if "tools" not in stored or "pattern" not in stored:
        raise ValueError("tools and pattern are required")
    stored["tools"] = _normalize_rule_tools(stored["tools"])
    stored["pattern"] = str(stored["pattern"]).strip()
    if not stored["tools"]:
        raise ValueError("tools must be a non-empty list")
    if not stored["pattern"]:
        raise ValueError("pattern must be non-empty")
    _normalize_rule_severity_action(stored)
    return stored


def _apply_rule_patch(existing: dict[str, Any], rule_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    from jiuwenswarm.common.config import (
        _RULE_MUTABLE_KEYS,
        _normalize_rule_severity_action,
        _normalize_rule_tools,
    )

    rid = str(rule_id or "").strip()
    if not rid:
        raise ValueError("id is required")
    merged = dict(existing)
    for key, value in patch.items():
        if key == "id" or key not in _RULE_MUTABLE_KEYS:
            continue
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    merged["id"] = rid
    if "tools" in merged:
        merged["tools"] = _normalize_rule_tools(merged["tools"])
    if "pattern" in merged:
        merged["pattern"] = str(merged["pattern"]).strip()
    if not merged.get("tools"):
        raise ValueError("tools must be a non-empty list")
    if not merged.get("pattern"):
        raise ValueError("pattern must be non-empty")
    _normalize_rule_severity_action(merged)
    return merged


def _permissions_body(request: AgentRequest | None = None) -> dict[str, Any]:
    if request is not None:
        target = _rpc_persist_target(request)
        if target:
            from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                resolve_yaml_agent_permissions_body,
            )

            body = resolve_yaml_agent_permissions_body(target)
            return dict(body) if isinstance(body, dict) else {}
    body = run_awaitable(get_permissions_body_in_config())
    return dict(body) if isinstance(body, dict) else {}


def _write_agent_permissions(request: AgentRequest, mutate_fn: Callable[[dict[str, Any]], None]) -> bool:
    """命中 yaml ``agents[id]`` 时写入专属 body。返回是否已处理。"""
    target = _rpc_persist_target(request)
    if not target:
        return False
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        persist_permissions_mutate,
    )

    persist_permissions_mutate(
        mutate_fn,
        persist_scope="base",
        persist_target_agent_id=target,
        source="permissions_config_rpc",
    )
    return True


def _permissions_tools_view(request: AgentRequest | None = None) -> dict[str, Any]:
    tools = _permissions_body(request).get("tools")
    if not isinstance(tools, dict):
        return {"tools": {}}
    return {"tools": dict(tools)}


def _permissions_rules_view(request: AgentRequest | None = None) -> dict[str, Any]:
    rules = _permissions_body(request).get("rules")
    if not isinstance(rules, list):
        return {"rules": []}
    return {"rules": [r for r in rules if isinstance(r, dict)]}


def _permissions_approval_overrides_view(request: AgentRequest | None = None) -> dict[str, Any]:
    raw = _permissions_body(request).get("approval_overrides")
    if not isinstance(raw, list):
        return {"approval_overrides": []}
    return {"approval_overrides": [x for x in raw if isinstance(x, dict)]}


_WORKSPACE_ACCESS_AXES: tuple[str, ...] = ("read", "write", "exec")
_WORKSPACE_ACCESS_LEVELS: frozenset[str] = frozenset({"allow", "ask", "deny"})


def _permissions_file_guard_workspace_rw_enabled(request: AgentRequest | None = None) -> bool:
    fg = _permissions_body(request).get("file_guard")
    if not isinstance(fg, dict):
        return True
    ws = fg.get("workspace")
    if not isinstance(ws, dict):
        return True
    return bool(ws.get("rw_enabled", True))


def _workspace_access_view(request: AgentRequest | None = None) -> dict[str, str]:
    fg = _permissions_body(request).get("file_guard")
    if not isinstance(fg, dict):
        return {axis: "ask" for axis in _WORKSPACE_ACCESS_AXES}
    ws = fg.get("workspace")
    if not isinstance(ws, dict):
        return {axis: "ask" for axis in _WORKSPACE_ACCESS_AXES}
    result: dict[str, str] = {}
    for axis in _WORKSPACE_ACCESS_AXES:
        raw = ws.get(axis, "ask")
        if not isinstance(raw, str) or raw not in _WORKSPACE_ACCESS_LEVELS:
            result[axis] = "ask"
        else:
            result[axis] = raw
    return result


def _normalize_workspace_access_patch(axis: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for axis_name in _WORKSPACE_ACCESS_AXES:
        if axis_name not in axis:
            continue
        val = axis[axis_name]
        if val not in _WORKSPACE_ACCESS_LEVELS:
            raise ValueError(
                f"workspace.{axis_name} must be one of "
                f"{sorted(_WORKSPACE_ACCESS_LEVELS)}, got {val!r}"
            )
        normalized[axis_name] = str(val)
    return normalized


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


def dispatch_permissions_config_request(
    request: AgentRequest,
    *,
    get_runtime_tools_catalog: Callable[[], dict[str, dict[str, str]]] | None = None,
) -> AgentResponse:
    """执行一条 permissions 配置 RPC（与原先 WebSocket register_method 语义一致）。"""
    from jiuwenswarm.common.config import (
        build_permissions_tools_list_view,
        get_permissions_file_guard_workspace_access,
        update_permissions_file_guard_workspace_access_in_config,
    )

    m = request.req_method
    params = request.params if isinstance(request.params, dict) else {}
    params = _normalize_permissions_config_params(params)
    tag = m.value if m is not None else ""

    try:
        if m == ReqMethod.PERMISSIONS_ENABLED_GET:
            enabled = bool(_permissions_body(request).get("enabled", True))
            return _ok(request, {"enabled": enabled})

        if m == ReqMethod.PERMISSIONS_ENABLED_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            value = params.get("enabled")
            if not isinstance(value, bool):
                return _err(request, "enabled must be boolean")
            if not _write_agent_permissions(request, lambda perms: perms.__setitem__("enabled", value)):
                run_awaitable(update_permissions_enabled_in_config(value))
            try:
                _hot_reload_permissions_config_cache()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"enabled": value})

        if m == ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_GET:
            rw_enabled = _permissions_file_guard_workspace_rw_enabled(request)
            return _ok(request, {"rw_enabled": rw_enabled})

        if m == ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            value = params.get("rw_enabled")
            if not isinstance(value, bool):
                return _err(request, "rw_enabled must be boolean")

            def _mutate_rw(perms: dict[str, Any]) -> None:
                fg = perms.get("file_guard")
                if not isinstance(fg, dict):
                    fg = {}
                    perms["file_guard"] = fg
                ws = fg.get("workspace")
                if not isinstance(ws, dict):
                    ws = {}
                    fg["workspace"] = ws
                ws["rw_enabled"] = bool(value)

            if not _write_agent_permissions(request, _mutate_rw):
                run_awaitable(
                    update_permissions_file_guard_workspace_rw_enabled_in_config(value)
                )
            try:
                _hot_reload_permissions_config_cache()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, {"rw_enabled": value})

        if m == ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET:
            if _rpc_persist_target(request):
                return _ok(request, _workspace_access_view(request))
            return _ok(request, get_permissions_file_guard_workspace_access())

        if m == ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            axis = params.get("access")
            if not isinstance(axis, dict):
                return _err(request, "access must be object with read/write/exec")
            try:
                normalized = _normalize_workspace_access_patch(axis)
            except ValueError as e:
                return _err(request, str(e))

            def _mutate_access(perms: dict[str, Any]) -> None:
                fg = perms.get("file_guard")
                if not isinstance(fg, dict):
                    fg = {}
                    perms["file_guard"] = fg
                ws = fg.get("workspace")
                if not isinstance(ws, dict):
                    ws = {}
                    fg["workspace"] = ws
                for axis_name, val in normalized.items():
                    ws[axis_name] = val

            if _write_agent_permissions(request, _mutate_access):
                updated = _workspace_access_view(request)
            else:
                try:
                    updated = update_permissions_file_guard_workspace_access_in_config(axis)
                except ValueError as e:
                    return _err(request, str(e))
            try:
                _hot_reload_permissions_config_cache()
            except Exception as e:
                logger.warning("[%s] Failed to hot reload permission engine: %s", tag, e)
            return _ok(request, updated)

        if m == ReqMethod.PERMISSIONS_TOOLS_GET:
            return _ok(request, dict(_permissions_tools_view(request)))

        if m == ReqMethod.PERMISSIONS_TOOLS_LIST:
            catalog = (get_runtime_tools_catalog or (lambda: {}))()
            return _ok(
                request,
                build_permissions_tools_list_view(
                    catalog if isinstance(catalog, dict) else None,
                    permissions_body=_permissions_body(request),
                ),
            )

        if m == ReqMethod.PERMISSIONS_TOOLS_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tools = _validate_tools_payload(params.get("tools"))
            if not _write_agent_permissions(request, lambda perms: perms.__setitem__("tools", tools)):
                run_awaitable(replace_permissions_tools_in_config(tools))
            _hot_reload_permissions_config_cache()
            return _ok(request, {"ok": True})

        if m == ReqMethod.PERMISSIONS_TOOLS_UPDATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tool = str(params.get("tool") or params.get("name") or "").strip()
            if not tool:
                return _err(request, "tool is required")
            if "level" not in params:
                return _err(request, "level is required")
            level_map = _validate_tools_payload({tool: params.get("level")})
            level = level_map[tool]

            def _mutate_tool(perms: dict[str, Any]) -> None:
                existing = perms.get("tools")
                if not isinstance(existing, dict):
                    existing = {}
                    perms["tools"] = existing
                existing[tool] = level

            if _write_agent_permissions(request, _mutate_tool):
                payload = {"tools": dict(_permissions_tools_view(request).get("tools") or {})}
            else:
                payload = run_awaitable(
                    update_permissions_tool_in_config(tool, level)
                )
            _hot_reload_permissions_config_cache()
            return _ok(request, dict(payload))

        if m == ReqMethod.PERMISSIONS_TOOLS_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tool = str(params.get("tool") or params.get("name") or "").strip()
            if not tool:
                return _err(request, "tool is required")
            deleted = {"value": False}

            def _mutate_del_tool(perms: dict[str, Any]) -> None:
                tools_map = perms.get("tools")
                if not isinstance(tools_map, dict) or tool not in tools_map:
                    return
                perms["tools"] = {
                    key: value for key, value in tools_map.items() if key != tool
                }
                deleted["value"] = True

            if _write_agent_permissions(request, _mutate_del_tool):
                ok_del = deleted["value"]
            else:
                ok_del = run_awaitable(delete_permissions_tool_in_config(tool))
            if not ok_del:
                return _err(request, "tool not found in permissions.tools", code="NOT_FOUND")
            _hot_reload_permissions_config_cache()
            return _ok(request, dict(_permissions_tools_view(request)))

        if m == ReqMethod.PERMISSIONS_RULES_GET:
            return _ok(request, dict(_permissions_rules_view(request)))

        if m == ReqMethod.PERMISSIONS_RULES_CREATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            rule = params.get("rule")
            if not isinstance(rule, dict):
                return _err(request, "rule must be object")
            stored = _normalize_rule_for_create(rule)
            if _rpc_persist_target(request):
                def _mutate_rule(perms: dict[str, Any]) -> None:
                    rules_list = perms.get("rules")
                    if not isinstance(rules_list, list):
                        rules_list = []
                        perms["rules"] = rules_list
                    if any(
                        isinstance(item, dict)
                        and str(item.get("id") or "").strip() == stored["id"]
                        for item in rules_list
                    ):
                        raise ValueError(f"rule id already exists: {stored['id']}")
                    rules_list.append(dict(stored))

                _write_agent_permissions(request, _mutate_rule)
            else:
                stored = run_awaitable(create_permissions_rule_in_config(rule))
            _hot_reload_permissions_config_cache()
            return _ok(request, {"rule": stored})

        if m == ReqMethod.PERMISSIONS_RULES_UPDATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            rid = params.get("id")
            patch = params.get("patch")
            if not isinstance(patch, dict):
                return _err(request, "patch must be object")
            merged_holder: dict[str, Any] = {}

            def _mutate_rule_update(perms: dict[str, Any]) -> None:
                rules_list = perms.get("rules")
                if not isinstance(rules_list, list):
                    raise ValueError(f"rule not found: {rid}")
                idx = None
                for i, item in enumerate(rules_list):
                    if isinstance(item, dict) and str(item.get("id") or "").strip() == str(rid or "").strip():
                        idx = i
                        break
                if idx is None:
                    raise ValueError(f"rule not found: {rid}")
                merged = _apply_rule_patch(rules_list[idx], str(rid or ""), patch)
                rules_list[idx] = merged
                merged_holder.clear()
                merged_holder.update(merged)

            if _write_agent_permissions(request, _mutate_rule_update):
                merged = dict(merged_holder)
            else:
                merged = run_awaitable(update_permissions_rule_in_config(str(rid or ""), patch))
            _hot_reload_permissions_config_cache()
            return _ok(request, {"rule": merged})

        if m == ReqMethod.PERMISSIONS_RULES_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            rid = str(params.get("id") or "")
            deleted = {"value": False}

            def _mutate_rule_del(perms: dict[str, Any]) -> None:
                rules_list = perms.get("rules")
                if not isinstance(rules_list, list):
                    return
                new_rules = [
                    item
                    for item in rules_list
                    if not (isinstance(item, dict) and str(item.get("id") or "").strip() == rid.strip())
                ]
                if len(new_rules) == len(rules_list):
                    return
                perms["rules"] = new_rules
                deleted["value"] = True

            if _write_agent_permissions(request, _mutate_rule_del):
                ok_del = deleted["value"]
            else:
                ok_del = run_awaitable(delete_permissions_rule_in_config(rid))
            if not ok_del:
                return _err(request, "rule not found", code="NOT_FOUND")
            _hot_reload_permissions_config_cache()
            return _ok(request, {"ok": True})

        if m == ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET:
            return _ok(request, dict(_permissions_approval_overrides_view(request)))

        if m == ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            oid = str(params.get("id") or "")
            deleted = {"value": False}

            def _mutate_override_del(perms: dict[str, Any]) -> None:
                overrides = perms.get("approval_overrides")
                if not isinstance(overrides, list):
                    return
                new_list = [
                    item
                    for item in overrides
                    if not (isinstance(item, dict) and str(item.get("id") or "").strip() == oid.strip())
                ]
                if len(new_list) == len(overrides):
                    return
                perms["approval_overrides"] = new_list
                deleted["value"] = True

            if _write_agent_permissions(request, _mutate_override_del):
                ok_del = deleted["value"]
            else:
                ok_del = run_awaitable(
                    delete_permissions_approval_override_in_config(oid)
                )
            if not ok_del:
                return _err(request, "approval_override not found", code="NOT_FOUND")
            _hot_reload_permissions_config_cache()
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
