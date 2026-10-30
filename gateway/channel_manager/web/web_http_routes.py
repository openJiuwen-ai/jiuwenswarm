# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gateway Web HTTP + SSE routes on /api/v1 (core + mapped table).

Registers onto the FastAPI app from ``web_channel_app``; does not build an app.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.enterprise import is_enterprise
from gateway.channel_manager.web.http_routes.mapped import (
    WebHttpMappedRoute,
    catalog_entries,
    mapped_routes_for_edition,
)
from gateway.channel_manager.web.web_http_dispatch import dispatch_http_request
from gateway.channel_manager.web.web_http_timeouts import (
    resolve_web_http_history_timeout,
    resolve_web_http_sse_idle_timeout,
    resolve_web_http_sse_keepalive,
    resolve_web_http_sse_timeout,
    resolve_web_http_unary_timeout,
)

_HEADER_TO_PARAM = {
    "x-user-id": "user_id",
    "x-group-id": "group_id",
    "x-bot-id": "bot_id",
    "x-gateway-id": "gateway_id",
    "x-session-id": "session_id",
}

logger = logging.getLogger(__name__)


def _request_client_host(request: Request) -> str | None:
    client = request.client
    if client is None:
        return None
    host = str(getattr(client, "host", "") or "").strip()
    return host or None

_STREAM_RPC_METHODS = frozenset({"chat.send", "history.get"})


class _ExtraAllow(BaseModel):
    """OpenAPI: extra keys allowed, but do not render dummy additionalProp1."""

    model_config = ConfigDict(extra="allow")


def _session_create_schema(schema: dict[str, Any]) -> None:
    schema["example"] = {}
    schema["additionalProperties"] = True
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("examples", None)
            prop["default"] = None


