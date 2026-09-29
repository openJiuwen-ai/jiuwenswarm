"""Permissions 配置 RPC（宿主侧）。

这是原 `jiuwenswarm.agents.harness.common.rails.permissions.config_rpc` 的新归档位置，
用于减少对 legacy permissions 包路径的依赖。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)

_PERMISSIONS_CFG_METHODS: frozenset[ReqMethod] = frozenset(
    {
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
        ReqMethod.PERMISSIONS_NET_GUARD_GET,
        ReqMethod.PERMISSIONS_NET_GUARD_SET,
    }
)

_PERMISSIONS_READ_ONLY_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.PERMISSIONS_TOOLS_GET,
        ReqMethod.PERMISSIONS_RULES_GET,
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
        ReqMethod.PERMISSIONS_NET_GUARD_GET,
    }
)


def get_permissions_config_req_methods() -> frozenset[ReqMethod]:
    return _PERMISSIONS_CFG_METHODS


def get_permissions_read_only_req_methods() -> frozenset[ReqMethod]:
    return _PERMISSIONS_READ_ONLY_METHODS


def publish_host_exit_policy_from_config(config: dict[str, Any] | None = None) -> None:
    """把 ``permissions.net_guard`` 发布给宿主出口（P3）。

    P3 是进程级状态，只在配置加载（AgentServer 启动、配置热更新）和
    ``permissions.net_guard.set`` 写入后发布；``config`` 缺省时读当前 config.yaml。
    """
    from openjiuwen.harness.security.outbound import publish_host_exit_policy
    from openjiuwen.harness.security.permission_engine.core import prepare_permissions_for_engine

    from jiuwenswarm.common.config import get_config

    cfg = config if isinstance(config, dict) else (get_config() or {})
    perms = cfg.get("permissions") if isinstance(cfg.get("permissions"), dict) else {}
    publish_host_exit_policy(prepare_permissions_for_engine(perms))


def _net_guard_payload() -> dict[str, Any]:
    """URL / 域名规则分组：用户规则 + 只读内置底线 + P3 状态 + 豁免清单。"""
    from openjiuwen.harness.security.outbound import describe_host_exit
    from openjiuwen.harness.security.permission_engine.netguard.net_urls import load_package_net_urls

    from jiuwenswarm.common.config import get_config

    cfg = get_config() or {}
    perms = cfg.get("permissions") if isinstance(cfg.get("permissions"), dict) else {}
    raw = perms.get("net_guard") if isinstance(perms.get("net_guard"), dict) else {}
    section = {
        "enabled": bool(raw.get("enabled")),
        "defaults": str(raw.get("defaults") or "allow"),
        "urls": dict(raw.get("urls") or {}) if isinstance(raw.get("urls"), dict) else {},
        "enforce_host_exit": raw.get("enforce_host_exit", True) is not False,
    }
    warnings: list[str] = []
    if section["enabled"] and not section["enforce_host_exit"]:
        warnings.append("enforce_host_exit=false：宿主出站 HTTP 无强制")
    if not section["enabled"]:
        warnings.append("net_guard 未启用：工具参数护栏（P1）与宿主出口（P3）均不生效")
    return {
        "net_guard": section,
        "builtin_urls": load_package_net_urls(),
        "enforcement_points": ["P1 工具参数护栏", "P3 宿主出口"],
        "apply_mode": "hot_reload",
        "host_exit": describe_host_exit(),
        "warnings": warnings,
    }


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
    from jiuwenswarm.common.config import (
        create_permissions_rule_in_config,
        delete_permissions_approval_override_in_config,
        delete_permissions_rule_in_config,
        delete_permissions_tool_in_config,
        get_permissions_approval_overrides,
        get_permissions_rules,
        get_permissions_tools,
        replace_permissions_tools_in_config,
        update_permissions_rule_in_config,
        update_permissions_tool_in_config,
    )

    m = request.req_method
    params = request.params if isinstance(request.params, dict) else {}
    tag = m.value if m is not None else ""

    try:
        if m == ReqMethod.PERMISSIONS_TOOLS_GET:
            return _ok(request, dict(get_permissions_tools()))

        if m == ReqMethod.PERMISSIONS_TOOLS_SET:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            tools = params.get("tools")
            replace_permissions_tools_in_config(tools)
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
            return _ok(request, {"rule": stored})

        if m == ReqMethod.PERMISSIONS_RULES_UPDATE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            rid = params.get("id")
            patch = params.get("patch")
            if not isinstance(patch, dict):
                return _err(request, "patch must be object")
            merged = update_permissions_rule_in_config(str(rid or ""), patch)
            return _ok(request, {"rule": merged})

        if m == ReqMethod.PERMISSIONS_RULES_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            ok_del = delete_permissions_rule_in_config(str(params.get("id") or ""))
            if not ok_del:
                return _err(request, "rule not found", code="NOT_FOUND")
            return _ok(request, {"ok": True})

        if m == ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET:
            return _ok(request, dict(get_permissions_approval_overrides()))

        if m == ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_DELETE:
            if not isinstance(params, dict):
                return _err(request, "params must be object")
            ok_del = delete_permissions_approval_override_in_config(str(params.get("id") or ""))
            if not ok_del:
                return _err(request, "approval_override not found", code="NOT_FOUND")
            return _ok(request, {"ok": True})

        if m == ReqMethod.PERMISSIONS_NET_GUARD_GET:
            return _ok(request, _net_guard_payload())

        if m == ReqMethod.PERMISSIONS_NET_GUARD_SET:
            from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
                persist_net_guard_section,
            )

            patch = params.get("net_guard") if isinstance(params.get("net_guard"), dict) else params
            persist_net_guard_section(patch)
            publish_host_exit_policy_from_config()
            return _ok(request, _net_guard_payload())

    except ValueError as e:
        return _err(request, str(e))
    except Exception as e:
        logger.exception("[%s] %s", tag, e)
        return _err(request, str(e), code="INTERNAL_ERROR")

    return _err(request, "unknown permissions req_method", code="BAD_REQUEST")


__all__ = [
    "dispatch_permissions_config_request",
    "get_permissions_config_req_methods",
    "get_permissions_read_only_req_methods",
    "publish_host_exit_policy_from_config",
]

