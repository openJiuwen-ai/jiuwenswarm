# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Dispatch Web HTTP calls into WebChannel RPC method pipeline."""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from jiuwenswarm.common.request_ext import attach_to_metadata as _ext_attach
from jiuwenswarm.common.request_ext import build_ext_from_source as _ext_build
from jiuwenswarm.common.schema.message import Message
from gateway.enterprise import is_enterprise
from gateway.channel_manager.web.invoke import dispatch_web_request as invoke_web_request
from gateway.channel_manager.web.outbound import (
    HttpJsonOutbound,
    HttpSseOutbound,
    bind_http_session,
)

logger = logging.getLogger(__name__)


def _header_map(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if headers is None:
        return out
    try:
        for k, v in headers.items():
            out[str(k)] = str(v)
    except Exception:  # noqa: BLE001
        pass
    return out


def _get_header(headers: dict[str, str], name: str) -> str:
    want = name.lower()
    for k, v in headers.items():
        if k.lower() == want:
            return str(v).strip()
    return ""


def _trust_client_tenant_headers(client_host: str | None) -> bool:
    explicit = os.getenv("GATEWAY_WEB_HTTP_TRUST_CLIENT_HEADERS", "").strip().lower()
    if explicit in {"1", "true", "yes"}:
        return True
    if explicit in {"0", "false", "no"}:
        return False
    # 企业用户面通过 NodePort/Web Pod 访问时，客户端地址不是回环地址。
    # 企业版的身份边界由上游认证和 ``is_enterprise()`` 控制，必须把选中的
    # user/group/bot 透传给 Runtime 路由；个人版继续只信任本机请求。
    if is_enterprise():
        return True
    host = str(client_host or "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _build_http_ext(
    hdrs: dict[str, str],
    query: dict[str, list[str]],
) -> dict[str, Any] | None:
    source: dict[str, Any] = dict(hdrs)
    for key, values in query.items():
        if not values:
            continue
        source[key] = values[0]
    ext = _ext_build(source)
    # The enterprise login integration has one built-in credential field.
    # Other extension fields continue to use the existing allowlist.
    if is_enterprise():
        user_token = _get_header(hdrs, "X-User-Token")
        if user_token:
            ext = dict(ext or {}, **{"X-User-Token": user_token})
    return ext


async def dispatch_http_request(
    channel: Any,
    *,
    method: str,
    params: dict[str, Any] | None,
    headers: Any = None,
    request_id: str | None = None,
    is_stream: bool = False,
    use_sse: bool = False,
    bind_session_param: bool = True,
    client_host: str | None = None,
) -> tuple[Any, str, str]:
    """HTTP adapter: bind request Outbound, then run shared ``invoke.dispatch_web_request``.

    Returns ``(outbound, request_id, session_id)``. Caller must
    ``unregister_request_outbound`` when finished consuming frames.
    Does **not** call ``register_ws``.

    ``use_sse=True`` selects ``HttpSseOutbound`` (SSE / history collectors);
    otherwise ``HttpJsonOutbound`` (unary ``wait_response``). ``is_stream`` only
    sets Message.is_stream for downstream handlers.
    """
    hdrs = _header_map(headers)
    params = dict(params or {})
    req_id = (request_id or _get_header(hdrs, "X-Request-Id") or uuid.uuid4().hex).strip()
    session_id, params = bind_http_session(
        method,
        params,
        header_session_id=_get_header(hdrs, "X-Session-Id"),
        bind_param=bind_session_param,
    )
    trust_tenant_headers = _trust_client_tenant_headers(client_host)
    user_id = _get_header(hdrs, "X-User-Id") if trust_tenant_headers else None
    channel_id_hdr = _get_header(hdrs, "X-Channel-Id") or "web"
    app_id = _get_header(hdrs, "X-App-Id") or "default"

    outbound: HttpJsonOutbound | HttpSseOutbound
    if use_sse:
        outbound = HttpSseOutbound(headers=hdrs, session_id=session_id)
    else:
        outbound = HttpJsonOutbound(headers=hdrs, session_id=session_id)

    from gateway.channel_manager.web.web_connect import (
        _WEB_CONNECTION_USER_ID_ATTR,
    )

    setattr(outbound, _WEB_CONNECTION_USER_ID_ATTR, user_id)
    channel.register_request_outbound(outbound)

    # 仅新建会话且解析到 user_id 时打 http_resolve_identity UA。
    if method == "session.create" and user_id:
        try:
            from gateway.enterprise.capabilities.audit.audit_emit import emit_audit_ua

            emit_audit_ua(
                SUBMDL="gateway",
                PROC="http_resolve_identity",
                UID=user_id,
                session_id=session_id,
                request_id=req_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[WebHTTP] http_resolve_identity audit skipped",
                exc_info=True,
            )

    query: dict[str, list[str]] = {}
    if trust_tenant_headers:
        for hk, hv in (
            ("user_id", user_id or ""),
            ("group_id", _get_header(hdrs, "X-Group-Id")),
            ("bot_id", _get_header(hdrs, "X-Bot-Id")),
            ("gateway_id", _get_header(hdrs, "X-Gateway-Id")),
        ):
            if hv:
                query[hk] = [hv]

    ext = _build_http_ext(hdrs, query if trust_tenant_headers else {})

    mode = str(params.get("mode") or "agent")
    agent_id = str(params.get("agent_id") or "default")

    from jiuwenswarm.common.request_identity import (
        apply_routing_metadata,
        normalize_routing_identity,
    )

    _routing = normalize_routing_identity(
        query,
        {"user_id": user_id} if user_id else None,
    )
    setattr(outbound, "_web_routing", _routing)
    _meta = apply_routing_metadata(
        {
            "query": query,
            "method": method,
            "ws_id": getattr(outbound, "_jiuwen_ws_id", ""),
            "transport": "web-http",
        },
        _routing,
    )

    from gateway.channel_manager.web.web_connect import WebChannel

    parse_req = getattr(channel, "parse_req_method", None) or WebChannel.parse_req_method
    parse_mode = getattr(channel, "parse_mode", None) or WebChannel.parse_mode

    user_message = Message(
        id=req_id,
        type="req",
        channel_id=getattr(channel, "channel_id", None) or channel_id_hdr or "web",
        session_id=session_id,
        params=params,
        timestamp=time.time(),
        ok=True,
        req_method=parse_req(method),
        mode=parse_mode(params.get("mode")),
        is_stream=bool(is_stream),
        app_id=app_id,
        agent_ref={"mode": mode, "id": agent_id},
        user_id=user_id,
        metadata=_ext_attach(_meta, ext=ext),
    )

    await invoke_web_request(
        channel,
        method=method,
        params=params,
        request_id=req_id,
        outbound=outbound,
        session_id=session_id,
        user_message=user_message,
    )
    return outbound, req_id, session_id
