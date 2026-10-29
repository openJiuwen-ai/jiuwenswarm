# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 路由。每条路径直接调用一个处理函数，不查 WebSocket 的消息流水线。

和 dev-stable 一样：路径表里的动作各有自己的网址，例如新建对话是
``POST /sessions``。没有专用网址的方法不在这里接收。
原先的列 Agent 和 ``/chat`` 仍保留。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jiuwenswarm.common.log_context import (
    bind_log_session,
    current_log_session_id,
    unbind_log_session,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from gateway.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.server import http_chat
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.context import AgentServerServices, RequestContext
from jiuwenswarm.server.handlers import agents as agent_handlers
from jiuwenswarm.server.http_invoke import invoke_stream, invoke_unary
from jiuwenswarm.server.transports.http_sink import SSESink, UnaryHTTPSink

logger = logging.getLogger(__name__)

HTTP_CONNECTION_ID = "http"
API_PREFIX = "/api/v1"


class _RequestBodyTooLarge(Exception):
    """请求体超过和 WebSocket 相同的单条上限。"""


class RequestBodyLimitMiddleware:
    """请求体超过 WebSocket 单条上限时直接拒绝，避免整段读进内存。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > AGENT_WS_MAX_MESSAGE_BYTES:
            await _payload_too_large()(scope, receive, send)
            return
        try:
            body = await _read_bounded_body(receive)
        except _RequestBodyTooLarge:
            await _payload_too_large()(scope, receive, send)
            return
        if body is None:
            return
        await self.app(scope, _replay_body(body, receive), send)


class SessionLogMiddleware:
    """每条网页请求进来时，把日志绑到这次的会话上，离开时解开。

    不用 ``BaseHTTPMiddleware``：它会把流式响应当成客户端已断开并取消任务，
    ``chat/completions`` 和 ``events/stream`` 会在发出内容前结束。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session_id = _session_id_from_scope(scope)
        token, previous_trace = bind_log_session(session_id)
        method = scope.get("method", "")
        path = scope.get("path", "")
        logger.info(
            "[AgentHTTPServer] request arrived: method=%s path=%s session_id=%s",
            method,
            path,
            current_log_session_id(),
        )
        try:
            await self.app(scope, receive, send)
        finally:
            unbind_log_session(token, previous_trace)


def build_http_app(app_services: AppServices) -> FastAPI:
    """造出 HTTP 应用。业务函数通过工具箱拿服务，不接触 WebSocket 服务器。"""
    app = FastAPI(title="JiuwenSwarm HTTP", docs_url=None, redoc_url=None)
    app.add_middleware(SessionLogMiddleware)
    # 后加的包在最外层，先拦超大请求体，再记日志。
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.get(f"{API_PREFIX}/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post(f"{API_PREFIX}/agents/list")
    async def agents_list(request: Request) -> JSONResponse:
        ctx = await _context_from_body(
            app_services,
            request,
            req_method=ReqMethod.AGENTS_LIST,
            sink=UnaryHTTPSink(),
        )
        await invoke_unary(ctx, agent_handlers.handle_agents_list)
        return JSONResponse(ctx.sink.body or {}, status_code=ctx.sink.status_code)

    @app.post(f"{API_PREFIX}/chat")
    async def chat(request: Request):
        body = await _read_json(request)
        is_stream = bool(body.get("is_stream"))
        if is_stream:
            sink = SSESink()
            ctx = _context_from_mapping(
                app_services,
                body,
                req_method=_chat_method(body),
                sink=sink,
                is_stream=True,
            )

            async def _frames():
                task = asyncio.create_task(invoke_stream(ctx, http_chat.handle_http_chat))
                try:
                    async for frame in sink.frames():
                        yield frame
                finally:
                    await _stop_background_chat(task)

            return StreamingResponse(_frames(), media_type="text/event-stream")

        sink = UnaryHTTPSink()
        ctx = _context_from_mapping(
            app_services,
            body,
            req_method=_chat_method(body),
            sink=sink,
            is_stream=False,
        )
        await invoke_unary(ctx, http_chat.handle_http_chat)
        return JSONResponse(ctx.sink.body or {}, status_code=ctx.sink.status_code)

    @app.post(f"{API_PREFIX}/chat/completions")
    async def chat_completions(request: Request):
        return await _runtime_chat(app_services, request, ReqMethod.CHAT_SEND)

    @app.post(f"{API_PREFIX}/chat/resume")
    async def chat_resume(request: Request):
        return await _runtime_chat(app_services, request, ReqMethod.CHAT_RESUME)

    @app.get(f"{API_PREFIX}/events/stream")
    async def events_stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _event_frames(request),
            media_type="text/event-stream",
        )

    _register_declared_routes(app, app_services)
    return app


def _register_declared_routes(app: FastAPI, app_services: AppServices) -> None:
    """只注册已经有处理函数的专用网址。技能、插件、频道配置这些先不挂。"""
    from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
        declared_http_routes,
    )

    for method, verb, path in declared_http_routes():
        handler = _handler_for(method)
        if handler is None:
            continue
        app.add_api_route(
            f"{API_PREFIX}{path}",
            _make_method_endpoint(app_services, method, handler),
            methods=[verb],
            name=f"{verb} {path}",
        )
    history = _handler_for(ReqMethod.HISTORY_GET.value)
    if history is not None:
        app.add_api_route(
            f"{API_PREFIX}/sessions/{{session_id}}/history/stream",
            _make_method_endpoint(
                app_services, ReqMethod.HISTORY_GET.value, history, force_stream=True
            ),
            methods=["GET"],
            name="GET /sessions/{session_id}/history/stream",
        )


