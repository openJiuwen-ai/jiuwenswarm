# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""E2A ``method`` → AgentServer REST（对照 ``docs/zh/Gateway-AgentServer-HTTP-REST组装.md``）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urljoin

from openjiuwen_protocol.e2a.models import E2AEnvelope
from openjiuwen_protocol.e2a.runtime_http_routes import (
    RUNTIME_HTTP_API_PREFIX as API_PREFIX,
    RUNTIME_HTTP_CHAT_ROUTES,
    RUNTIME_HTTP_ROUTES,
    resolve_runtime_http_route,
)
DEFAULT_HTTP_PORT = 8766

_PATH_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Agent ``session.create`` 拒绝 params.session_id（恢复会话走 session.switch）。
# 与北向 ``bind_http_session`` / Web outbound 同约定：信封顶层 session_id 可进
# ``X-Session-Id``，但不得写入 REST body（路径也无 {session_id}）。
_METHODS_WITHOUT_PARAM_SESSION_ID = frozenset({"session.create"})

_ENTERPRISE_TEAM_CONTROL_METHODS = frozenset({
    "team.create",
    "team.get",
    "team.stop",
    "team.member.join",
    "team.member.leave",
    "team.members.list",
    "team.quota.get",
})


def _visible_route_rows() -> tuple[tuple[str, str, str], ...]:
    from jiuwenswarm.enterprise.edition import is_enterprise

    if is_enterprise():
        return RUNTIME_HTTP_ROUTES
    return tuple(row for row in RUNTIME_HTTP_ROUTES if row[0] not in _ENTERPRISE_TEAM_CONTROL_METHODS)


REST_ROUTES: dict[str, tuple[str, str]] = {
    method: (verb, path) for method, verb, path in _visible_route_rows()
}

REST_ROUTES["video.conversation.append"] = ("POST", "/sessions/{session_id}/video/conversation/events")


class RestAssemblyError(ValueError):
    """REST 路径无法从信封拼出（缺 method / 缺路径占位符）。"""


class HttpMethodNotHandled(RestAssemblyError):
    """这个方法没有专用 HTTP 网址，Gateway 不再改走 /rpc。"""


@dataclass(frozen=True)
class AssembledRestRequest:
    verb: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None
    query: dict[str, str] | None
    used_rpc_fallback: bool


def normalize_agent_http_base(uri: str) -> str:
    """``http://host:8766`` 或已带 ``/api/v1`` → API 根（无末尾 /）。"""
    raw = (uri or "").strip().rstrip("/")
    if not raw:
        raise RestAssemblyError("AgentServer HTTP URL 为空")
    if raw.endswith(API_PREFIX):
        return raw
    return raw + API_PREFIX


def identity_headers(envelope: E2AEnvelope, *, accept: str) -> dict[str, str]:
    """组装 REST 身份头。

    REST body 只含业务 ``params``，不含整封 E2A；因此顶层 ``user_id`` 与
    ``channel_context.routing``（``group_id`` / ``bot_id`` / ``gateway_id``）
    必须经 ``X-*`` 头传到 Agent，由 Agent HTTP 入口重建。

    企业租户顶层字段同理：``service_id`` / ``agent_id`` / ``workspace_key``
    （Gateway ``apply_invoke_ids_to_envelope`` 写入）经
    ``X-Service-Id`` / ``X-Agent-Id`` / ``X-Workspace-Key`` 透传。

    ``channel_context.ext`` 是白名单过滤后的请求级扩展字段，经内部
    ``X-Jiuwenswarm-Request-Ext`` 传输；该头不承担鉴权。

    定时任务的推送真渠道 ``channel_context.targets``（= job.targets）经
    ``X-Cron-Targets`` 透传；执行侧 ``X-Channel-Id`` 仍可能是 ``__cron__``。
    Agent 重建为 ``metadata["targets"]``，供 send_file 等按真渠道开关。

    企业版另附 ``X-Jiuwenswarm-Expert-Packages``。值按这只 bot 的专家包模板展开，
    不用 ``channel_context`` 里客户端自带的字段。

    ``gateway_id`` 仅透传保留；Agent 业务（如企业配置 ``RoutingContext``）当前不消费。
    """
    from jiuwenswarm.common.request_ext import (
        INTERNAL_HEADER_NAME,
        METADATA_KEY,
        encode_internal_header,
    )
    from jiuwenswarm.common.request_identity import web_routing_identity

    headers = {
        "X-Request-Id": str(envelope.request_id or ""),
        "X-Channel-Id": str(envelope.channel or "web"),
        "Accept": accept,
    }
    session_id = envelope.session_id or (envelope.params or {}).get("session_id")
    if session_id:
        headers["X-Session-Id"] = str(session_id)
    identity = web_routing_identity(
        envelope.channel_context if isinstance(envelope.channel_context, dict) else None
    )
    user_id = envelope.user_id or identity.get("user_id")
    if user_id:
        headers["X-User-Id"] = str(user_id)
    for field, header_name in (
        ("group_id", "X-Group-Id"),
        ("bot_id", "X-Bot-Id"),
        ("gateway_id", "X-Gateway-Id"),
    ):
        value = identity.get(field)
        if value:
            headers[header_name] = value
    for attr, header_name in (
        ("service_id", "X-Service-Id"),
        ("agent_id", "X-Agent-Id"),
        ("workspace_key", "X-Workspace-Key"),
    ):
        value = getattr(envelope, attr, None)
        text = str(value).strip() if value is not None else ""
        if text:
            headers[header_name] = text
    channel_context = (
        envelope.channel_context if isinstance(envelope.channel_context, dict) else {}
    )
    cron_targets = str(channel_context.get("targets") or "").strip()
    if cron_targets:
        headers["X-Cron-Targets"] = cron_targets
    encoded_ext = encode_internal_header(channel_context.get(METADATA_KEY))
    if encoded_ext:
        headers[INTERNAL_HEADER_NAME] = encoded_ext
    _attach_expert_packages_header(headers)
    return headers