class SessionCreateBody(_ExtraAllow):
    """All fields optional. Swagger Try it out should send ``{}``."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra=_session_create_schema,
    )
    mode: str | None = Field(
        default=None,
        description="可选。agent / code.normal / …；省略则用通道默认",
    )
    project_id: str | None = Field(default=None, description="可选。省略则绑定默认项目")
    project_dir: str | None = Field(default=None, description="可选")
    work_mode: str | None = Field(default=None, description="可选。work / code")
    create_token: str | None = Field(
        default=None,
        description="可选。幂等令牌；省略时 Gateway 自动生成",
    )


class SessionPatchBody(_ExtraAllow):
    title: str | None = None
    pinned: bool | None = None


class ChatSendBody(_ExtraAllow):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "session_id": "web_xxx",
                "query": "ping",
                "mode": "agent",
                "enable_streaming": True,
            }
        },
    )
    session_id: str = Field(..., description="已有会话 id（先 POST /sessions）")
    query: str = Field(..., description="用户消息，对齐 RPC chat.send")
    mode: str | None = "agent"
    enable_streaming: bool = True


class ChatActionBody(_ExtraAllow):
    pass


def _body_dict(body: BaseModel | dict[str, Any] | None) -> dict[str, Any]:
    if body is None:
        return {}
    if isinstance(body, dict):
        return dict(body)
    return body.model_dump(exclude_none=True)


def _history_params(session_id: str, page_idx: int | None) -> dict[str, Any]:
    return {"session_id": session_id, "page_idx": int(page_idx) if page_idx is not None else 1}


def _history_params_from_request(
    session_id: str,
    request: Request,
    page_idx: int | None,
) -> dict[str, Any]:
    """按页面实际发来的参数组装 history.get。

    当前页面用游标拉历史：``cursor=null`` 加 ``limit``。查询串会丢掉 null，
    所以只看到 ``limit``、没有 ``page_idx`` 时也按游标协议转发。缺失或空白
    cursor 必须转发为 ``None``（首页），不能写成空串——agentserver 会把
    ``""`` 当成非法游标并返回 ``INVALID_HISTORY_CURSOR``。
    """
    query = request.query_params
    use_cursor = "cursor" in query or (
        "limit" in query and "page_idx" not in query and page_idx is None
    )
    if not use_cursor:
        return _history_params(session_id, page_idx)
    raw_cursor = str(query.get("cursor") or "").strip()
    raw_limit = str(query.get("limit") or "").strip()
    try:
        limit = int(raw_limit) if raw_limit else 50
    except ValueError:
        limit = 50
    params: dict[str, Any] = {
        "session_id": session_id,
        "cursor": raw_cursor or None,
        "limit": limit,
    }
    subagent_id = str(query.get("subagent_id") or "").strip()
    if subagent_id:
        params["subagent_id"] = subagent_id
    return params


def _web_http_metadata(method: str) -> dict[str, Any]:
    return {"rpc_method": method, "transport": "web-http"}


def _response_headers(request_id: str, method: str = "") -> dict[str, str]:
    headers = {"X-Request-Id": request_id}
    if method:
        headers["X-Web-RPC-Method"] = method
    return headers


def _envelope_from_res(
    frame: dict[str, Any],
    request_id: str,
    *,
    rpc_method: str = "",
) -> tuple[dict[str, Any], int]:
    ok = bool(frame.get("ok", False))
    body: dict[str, Any] = {"request_id": request_id, "ok": ok}
    if ok:
        body["data"] = frame.get("payload") if frame.get("payload") is not None else {}
        body["metadata"] = _web_http_metadata(rpc_method) if rpc_method else {}
        return body, 200
    err_code = str(frame.get("code") or "INTERNAL_ERROR")
    body["error"] = {
        "code": err_code,
        "message": str(frame.get("error") or "request failed"),
        "details": frame.get("payload") if isinstance(frame.get("payload"), dict) else {},
    }
    if rpc_method:
        body["metadata"] = _web_http_metadata(rpc_method)
    status = {
        "BAD_REQUEST": 400,
        "UNAUTHORIZED": 401,
        "FORBIDDEN": 403,
        "NOT_FOUND": 404,
        "METHOD_NOT_FOUND": 404,
        "CONFLICT": 409,
        "TIMEOUT": 504,
        "SERVICE_UNAVAILABLE": 503,
        "WORKSPACE_QUOTA_EXCEEDED": 403,
    }.get(err_code, 500)
    return body, status


def _sse_pack(request_id: str, event: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"id: {request_id}\nevent: {event}\ndata: {payload}\n\n"


def register_web_http_api_routes(
    app: FastAPI,
    channel: Any,
    *,
    enterprise: bool | None = None,
) -> None:
    """Register CORE + mapped ``/api/v1`` routes on an existing FastAPI app.

    ``enterprise`` 默认读 ``is_enterprise()``；调用方也可显式传入。
    """
    enterprise_mode = is_enterprise() if enterprise is None else bool(enterprise)
    mapped_routes = mapped_routes_for_edition(enterprise=enterprise_mode)
    from gateway.channel_manager.web.http_routes.share_jobs import (
        register_share_job_routes,
    )

    register_share_job_routes(app)

    @app.get("/api/v1/health", tags=["health"], summary="探活")
    async def health(request: Request) -> JSONResponse:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        return JSONResponse(
            {"request_id": rid, "ok": True, "data": {"status": "ready"}, "metadata": {}},
            headers={"X-Request-Id": rid},
        )

    @app.get("/api/v1/connection/status", tags=["health"], summary="Agent 是否就绪")
    async def connection_status(request: Request) -> Response:
        return await _unary(request, channel, "connection.status", {})

    @app.get("/api/v1/sessions", tags=["sessions"], summary="列会话（本地 session.list）")
    async def session_list(
        request: Request,
        limit: int | None = Query(None, ge=1, le=200),
        offset: int | None = Query(None, ge=0),
    ) -> Response:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await _unary(request, channel, "session.list", params)

    @app.post(
        "/api/v1/sessions",
        tags=["sessions"],
        summary="建会话（body 可空，所有字段均可省略）",
        status_code=201,
    )
    async def session_create(
        request: Request,
        body: SessionCreateBody | None = Body(
            default=None,
            description="全部可选。直接 Execute 空对象即可；create_token 省略时由 Gateway 生成。",
            openapi_examples={
                "empty": {
                    "summary": "空 body（推荐）",
                    "value": {},
                },
            },
        ),
    ) -> Response:
        return await _unary(
            request, channel, "session.create", _body_dict(body), created=True,
        )

    @app.get(
        "/api/v1/sessions/{session_id}",
        tags=["sessions"],
        summary="会话元数据",
    )
    async def session_get(session_id: str, request: Request) -> Response:
        return await _unary(
            request, channel, "session.get_metadata", {"session_id": session_id},
        )

    @app.patch("/api/v1/sessions/{session_id}", tags=["sessions"], summary="重命名 / 置顶")
    async def session_patch(
        session_id: str, request: Request, body: SessionPatchBody | None = None,
    ) -> Response:
        payload = _body_dict(body)
        payload["session_id"] = session_id
        if "pinned" in payload or "pin" in payload:
            return await _unary(request, channel, "session.pin", payload)
        return await _unary(request, channel, "session.rename", payload)

    @app.delete("/api/v1/sessions/{session_id}", tags=["sessions"], summary="删除会话")
    async def session_delete(session_id: str, request: Request) -> Response:
        return await _unary(
            request, channel, "session.delete", {"session_id": session_id},
        )

    @app.get(
        "/api/v1/sessions/{session_id}/history",
        tags=["sessions"],
        summary="拉历史",
    )
    async def history_get(
        session_id: str,
        request: Request,
        page_idx: int | None = Query(None, ge=1),
    ) -> Response:
        params = _history_params_from_request(session_id, request, page_idx)
        accept = (request.headers.get("accept") or "").lower()
        if "text/event-stream" in accept:
            return await _stream(request, channel, "history.get", params)
        return await _history_json(request, channel, params)

    @app.get(
        "/api/v1/sessions/{session_id}/live",
        tags=["sessions"],
        summary="订阅正在执行的定时任务会话",
    )
    async def session_live(session_id: str, request: Request) -> Response:
        """把定时任务的执行结果以 SSE 推给已经打开的会话页。

        企业版页面没有个人版那种常驻长连接。执行会话编号以 ``cron_`` 或
        ``__cron__`` 开头时才允许订阅，其它会话直接 404。登录校验由企业版
        中间件完成。
        """
        sid = str(session_id or "").strip()
        if not sid.startswith("cron_") and not sid.startswith("__cron__"):
            return JSONResponse({"error": "not_cron_session"}, status_code=404)

        from gateway.cron.live_bus import cron_live_bus

        queue = cron_live_bus.subscribe(sid, replay=True)

        async def _frames() -> AsyncIterator[str]:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    event_name = str(frame.get("event") or "message")
                    data = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
                    data = {**data, "live_replay": bool(frame.get("replay"))}
                    yield _sse_pack(sid, event_name, data)
            finally:
                cron_live_bus.unsubscribe(sid, queue)

        return StreamingResponse(
            _frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/v1/chat/completions",
        tags=["chat"],
        summary="发消息（默认 SSE；Web RPC method=chat.send）",
    )
    @app.post(
        "/api/v1/chat/send",
        tags=["chat"],
        summary="发消息（兼容路径；请改用 /chat/completions）",
        include_in_schema=False,
    )
    async def chat_send(request: Request, body: ChatSendBody) -> Response:
        payload = _body_dict(body)
        accept = (request.headers.get("accept") or "").lower()
        want_stream = "text/event-stream" in accept or bool(
            payload.get("enable_streaming", True),
        )
        if want_stream:
            return await _stream(request, channel, "chat.send", payload)
        return await _unary(request, channel, "chat.send", payload, is_stream=False)

    @app.post("/api/v1/chat/resume", tags=["chat"], summary="恢复中断的对话")
    async def chat_resume(request: Request, body: ChatActionBody | None = None) -> Response:
        return await _unary(
            request, channel, "chat.resume", _body_dict(body), is_stream=True,
        )

    @app.post(
        "/api/v1/chat/{session_id}/actions/interrupt",
        tags=["chat"],
        summary="中断当前生成（Web RPC method=chat.interrupt）",
    )
    async def chat_interrupt(
        session_id: str, request: Request, body: ChatActionBody | None = None,
    ) -> Response:
        payload = _body_dict(body)
        payload["session_id"] = session_id
        return await _unary(request, channel, "chat.interrupt", payload, is_stream=True)

    @app.post(
        "/api/v1/chat/{session_id}/actions/user_answer",
        tags=["chat"],
        summary="回答 Agent 追问（Web RPC method=chat.user_answer）",
    )
    @app.post(
        "/api/v1/chat/{session_id}/actions/answer",
        tags=["chat"],
        summary="回答 Agent 追问（兼容旧路径；请改用 …/user_answer）",
        include_in_schema=False,
    )
    async def chat_user_answer(
        session_id: str, request: Request, body: ChatActionBody | None = None,
    ) -> Response:
        payload = _body_dict(body)
        payload["session_id"] = session_id
        return await _unary(request, channel, "chat.user_answer", payload, is_stream=True)

    @app.get(
        "/api/v1/catalog",
        tags=["Web HTTP catalog"],
        summary="HTTP → Web RPC method 对照（可追溯）",
    )
    async def web_http_catalog(request: Request) -> JSONResponse:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        return JSONResponse(
            {
                "request_id": rid,
                "ok": True,
                "data": {
                    "prefix": "/api/v1",
                    "routes": catalog_entries(routes=mapped_routes),
                },
                "metadata": _web_http_metadata("catalog"),
            },
            headers=_response_headers(rid, "catalog"),
        )

    @app.post(
        "/api/v1/harness/packages/actions/import-file",
        tags=["harness"],
        summary="导入 zip（multipart 字段 file）",
    )
    async def harness_import_file(
        request: Request,
        file: UploadFile = File(..., description="harness 包 zip"),
    ) -> Response:
        raw = await file.read()
        max_size = 50 * 1024 * 1024
        if len(raw) > max_size:
            rid = request.headers.get("x-request-id") or uuid.uuid4().hex
            return JSONResponse(
                {
                    "request_id": rid,
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "File exceeds 50MB limit",
                        "details": {},
                    },
                    "metadata": _web_http_metadata("harness.import"),
                },
                status_code=400,
                headers=_response_headers(rid, "harness.import"),
            )
        payload = {"file_content": base64.b64encode(raw).decode("ascii")}
        return await _unary(
            request, channel, "harness.import", payload, bind_session_param=False,
        )

    _register_mapped_routes(app, channel, mapped_routes)


async def _history_json(
    request: Request,
    channel: Any,
    params: dict[str, Any],
) -> JSONResponse:
    """Collect AgentServer history.message frames into a REST envelope."""
    outbound = None
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        outbound, rid, _sid = await dispatch_http_request(
            channel,
            method="history.get",
            params=params,
            headers=request.headers,
            request_id=req_id,
            is_stream=True,
            use_sse=True,
            client_host=_request_client_host(request),
        )
        messages: list[Any] = []
        total_pages: Any = None
        page_idx: Any = params.get("page_idx", 1)
        error_payload: dict[str, Any] | None = None
        async for frame in outbound.iter_sse_frames(
            rid,
            timeout=resolve_web_http_history_timeout(),
            idle_timeout=resolve_web_http_sse_idle_timeout(),
            keepalive=resolve_web_http_sse_keepalive(),
        ):
            ftype = frame.get("type")
            if ftype == "res":
                if not frame.get("ok", True):
                    body, status = _envelope_from_res(
                        frame, rid, rpc_method="history.get",
                    )
                    return JSONResponse(
                        body,
                        status_code=status,
                        headers=_response_headers(rid, "history.get"),
                    )
                continue
            if ftype != "event":
                continue
            ev = str(frame.get("event") or "")
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            if ev == "chat.error":
                error_payload = payload
                break
            if ev == "history.message":
                if payload.get("status") == "done":
                    total_pages = payload.get("total_pages", total_pages)
                    page_idx = payload.get("page_idx", page_idx)
                    break
                item = payload.get("message")
                if item is not None:
                    messages.append(item)
                total_pages = payload.get("total_pages", total_pages)
                page_idx = payload.get("page_idx", page_idx)
        if error_payload is not None:
            return JSONResponse(
                {
                    "request_id": rid,
                    "ok": False,
                    "error": {
                        "code": "NOT_FOUND",
                        "message": str(error_payload.get("error") or "history.get failed"),
                        "details": error_payload,
                    },
                },
                status_code=404,
                headers=_response_headers(rid, "history.get"),
            )
        return JSONResponse(
            {
                "request_id": rid,
                "ok": True,
                "data": {
                    "session_id": params.get("session_id"),
                    "messages": messages,
                    "total_pages": total_pages,
                    "page_idx": page_idx,
                },
                "metadata": _web_http_metadata("history.get"),
            },
            headers=_response_headers(rid, "history.get"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[WebHTTP] history json failed: %s", exc)
        try:
            from gateway.enterprise.capabilities.audit.audit_emit import emit_audit_evt

            emit_audit_evt(
                SUBMDL="gateway",
                PROC="web_http_internal_error",
                MSG=str(exc),
                EVT="web_http_internal_error",
                request_id=str(req_id or ""),
                method="history.get",
            )
        except Exception:  # noqa: BLE001
            logger.debug("[WebHTTP] web_http_internal_error audit skipped", exc_info=True)
        return JSONResponse(
            {
                "request_id": req_id,
                "ok": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}},
            },
            status_code=500,
            headers={"X-Request-Id": req_id},
        )
    finally:
        if outbound is not None:
            try:
                await channel.unregister_request_outbound(outbound)
            except Exception:  # noqa: BLE001
                logger.debug("[WebHTTP] unregister_request_outbound failed", exc_info=True)


def _chat_identity_error(
    request: Request, method: str, params: dict[str, Any], request_id: str,
) -> JSONResponse | None:
    """Check history-writing chat methods before dispatch or opening SSE."""
    if method not in ("chat.send", "chat.resume", "chat.user_answer"):
        return None
    for field in ("user_id", "group_id", "bot_id", "gateway_id"):
        header = (request.headers.get("x-" + field.replace("_", "-")) or "").strip()
        value = params.get(field)
        body_value = str(value).strip() if value is not None else ""
        # Match routing normalization: missing/null/blank identity is unset.
        if header and body_value and body_value != header:
            return JSONResponse(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": "IDENTITY_CONFLICT",
                        "message": "Request header and body identity must match",
                        "details": {"field": field},
                    },
                    "metadata": _web_http_metadata(method),
                },
                status_code=400,
                headers=_response_headers(request_id, method),
            )
    return None


async def _unary(
    request: Request,
    channel: Any,
    method: str,
    params: dict[str, Any],
    *,
    created: bool = False,
    is_stream: bool = False,
    bind_session_param: bool = True,
) -> JSONResponse:
    outbound = None
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    error = _chat_identity_error(request, method, params, req_id)
    if error is not None:
        return error
    try:
        outbound, req_id, _sid = await dispatch_http_request(
            channel,
            method=method,
            params=params,
            headers=request.headers,
            request_id=req_id,
            is_stream=is_stream or method in _STREAM_RPC_METHODS,
            use_sse=False,
            bind_session_param=bind_session_param,
            client_host=_request_client_host(request),
        )
        # For forwarded stream methods without SSE Accept, wait for first res (accepted).
        frame = await outbound.wait_response(
            req_id, timeout=resolve_web_http_unary_timeout(),
        )
        body, status = _envelope_from_res(frame, req_id, rpc_method=method)
        if created and body.get("ok"):
            status = 201
        return JSONResponse(
            body, status_code=status, headers=_response_headers(req_id, method),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[WebHTTP] unary %s failed: %s", method, exc)
        try:
            from gateway.enterprise.capabilities.audit.audit_emit import emit_audit_evt

            emit_audit_evt(
                SUBMDL="gateway",
                PROC="web_http_internal_error",
                MSG=str(exc),
                EVT="web_http_internal_error",
                request_id=str(req_id or ""),
                method=str(method or ""),
            )
        except Exception:  # noqa: BLE001
            logger.debug("[WebHTTP] web_http_internal_error audit skipped", exc_info=True)
        return JSONResponse(
            {
                "request_id": req_id,
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                    "details": {},
                },
                "metadata": _web_http_metadata(method),
            },
            status_code=500,
            headers=_response_headers(req_id, method),
        )
    finally:
        if outbound is not None:
            try:
                await channel.unregister_request_outbound(outbound)
            except Exception:  # noqa: BLE001
                logger.debug("[WebHTTP] unregister_request_outbound failed", exc_info=True)


async def _stream(
    request: Request,
    channel: Any,
    method: str,
    params: dict[str, Any],
) -> Response:
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    error = _chat_identity_error(request, method, params, req_id)
    if error is not None:
        return error

    async def gen() -> AsyncIterator[str]:
        outbound = None
        try:
            outbound, rid, _sid = await dispatch_http_request(
                channel,
                method=method,
                params=params,
                headers=request.headers,
                request_id=req_id,
                is_stream=True,
                use_sse=True,
                client_host=_request_client_host(request),
            )
            async for frame in outbound.iter_sse_frames(
                rid,
                timeout=resolve_web_http_sse_timeout(),
                idle_timeout=resolve_web_http_sse_idle_timeout(),
                keepalive=resolve_web_http_sse_keepalive(),
            ):
                if await request.is_disconnected():
                    logger.info(
                        "[WebHTTP] SSE client disconnected method=%s request_id=%s",
                        method,
                        rid,
                    )
                    break
                ftype = frame.get("type")
                if ftype == "keepalive":
                    yield ": keepalive\n\n"
                    continue
                if ftype == "res":
                    # Surface accepted / error as SSE event for clients that want it.
                    ev = "web.response" if frame.get("ok", True) else "chat.error"
                    data = frame.get("payload") if frame.get("ok", True) else {
                        "error": frame.get("error"),
                        "code": frame.get("code"),
                        **(frame.get("payload") or {}),
                    }
                    yield _sse_pack(rid, ev, data)
                    if not frame.get("ok", True):
                        return
                    continue
                if ftype == "event":
                    ev_name = str(frame.get("event") or "message")
                    payload = frame.get("payload") if frame.get("payload") is not None else {}
                    yield _sse_pack(rid, ev_name, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[WebHTTP] stream %s failed: %s", method, exc)
            yield _sse_pack(req_id, "chat.error", {"error": str(exc)})
        finally:
            if outbound is not None:
                try:
                    await channel.unregister_request_outbound(outbound)
                except Exception:  # noqa: BLE001
                    logger.debug("[WebHTTP] unregister_request_outbound failed", exc_info=True)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Request-Id": req_id,
            "X-Accel-Buffering": "no",
            "X-Web-RPC-Method": method,
        },
    )


def _coerce_query_value(raw: str) -> Any:
    text = str(raw).strip()
    lowered = text.lower()
    # 布尔只认显式字面量；``1``/``0`` 不归入布尔——它们更可能是页码/数量等数值
    # （如 ``page=1``），交给下面 isdigit() 转 int，避免被截胡成 True/False。
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    try:
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
    except ValueError:
        pass
    return text


def _merge_header_params(request: Request, params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    for header_name, param_name in _HEADER_TO_PARAM.items():
        if out.get(param_name):
            continue
        value = request.headers.get(header_name)
        if value:
            out[param_name] = value
    return out


async def _params_from_mapped_route(
    request: Request,
    spec: WebHttpMappedRoute,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = dict(spec.extra_params)
    for src, dst in spec.path_to_param.items():
        if src in request.path_params:
            params[dst] = request.path_params[src]
    for key in spec.query_keys:
        if key in request.query_params:
            params[key] = _coerce_query_value(request.query_params[key])
    raw_body = body
    if spec.accept_body and raw_body is None and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            try:
                parsed = await request.json()
            except Exception:  # noqa: BLE001
                parsed = None
            if isinstance(parsed, dict):
                raw_body = parsed
    if isinstance(raw_body, dict):
        merged = dict(raw_body)
        merged.update(params)
        params = merged
    return _merge_header_params(request, params)


def _register_mapped_routes(app: FastAPI, channel: Any, routes: tuple[WebHttpMappedRoute, ...]) -> None:
    """Mount table-driven Web HTTP routes. Static paths must appear before `{param}` rows."""

    for spec in routes:
        def _make(route: WebHttpMappedRoute):
            if route.accept_body:
                async def _handler(
                    request: Request,
                    body: dict[str, Any] | None = Body(default=None),
                ) -> Response:
                    params = await _params_from_mapped_route(request, route, body)
                    return await _unary(
                        request,
                        channel,
                        route.rpc_method,
                        params,
                        created=route.created,
                        bind_session_param=route.bind_session_param,
                    )
            else:
                async def _handler(request: Request) -> Response:
                    params = await _params_from_mapped_route(request, route)
                    return await _unary(
                        request,
                        channel,
                        route.rpc_method,
                        params,
                        created=route.created,
                        bind_session_param=route.bind_session_param,
                    )

            _handler.__name__ = (
                f"webhttp_{route.rpc_method.replace('.', '_')}_{route.http_method.lower()}"
            )
            _handler.__doc__ = route.summary
            return _handler

        app.add_api_route(
            f"/api/v1{spec.path}",
            _make(spec),
            methods=[spec.http_method],
            tags=[spec.tag],
            summary=spec.summary,
            name=f"webhttp_{spec.rpc_method}_{spec.http_method}_{spec.path}",
            response_model=None,
        )