def _handler_for(method_name: str):
    """注册网址时就定下处理函数。聊天交给聊天适配，其余直接用处理函数表里的那一个。"""
    from jiuwenswarm.server.dispatch import HANDLERS

    try:
        req_method = ReqMethod(method_name)
    except ValueError:
        return None
    if http_chat.is_http_chat_method(req_method):
        return http_chat.handle_http_chat
    return HANDLERS.get(req_method)


def _make_method_endpoint(
    app_services: AppServices,
    method_name: str,
    handler,
    *,
    force_stream: bool = False,
):
    async def endpoint(request: Request):
        return await _serve_method(
            app_services,
            request,
            method_name,
            handler,
            force_stream=force_stream,
        )

    endpoint.__name__ = "http_" + method_name.replace(".", "_")
    return endpoint


async def _serve_method(
    app_services: AppServices,
    request: Request,
    method_name: str,
    handler,
    *,
    force_stream: bool,
):
    """专用网址直接调用注册时定下的处理函数。路径和查询里的参数并进请求体。"""
    try:
        req_method = ReqMethod(method_name)
    except ValueError:
        return JSONResponse(
            {
                "request_id": _header(request, "x-request-id", "http"),
                "ok": False,
                "error": {
                    "code": "UNKNOWN_METHOD",
                    "message": method_name,
                    "details": {},
                },
            }
        )
    params: dict[str, Any] = dict(request.query_params)
    params.update(dict(request.path_params or {}))
    params.update(await _read_json(request))
    is_stream = force_stream or _wants_stream(request)
    sink = SSESink() if is_stream else UnaryHTTPSink()
    ctx = _context_from_runtime(
        app_services,
        request,
        params,
        req_method=req_method,
        sink=sink,
        is_stream=is_stream,
    )
    if is_stream:
        return _stream_response(ctx, handler)
    await invoke_unary(ctx, handler)
    return JSONResponse(
        _client_json(ctx.sink.body or {}),
        status_code=ctx.sink.status_code,
    )


async def _stop_background_chat(task: asyncio.Task[None]) -> None:
    """流结束时等后台聊天收尾；客户端断开时先把它停掉。

    Python 3.11 起，当前任务已被取消时，``finally`` 里的 ``await`` 会马上再抛
    「已取消」，后台任务就没人取消、也没人等待。先把这次取消暂存起来，等后台
    停稳，再把取消还回去，让这次网页请求仍然按断开结束。

    正常读完时不要取消。取消只发生在请求本身已被取消，或生成器被关掉
    （客户端中途离开，生成器收到 GeneratorExit）的时候。
    """
    current = asyncio.current_task()
    abandoned = sys.exception() is not None or (
        current is not None and current.cancelling() > 0
    )
    if abandoned and not task.done():
        task.cancel()
    parked = 0
    if abandoned and current is not None:
        while current.cancelling():
            current.uncancel()
            parked += 1
    try:
        await task
    except asyncio.CancelledError:
        if not abandoned:
            raise
    finally:
        if current is not None:
            for _ in range(parked):
                current.cancel()