def _attach_expert_packages_header(headers: dict[str, str]) -> None:
    """企业版写入刚绑定的名单。未绑定时按空名单发送。"""
    from agent_gateway.enterprise.edition import is_enterprise

    if not is_enterprise():
        return
    from agent_gateway.enterprise.capabilities.experts.allowlist import (
        attach_expert_packages_header,
    )

    attach_expert_packages_header(headers)


def method_has_http_handler(method: str) -> bool:
    """Agent 的 HTTP 处理表或聊天适配里有这个方法时，才使用专用网址。"""
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.dispatch import HANDLERS
    from jiuwenswarm.server.http_chat import is_http_chat_method

    try:
        req_method = ReqMethod(method)
    except ValueError:
        return False
    return is_http_chat_method(req_method) or req_method in HANDLERS


# Gateway lifecycle_handlers 轮询用：全量 events / inventory，无 project_id。
_PROJECT_LIFECYCLE_FEED_ROUTE: tuple[str, str] = ("POST", "/projects/actions/lifecycle")


def declared_http_routes() -> tuple[tuple[str, str, str], ...]:
    """返回实际要注册的 ``(方法名, 动词, 路径)``。没有处理函数的不出现。"""
    chosen: dict[tuple[str, str], str] = {}
    # 固定 /projects/actions/... 必须先于 /projects/{project_id}/... 注册，
    # 否则 Starlette 会把 ``actions`` 当成 project_id。
    if method_has_http_handler("project.lifecycle"):
        chosen[_PROJECT_LIFECYCLE_FEED_ROUTE] = "project.lifecycle"
    for method, (verb, path) in REST_ROUTES.items():
        if not method_has_http_handler(method):
            continue
        key = (verb, path)
        if key not in chosen:
            chosen[key] = method
    return tuple((method, verb, path) for (verb, path), method in chosen.items())


def _lookup_route(
    method: str,
    *,
    is_stream: bool,
    params: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    if method == "project.lifecycle" and method_has_http_handler(method):
        feed = params or {}
        if feed.get("events") or feed.get("inventory"):
            return _PROJECT_LIFECYCLE_FEED_ROUTE
    route = resolve_runtime_http_route(method, is_stream=is_stream)
    has_rest_handler = method in REST_ROUTES and method_has_http_handler(method)
    if route is not None:
        if route in RUNTIME_HTTP_CHAT_ROUTES or has_rest_handler:
            return route.verb, route.path
    elif has_rest_handler:
        return REST_ROUTES[method]
    extra = _enterprise_pod_route(method)
    if extra is not None:
        return extra
    raise HttpMethodNotHandled(f"这个方法还没有 HTTP 处理: {method}")


def _enterprise_pod_route(method: str) -> tuple[str, str] | None:
    """企业版从 Gateway 企业平面取专家包专用网址。个人版不加载该模块。"""
    from agent_gateway.enterprise.edition import is_enterprise

    if not is_enterprise():
        return None
    from agent_gateway.enterprise.capabilities.experts.http_routes import pod_http_routes

    return pod_http_routes().get(method)


def _fill_path(template: str, values: Mapping[str, Any]) -> tuple[str, set[str]]:
    used: set[str] = set()
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None or values[key] == "":
            missing.append(key)
            return match.group(0)
        used.add(key)
        return quote(str(values[key]), safe="")

    filled = _PATH_PLACEHOLDER.sub(_replace, template)
    if missing:
        raise RestAssemblyError(f"REST 路径缺占位符 {missing}: {template}")
    return filled, used


def _remaining_params(params: Mapping[str, Any], used: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in used or value is None:
            continue
        out[key] = value
    return out


def _query_values(remaining: Mapping[str, Any]) -> dict[str, str]:
    query: dict[str, str] = {}
    for key, value in remaining.items():
        if isinstance(value, (dict, list)):
            query[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            query[key] = str(value)
    return query


def assemble_rest_request(envelope: E2AEnvelope, *, base_url: str) -> AssembledRestRequest:
    """信封 → REST/SSE；body 保持纯 params，上下文通过内部 header 映射。"""
    method = str(envelope.method or "").strip()
    if not method:
        raise RestAssemblyError("envelope.method 为空，无法组装 REST")

    values: dict[str, Any] = dict(envelope.params or {})
    verb, template = _lookup_route(
        method,
        is_stream=bool(envelope.is_stream),
        params=values,
    )
    if method in _METHODS_WITHOUT_PARAM_SESSION_ID:
        # 勿 setdefault 信封 session_id；并剥掉 params 里误带的客户端临时 id。
        values.pop("session_id", None)
    elif envelope.session_id and not values.get("session_id"):
        values["session_id"] = envelope.session_id

    filled, used = _fill_path(template, values)
    remaining = _remaining_params(values, used)
    api_root = normalize_agent_http_base(base_url)
    url = urljoin(api_root + "/", filled.lstrip("/"))

    accept = "text/event-stream" if envelope.is_stream else "application/json"
    headers = identity_headers(envelope, accept=accept)
    json_body: dict[str, Any] | None = None
    query: dict[str, str] | None = None
    if verb == "GET" or template.endswith("/history/stream"):
        query = _query_values(remaining) or None
    else:
        headers["Content-Type"] = "application/json"
        json_body = remaining
    return AssembledRestRequest(
        verb=verb,
        url=url,
        headers=headers,
        json_body=json_body,
        query=query,
        used_rpc_fallback=False,
    )
