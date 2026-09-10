# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gateway Web HTTP + SSE — FastAPI routes on /api/v1 (core + mapped table)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.channel_manager.web.web_http_dispatch import dispatch_http_request
from jiuwenswarm.gateway.channel_manager.web.web_http_routes import (
    WebHttpMappedRoute,
    catalog_entries,
    mapped_routes_for_edition,
)
from jiuwenswarm.gateway.channel_manager.web.web_http_file_compat import (
    catalog_file_compat_entries,
    register_file_compat_routes,
)
from jiuwenswarm.gateway.channel_manager.web.web_http_server import (
    resolve_web_http_history_timeout,
    resolve_web_http_sse_idle_timeout,
    resolve_web_http_sse_keepalive,
    resolve_web_http_sse_timeout,
    resolve_web_http_unary_timeout,
)
from jiuwenswarm.gateway.channel_manager.web.web_http_sessions_compat import (
    catalog_sessions_compat_entries,
    register_sessions_compat_routes,
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

# AgentServer 本地历史缺失（mnt pod 回收后按需重新拉起为空目录）时其 history.get
# 流式路径会发出该报错；remote(PG) 模式下网关按此签名拦截并从 PG 合成历史页。
_HISTORY_NOT_FOUND_SNIPPET = "invalid page_idx or session history not found"
# 与 server/wire_truncate.py 的 _HISTORY_PAGE_SIZE 保持一致（避免为分页常量引入 server 依赖）
_HISTORY_PG_PAGE_SIZE = 50
# history.get 等待 Agent 首帧的预算：pod 存活时首帧（accepted）亚秒级到达；
# 会话 pod 已回收时网关会走拉起/就绪等待（AGENT_SERVER_READY_TIMEOUT，最长数百秒）
# ——期间客户端表现为"历史数据加载中…"永久挂起。超过预算即回退 PG，不再干等。
_HISTORY_FIRST_FRAME_TIMEOUT = 20.0


async def _race_history_first_frame(
    frame_iter: AsyncIterator[dict[str, Any]],
    timeout: float,
) -> tuple[str, dict[str, Any] | None, "asyncio.Future[dict[str, Any]] | None"]:
    """竞速等待 SSE 帧流的第一个帧。

    返回 ``("frame", frame, None)`` / ``("end", None, None)``（流结束）/
    ``("timeout", None, first_task)``。超时不取消底层等待（让拉起流程继续），
    把仍 pending 的首帧任务交回调用方：后续要么放弃（``_discard_pending_first_frame``），
    要么继续等同一帧（``_finish_pending_first_frame``）。绝不能对同一生成器重新
    ``__anext__()``——上一个 anext 还在 pending，会抛 "asynchronous generator
    is already running" 的 RuntimeError。
    """
    first_task = asyncio.ensure_future(frame_iter.__anext__())  # type: ignore[arg-type]
    done, _pending = await asyncio.wait({first_task}, timeout=timeout)
    if first_task not in done:
        return ("timeout", None, first_task)
    if first_task.cancelled() or first_task.exception() is not None:
        return ("end", None, None)
    try:
        return ("frame", first_task.result(), None)
    except StopAsyncIteration:
        return ("end", None, None)


def _discard_pending_first_frame(
    first_task: "asyncio.Future[dict[str, Any]] | None",
) -> None:
    """放弃竞速期遗留的首帧任务（PG 回退接管响应时调用）。

    取消任务让底层 ``iter_sse_frames`` 生成器收到 CancelledError 正常收尾，
    并消费任务结果，避免 "Task exception was never retrieved" 噪音。
    """
    if first_task is None or first_task.done():
        return
    first_task.cancel()
    first_task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def _finish_pending_first_frame(
    first_task: "asyncio.Future[dict[str, Any]] | None",
) -> tuple[str, dict[str, Any] | None]:
    """PG 不可用时继续等竞速期遗留的同一帧（不重新驱动生成器）。

    生成器自带总超时/空闲超时，这里等其自然完成；流结束或出错按
    ``("end", None)`` 处理（调用方据此结束循环并走原有回退）。
    """
    if first_task is None:
        return ("end", None)
    try:
        return ("frame", await first_task)
    except StopAsyncIteration:
        return ("end", None)
    except Exception:  # noqa: BLE001
        return ("end", None)


async def _history_page_from_pg(
    request: Request, params: dict[str, Any],
) -> dict[str, Any] | None:
    """remote(PG) 模式下从 ChatHistoryStore 合成 ``history.get`` 分页。

    会话历史文件在按会话拉起的 AgentServer pod 本地盘，pod 回收后重拉的空目录
    必然报 "invalid page_idx or session history not found"。此时网关回退读 PG
    （chat 上行时 ``record_user``/``record_assistant`` 落库的消息）合成同形分页；
    PG 也没有则合成空页——语义是"该会话没有历史"，前端按空会话渲染而非报错。
    仅 remote 存储模式生效；返回 None 表示不回退（保持原错误透传）。
    """
    from jiuwenswarm.gateway.routing.session_index import is_remote_storage

    if not is_remote_storage():
        return None
    sid = str(params.get("session_id") or "").strip()
    if not sid:
        return None
    try:
        page_idx = int(params.get("page_idx") or 1)
    except (TypeError, ValueError):
        page_idx = 1
    page_idx = max(1, page_idx)
    uid = (request.headers.get("x-user-id") or "").strip() or "guest"
    try:
        from jiuwenswarm.channels.web.history_store.api import get_session_detail_strict_sync

        # strict 版：库不可用/查询失败抛异常（None 仅表示会话不存在）。
        # PG 故障时返回 None 让上游透传原始错误，而不是把故障伪装成"空历史"。
        detail = await asyncio.to_thread(get_session_detail_strict_sync, sid, None, user=uid)
    except Exception:  # noqa: BLE001
        logger.exception("[WebHTTP] history.get PG 回退读取失败（不合成空页）: session_id=%s", sid)
        return None
    records: list[dict[str, Any]] = []
    if isinstance(detail, dict):
        records = [m for m in (detail.get("messages") or []) if isinstance(m, dict)]
    total = len(records)
    total_pages = max(1, (total + _HISTORY_PG_PAGE_SIZE - 1) // _HISTORY_PG_PAGE_SIZE)
    ordered = list(reversed(records))  # 与 server get_conversation_history 一致：新→旧
    start = (page_idx - 1) * _HISTORY_PG_PAGE_SIZE
    page_messages = [
        {
            "role": m.get("role"),
            "content": m.get("content"),
            "timestamp": m.get("timestamp"),
            "session_id": sid,
            "request_id": m.get("request_id"),
        }
        for m in ordered[start:start + _HISTORY_PG_PAGE_SIZE]
    ]
    return {
        "session_id": sid,
        "messages": page_messages,
        "total_pages": total_pages,
        "page_idx": page_idx,
    }


def _history_page_sse_frames(rid: str, page: dict[str, Any]) -> list[str]:
    """把 PG 合成页打包为与 AgentServer 同形的 ``history.message`` SSE 帧序列。"""
    sid = str(page.get("session_id") or "")
    frames: list[str] = []
    for item in page.get("messages") or []:
        frames.append(_sse_pack(rid, "history.message", {
            "event_type": "history.message",
            "message": item,
            "session_id": sid,
            "total_pages": page.get("total_pages"),
            "page_idx": page.get("page_idx"),
        }))
    frames.append(_sse_pack(rid, "history.message", {
        "event_type": "history.message",
        "status": "done",
        "session_id": sid,
        "total_pages": page.get("total_pages"),
        "page_idx": page.get("page_idx"),
    }))
    return frames

_OPENAPI_TAGS = [
    {"name": "health", "description": "探活与 Agent 连接状态"},
    {"name": "sessions", "description": "会话 CRUD / 历史（RPC session.* / history.get）"},
    {"name": "chat", "description": "发消息 SSE / 中断 / 回答（RPC chat.*）"},
    {"name": "Web HTTP catalog", "description": "HTTP → Web RPC method 对照表（可追溯）"},
    {"name": "config", "description": "RPC config.get（企业只读）"},
    {"name": "models", "description": "RPC models.list（企业聊天下拉只读）"},
    {"name": "locale", "description": "RPC locale.get_conf / set_conf"},
    {"name": "cron", "description": "RPC cron.job.*（企业 CronPanel）"},
    {"name": "permissions", "description": "RPC permissions.*（浏览器设置；非 Claw Manager）"},
    {"name": "skills", "description": "RPC skills.*（个人目录 + 企业租户）"},
    {"name": "harness", "description": "RPC harness.*（本机包；进度事件仍走 SSE/WS）"},
    {
        "name": "enterprise history",
        "description": "兼容 Web Pod：GET /api/sessions* → ChatHistoryStore（非 /api/v1 session.list）",
    },
    {
        "name": "file / share HTTP",
        "description": "原 app_web /file-api/*、/share-api/*（静态 Web 已剥离）",
    },
]


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


# Backward-compatible OpenAPI alias (deprecated path name).
ChatCompletionsBody = ChatSendBody


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
    }.get(err_code, 500)
    return body, status


def _sse_pack(request_id: str, event: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"id: {request_id}\nevent: {event}\ndata: {payload}\n\n"


def _schedule_abandoned_outbound_cleanup(
    channel: Any,
    task: "asyncio.Future[Any]",
) -> None:
    """PG 回退后 dispatch 仍在后台跑时，完成后注销其 outbound，避免注册表泄漏。

    只在真正放弃接管 outbound 时调用。chat.send / history.get 的 dispatch 会在
    后台流启动后立刻返回；若在 await 前挂 done-callback，callback 会把仍在
    投递 SSE 的 outbound 关掉（秒回 accepted 后立即 EOF）。
    """

    def _cleanup(done: "asyncio.Future[Any]") -> None:
        if done.cancelled() or done.exception() is not None:
            return
        try:
            abandoned = done.result()[0]
        except Exception:  # noqa: BLE001
            return
        if abandoned is None:
            return
        asyncio.ensure_future(channel.unregister_request_outbound(abandoned))

    task.add_done_callback(_cleanup)


def create_web_http_app(channel: Any) -> FastAPI:
    """Build FastAPI app bound to an existing ``WebChannel`` instance."""
    enterprise_mode = is_enterprise()
    mapped_routes = mapped_routes_for_edition(enterprise=enterprise_mode)
    app = FastAPI(
        title="JiuwenSwarm Gateway Web HTTP",
        version="0.2.0",
        description=(
            "Gateway 对外 HTTP+SSE（映射 WebSocket RPC method）。"
            "核心：health / sessions / chat；设置：config / models / locale / cron；"
            "工作区：permissions / skills / harness。"
            "企业旁路历史：GET /api/sessions*（ChatHistoryStore）。"
            "文件/分享：/file-api/*、/share-api/*。"
            "对照表：GET /api/v1/catalog。在本页点 **Try it out** 即可发请求。"
        ),
        docs_url="/doc",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=_OPENAPI_TAGS,
        swagger_ui_parameters={"tryItOutEnabled": True, "persistAuthorization": True},
    )
    app.state.web_channel = channel

    @app.get("/", include_in_schema=False)
    @app.get("/doc/", include_in_schema=False)
    @app.get("/docs", include_in_schema=False)
    async def _doc_redirect() -> RedirectResponse:
        return RedirectResponse(url="/doc", status_code=307)

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
        params = _history_params(session_id, page_idx)
        accept = (request.headers.get("accept") or "").lower()
        if "text/event-stream" in accept:
            return await _stream(request, channel, "history.get", params)
        return await _history_json(request, channel, params)

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
                    "routes": catalog_entries(routes=mapped_routes)
                    + catalog_sessions_compat_entries()
                    + catalog_file_compat_entries(),
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
    register_sessions_compat_routes(app)
    register_file_compat_routes(app)
    return app


async def _history_json(
    request: Request,
    channel: Any,
    params: dict[str, Any],
) -> JSONResponse:
    """Collect history.message SSE frames into a REST envelope."""
    outbound = None
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        dispatch_task = asyncio.ensure_future(dispatch_http_request(
            channel,
            method="history.get",
            params=params,
            headers=request.headers,
            request_id=req_id,
            is_stream=True,
            use_sse=True,
            client_host=_request_client_host(request),
        ))
        done, _ = await asyncio.wait(
            {dispatch_task}, timeout=_HISTORY_FIRST_FRAME_TIMEOUT,
        )
        if dispatch_task not in done:
            page = await _history_page_from_pg(request, params)
            if page is not None:
                _schedule_abandoned_outbound_cleanup(channel, dispatch_task)
                return JSONResponse(
                    {
                        "request_id": req_id,
                        "ok": True,
                        "data": page,
                        "metadata": _web_http_metadata("history.get"),
                    },
                    headers=_response_headers(req_id, "history.get"),
                )
        outbound, rid, _sid = await dispatch_task
        messages: list[Any] = []
        total_pages: Any = None
        page_idx: Any = params.get("page_idx", 1)
        error_payload: dict[str, Any] | None = None
        saw_history = False
        frame_iter = outbound.iter_sse_frames(
            rid,
            timeout=resolve_web_http_history_timeout(),
            idle_timeout=resolve_web_http_sse_idle_timeout(),
            keepalive=resolve_web_http_sse_keepalive(),
        )
        pending_first: dict[str, Any] | None = None
        state, first, pending_task = await _race_history_first_frame(
            frame_iter, _HISTORY_FIRST_FRAME_TIMEOUT,
        )
        if state == "timeout":
            page = await _history_page_from_pg(request, params)
            if page is not None:
                _discard_pending_first_frame(pending_task)
                return JSONResponse(
                    {
                        "request_id": rid,
                        "ok": True,
                        "data": page,
                        "metadata": _web_http_metadata("history.get"),
                    },
                    headers=_response_headers(rid, "history.get"),
                )
            # PG 不可用：继续等竞速期遗留的同一帧（重新 __anext__ 会触发
            # "asynchronous generator is already running"）
            state, first = await _finish_pending_first_frame(pending_task)
        if state == "frame":
            pending_first = first
        while True:
            if pending_first is not None:
                frame, pending_first = pending_first, None
            else:
                try:
                    frame = await frame_iter.__anext__()
                except StopAsyncIteration:
                    break
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
                saw_history = True
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
            if _HISTORY_NOT_FOUND_SNIPPET in str(error_payload.get("error") or ""):
                page = await _history_page_from_pg(request, params)
                if page is not None:
                    return JSONResponse(
                        {
                            "request_id": rid,
                            "ok": True,
                            "data": page,
                            "metadata": _web_http_metadata("history.get"),
                        },
                        headers=_response_headers(rid, "history.get"),
                    )
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
        if not saw_history:
            # 流结束但没有任何 history 帧（转发静默失败）：remote(PG) 模式回退 PG。
            page = await _history_page_from_pg(request, params)
            if page is not None:
                return JSONResponse(
                    {
                        "request_id": rid,
                        "ok": True,
                        "data": page,
                        "metadata": _web_http_metadata("history.get"),
                    },
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
) -> StreamingResponse:
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex

    async def gen() -> AsyncIterator[str]:
        outbound = None
        try:
            dispatch_task = asyncio.ensure_future(dispatch_http_request(
                channel,
                method=method,
                params=params,
                headers=request.headers,
                request_id=req_id,
                is_stream=True,
                use_sse=True,
                client_host=_request_client_host(request),
            ))

            rid = req_id
            pending_first: dict[str, Any] | None = None
            if method == "history.get":
                done, _ = await asyncio.wait(
                    {dispatch_task}, timeout=_HISTORY_FIRST_FRAME_TIMEOUT,
                )
                if dispatch_task not in done:
                    # Agent 派发超预算（会话 pod 大概率已回收，正在走拉起等待）：
                    # remote(PG) 模式下直接回退 PG 合成历史页，避免客户端无限挂起。
                    page = await _history_page_from_pg(request, params)
                    if page is not None:
                        _schedule_abandoned_outbound_cleanup(channel, dispatch_task)
                        for sse in _history_page_sse_frames(rid, page):
                            yield sse
                        return
                # PG 不可用：保持原行为，继续等派发完成
                outbound, rid, _sid = await dispatch_task
            else:
                outbound, rid, _sid = await dispatch_task
            frame_iter = outbound.iter_sse_frames(
                rid,
                timeout=resolve_web_http_sse_timeout(),
                idle_timeout=resolve_web_http_sse_idle_timeout(),
                keepalive=resolve_web_http_sse_keepalive(),
            )
            if method == "history.get":
                state, first, pending_task = await _race_history_first_frame(
                    frame_iter, _HISTORY_FIRST_FRAME_TIMEOUT,
                )
                if state == "timeout":
                    # Agent 首帧超预算（pod 活着但迟迟不应答）：同样回退 PG。
                    page = await _history_page_from_pg(request, params)
                    if page is not None:
                        _discard_pending_first_frame(pending_task)
                        for sse in _history_page_sse_frames(rid, page):
                            yield sse
                        return
                    # PG 不可用：继续等竞速期遗留的同一帧（重新 __anext__ 会触发
                    # "asynchronous generator is already running"）
                    state, first = await _finish_pending_first_frame(pending_task)
                if state == "frame":
                    pending_first = first
            saw_history_payload = False
            while True:
                if pending_first is not None:
                    frame, pending_first = pending_first, None
                else:
                    try:
                        frame = await frame_iter.__anext__()
                    except StopAsyncIteration:
                        break
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
                    if ev_name == "history.message":
                        saw_history_payload = True
                    # G.CTL.03: if 内布尔条件不超过 3 个，isinstance 判断前置
                    history_error_text = (
                        str(payload.get("error") or "")
                        if isinstance(payload, dict)
                        else ""
                    )
                    if (
                        method == "history.get"
                        and ev_name == "chat.error"
                        and _HISTORY_NOT_FOUND_SNIPPET in history_error_text
                    ):
                        page = await _history_page_from_pg(request, params)
                        if page is not None:
                            for sse in _history_page_sse_frames(rid, page):
                                yield sse
                            return
                    yield _sse_pack(
                        rid,
                        ev_name,
                        payload,
                    )
            if method == "history.get" and not saw_history_payload:
                # 流结束（含"秒回 accepted 后立即 EOF"的转发静默失败路径）但没有
                # 任何 history 帧：remote(PG) 模式下回退 PG，保证前端一定能收到
                # history.message/done 帧复位加载状态，否则永远停在"历史数据加载中…"。
                page = await _history_page_from_pg(request, params)
                if page is not None:
                    for sse in _history_page_sse_frames(rid, page):
                        yield sse
                    return
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