async def _runtime_chat(app_services: AppServices, request: Request, req_method: ReqMethod):
    """runtime 客户端的聊天。请求体是业务参数，身份在请求头里。"""
    body = await _read_json(request)
    is_stream = _wants_stream(request)
    sink = SSESink() if is_stream else UnaryHTTPSink()
    ctx = _context_from_runtime(
        app_services,
        request,
        body,
        req_method=req_method,
        sink=sink,
        is_stream=is_stream,
    )
    if is_stream:
        return _stream_response(ctx, http_chat.handle_http_chat)
    await invoke_unary(ctx, http_chat.handle_http_chat)
    return JSONResponse(_client_json(ctx.sink.body or {}), status_code=ctx.sink.status_code)


def _stream_response(ctx: RequestContext, handler) -> StreamingResponse:
    async def _frames():
        task = asyncio.create_task(invoke_stream(ctx, handler))
        try:
            async for frame in ctx.sink.frames():
                yield frame
        finally:
            await _stop_background_chat(task)

    return StreamingResponse(_frames(), media_type="text/event-stream")


async def _event_frames(request: Request):
    """Gateway 长连接：先报 push_ready，再转发本进程 ``send_push`` 的 wire。

    runtime 模式下 Gateway 没有 WebSocket，cron / 主动推送都靠这条 SSE。
    """
    from jiuwenswarm.server.gateway_push.http_hub import get_http_push_hub

    hub = get_http_push_hub()
    queue = hub.subscribe()
    try:
        yield _sse_frame({"event_type": "gateway.push_ready"})
        while True:
            if await request.is_disconnected():
                break
            try:
                wire = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _sse_frame(wire)
    finally:
        hub.unsubscribe(queue)


def _sse_frame(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _wants_stream(request: Request) -> bool:
    accept = request.headers.get("accept") or ""
    return "text/event-stream" in accept


def _header(request: Request, name: str, default: str) -> str:
    text = str(request.headers.get(name) or "").strip()
    return text or default


def _client_json(body: dict[str, Any]) -> dict[str, Any]:
    """收成 runtime 客户端认识的 ``{ok, data, error}``。"""
    ok = bool(body.get("ok"))
    result: dict[str, Any] = {"request_id": body.get("request_id"), "ok": ok}
    if body.get("metadata") is not None:
        result["metadata"] = body.get("metadata")
    if ok:
        payload = body.get("payload")
        result["data"] = payload if isinstance(payload, dict) else {"content": payload}
        return result
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    result["error"] = {
        "code": payload.get("code") or "ERROR",
        "message": str(payload.get("error") or payload.get("message") or ""),
        "details": payload,
    }
    return result


def _rebuild_identity_from_headers(request: Request) -> tuple[str, dict[str, Any] | None]:
    """从 Gateway 透传的 ``X-*`` 头重建 ``user_id`` 和 ``metadata``。

    Gateway 的 REST 正文只有业务参数。身份在头里：``X-User-Id``、路由三维、
    企业租户三字段，以及内部扩展头。企业版的专家包允许名单在
    ``X-Jiuwenswarm-Package-Allowlist``。没有 ``X-User-Id`` 时 ``user_id`` 为空，
    表示这次请求按匿名处理，和 WebSocket 入口缺省身份时一样。
    """
    from jiuwenswarm.common.request_identity import (
        apply_routing_metadata,
        normalize_routing_identity,
    )

    identity = normalize_routing_identity(
        {
            "user_id": _header(request, "x-user-id", ""),
            "group_id": _header(request, "x-group-id", ""),
            "bot_id": _header(request, "x-bot-id", ""),
            "gateway_id": _header(request, "x-gateway-id", ""),
        }
    )
    metadata: dict[str, Any] = apply_routing_metadata(None, identity) if identity else {}
    for field, header_name in (
        ("service_id", "x-service-id"),
        ("agent_id", "x-agent-id"),
        ("workspace_key", "x-workspace-key"),
    ):
        value = _header(request, header_name, "")
        if value:
            metadata[field] = value
    from jiuwenswarm.common.request_ext import (
        INTERNAL_HEADER_NAME,
        METADATA_KEY,
        RequestExtCodecError,
        decode_internal_header,
    )

    encoded_ext = request.headers.get(INTERNAL_HEADER_NAME)
    if encoded_ext:
        try:
            ext = decode_internal_header(encoded_ext)
        except RequestExtCodecError as exc:
            logger.warning(
                "[AgentHTTPServer] request ext header is invalid; ignored: %s",
                exc,
            )
        else:
            if ext:
                metadata[METADATA_KEY] = ext
    from jiuwenswarm.enterprise import is_enterprise

    if is_enterprise():
        from jiuwenswarm.server.runtime.package_allowlist import (
            HEADER_NAME,
            METADATA_KEY as GRANT_METADATA_KEY,
            decode_package_allowlist_header,
        )

        metadata[GRANT_METADATA_KEY] = decode_package_allowlist_header(
            request.headers.get(HEADER_NAME)
        )
    user_id = str(identity.get("user_id") or "").strip()
    return user_id, metadata or None


def _context_from_runtime(
    app_services: AppServices,
    request: Request,
    body: dict[str, Any],
    *,
    req_method: ReqMethod,
    sink: Any,
    is_stream: bool,
) -> RequestContext:
    session_id = _header(request, "x-session-id", "")
    if not session_id:
        session_id = str(body.get("session_id") or "").strip()
    user_id, metadata = _rebuild_identity_from_headers(request)
    agent_request = AgentRequest(
        request_id=_header(request, "x-request-id", "http"),
        channel_id=_header(request, "x-channel-id", "http"),
        session_id=session_id or None,
        req_method=req_method,
        params=body,
        is_stream=is_stream,
        metadata=metadata,
        user_id=user_id,
    )
    return RequestContext(
        request=agent_request,
        sink=sink,
        connection_id=HTTP_CONNECTION_ID,
        services=AgentServerServices(app_services),
    )


def _chat_method(body: dict[str, Any]) -> ReqMethod:
    raw = str(body.get("req_method") or ReqMethod.CHAT_SEND.value)
    try:
        method = ReqMethod(raw)
    except ValueError:
        return ReqMethod.CHAT_SEND
    if method in {ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME, ReqMethod.CHAT_ANSWER}:
        return method
    return ReqMethod.CHAT_SEND


async def _context_from_body(
    app_services: AppServices,
    request: Request,
    *,
    req_method: ReqMethod,
    sink: UnaryHTTPSink,
) -> RequestContext:
    return _context_from_mapping(
        app_services,
        await _read_json(request),
        req_method=req_method,
        sink=sink,
        is_stream=False,
    )


def _context_from_mapping(
    app_services: AppServices,
    body: dict[str, Any],
    *,
    req_method: ReqMethod,
    sink: Any,
    is_stream: bool,
) -> RequestContext:
    params = body.get("params")
    agent_request = AgentRequest(
        request_id=str(body.get("request_id") or "http"),
        channel_id=str(body.get("channel_id") or "http"),
        session_id=str(body.get("session_id") or "") or None,
        req_method=req_method,
        params=params if isinstance(params, dict) else {},
        is_stream=is_stream,
        metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
        user_id=str(body.get("user_id") or "").strip(),
    )
    return RequestContext(
        request=agent_request,
        sink=sink,
        connection_id=HTTP_CONNECTION_ID,
        services=AgentServerServices(app_services),
    )


def _content_length(scope: Scope) -> int | None:
    raw = Headers(scope=scope).get("content-length")
    if raw is None:
        return None
    try:
        size = int(raw)
    except ValueError:
        return None
    if size < 0:
        return None
    return size


def _payload_too_large() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "payload": {
                "code": "PAYLOAD_TOO_LARGE",
                "error": "request body exceeds the size limit",
            },
        },
        status_code=413,
    )


