# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 路由。每条路径直接调用一个处理函数，不查 WebSocket 的消息流水线。

runtime 客户端按路径表来：探活、聊天、事件流，以及没有单独网址时的 /rpc/{方法名}。
原先的列 Agent 和 /chat 仍保留。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jiuwenswarm.common.log_context import (
    bind_log_session,
    current_log_session_id,
    unbind_log_session,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.server import http_chat, http_rpc
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
        await self.app(scope, _replay_body(body), send)


class SessionLogMiddleware(BaseHTTPMiddleware):
    """每条网页请求进来时，把日志绑到这次的会话上，离开时解开。"""

    async def dispatch(self, request: Request, call_next):
        session_id = await _session_id_from_request(request)
        token, previous_trace = bind_log_session(session_id)
        logger.info(
            "[AgentHTTPServer] request arrived: method=%s path=%s session_id=%s",
            request.method,
            request.url.path,
            current_log_session_id(),
        )
        try:
            return await call_next(request)
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

    @app.post(f"{API_PREFIX}/rpc/{{method}}")
    async def rpc(method: str, request: Request):
        body = await _read_json(request)
        try:
            req_method = ReqMethod(method)
        except ValueError:
            return JSONResponse(
                {
                    "request_id": _header(request, "x-request-id", "http"),
                    "ok": False,
                    "error": {
                        "code": "UNKNOWN_METHOD",
                        "message": method,
                        "details": {},
                    },
                }
            )
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

            async def _run(current: RequestContext) -> None:
                await http_rpc.handle_http_rpc(current, is_stream=True)

            return _stream_response(ctx, _run)
        await invoke_unary(
            ctx,
            lambda current: http_rpc.handle_http_rpc(current, is_stream=False),
        )
        return JSONResponse(
            _client_json(ctx.sink.body or {}),
            status_code=ctx.sink.status_code,
        )

    return app


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
        if current is None:
            return
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
    """先告诉 Gateway 这条推送流已经接上，然后一直留着，直到对方断开。"""
    yield _sse_frame({"event_type": "gateway.push_ready"})
    while True:
        await asyncio.sleep(15)
        if await request.is_disconnected():
            break
        yield ": keepalive\n\n"


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
    agent_request = AgentRequest(
        request_id=_header(request, "x-request-id", "http"),
        channel_id=_header(request, "x-channel-id", "http"),
        session_id=session_id or None,
        req_method=req_method,
        params=body,
        is_stream=is_stream,
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


def _replay_body(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _session_id_from_request(request: Request) -> str:
    header = str(request.headers.get("x-session-id") or "").strip()
    if header:
        return header
    query = str(request.query_params.get("session_id") or "").strip()
    if query:
        return query
    body = await _read_json(request)
    return str(body.get("session_id") or "").strip()
