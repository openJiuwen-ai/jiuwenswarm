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
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.channel_manager.web.invoke import dispatch_web_request as invoke_web_request
from jiuwenswarm.gateway.channel_manager.web.outbound import (
    HttpJsonOutbound,
    HttpSseOutbound,
    bind_http_session,
)
from jiuwenswarm.gateway.channel_manager.web.web_ws_transport import WebWsTransport

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
    return _ext_build(source)


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

    from jiuwenswarm.gateway.channel_manager.web.web_connect import (
        _WEB_CONNECTION_USER_ID_ATTR,
    )

    setattr(outbound, _WEB_CONNECTION_USER_ID_ATTR, user_id)
    # 连接级身份 scope（与 WS 的 ws._web_routing 对齐）：本地 handler（session.list
    # 等）经 _ws_identity_scope 读取，用于 PG 会话行的身份过滤。
    _http_routing: dict[str, str] = {}
    if user_id:
        _http_routing["user_id"] = user_id
    if trust_tenant_headers:
        _gid = _get_header(hdrs, "X-Group-Id")
        _bid = _get_header(hdrs, "X-Bot-Id")
        if _gid:
            _http_routing["group_id"] = _gid
        if _bid:
            _http_routing["bot_id"] = _bid
    if _http_routing:
        setattr(outbound, "_web_routing", _http_routing)
    channel.register_request_outbound(outbound)

    # remote 模式 session.create 走转发主路径：与 WS 上行对齐，登记请求，
    # 成功响应经 _enqueue_send 拦截后落 web 库会话行（见
    # WebWsTransport._capture_session_create_row；失败不阻塞，首条消息兜底）。
    if method == "session.create":
        transport = getattr(channel, "ws", channel)
        pending_creates = getattr(transport, "_pending_session_creates", None)
        if isinstance(pending_creates, dict):
            if len(pending_creates) >= 512:
                pending_creates.clear()
            pending_creates[req_id] = (
                outbound,
                str(params.get("title") or "").strip(),
            )

    # Enterprise ChatHistoryStore: HTTP path has no browser WS frame; synthesize the
    # same inbound shape WebChannel._handle_raw_message records as "browser".
    if method in ("chat.send", "chat.resume", "chat.user_answer"):
        try:
            import json as _json

            # 注入连接级身份到 params，保证 history 回调按正确用户/身份 scope 落库
            # （前端请求体不含 user_id/group_id/bot_id，它们来自 X-* Header / 握手 query）。
            # 无条件覆盖：客户端伪造的 user/user_id/group_id/bot_id 不能覆盖连接级身份（防冒充）。
            frame_params = dict(params) if isinstance(params, dict) else {}
            if user_id:
                frame_params["user_id"] = user_id
                frame_params["user"] = user_id
            for _fid in ("group_id", "bot_id"):
                _fval = _http_routing.get(_fid)
                if _fval:
                    frame_params[_fid] = _fval
            browser_frame = {
                "type": "req",
                "id": req_id,
                "method": method,
                "params": frame_params,
            }
            channel.rpc.record_history_frame(
                "browser", _json.dumps(browser_frame, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001
            logger.debug("[WebHTTP] history browser frame skipped", exc_info=True)

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
    if ext:
        setattr(outbound, "_web_request_ext", ext)

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
    _meta = apply_routing_metadata(
        {
            "query": query,
            "method": method,
            "ws_id": getattr(outbound, "_jiuwen_ws_id", ""),
            "transport": "web-http",
        },
        _routing,
    )

    user_message = Message(
        id=req_id,
        type="req",
        channel_id=getattr(channel, "channel_id", None) or channel_id_hdr or "web",
        session_id=session_id,
        params=params,
        timestamp=time.time(),
        ok=True,
        req_method=WebWsTransport.parse_req_method(method),
        mode=WebWsTransport.parse_mode(params.get("mode")),
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