async def _read_bounded_body(receive: Receive) -> bytes | None:
    """按块读取。超过上限就停，不再把剩下的收进来。客户端先断开时返回 None。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] != "http.request":
            continue
        piece = message.get("body", b"")
        total += len(piece)
        if total > AGENT_WS_MAX_MESSAGE_BYTES:
            raise _RequestBodyTooLarge
        if piece:
            chunks.append(piece)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay_body(body: bytes, receive: Receive) -> Receive:
    """先交还已经读完的正文，之后继续听真正的断开。

    第二次读取就返回 ``http.disconnect`` 会让流式响应以为客户端已经离开，
    ``chat/completions`` 和 ``events/stream`` 会在第一帧后被取消。
    """
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _session_id_from_scope(scope: Scope) -> str:
    """只看请求头和查询串，不读正文，避免把流式连接提前消费掉。"""
    headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers") or []
    }
    header = headers.get("x-session-id", "").strip()
    if header:
        return header
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    values = query.get("session_id") or []
    return str(values[0]).strip() if values else ""


async def _session_id_from_request(request: Request) -> str:
    header = str(request.headers.get("x-session-id") or "").strip()
    if header:
        return header
    path_id = str((request.path_params or {}).get("session_id") or "").strip()
    if path_id:
        return path_id
    query = str(request.query_params.get("session_id") or "").strip()
    if query:
        return query
    body = await _read_json(request)
    return str(body.get("session_id") or "").strip()
