# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace HTTP 特殊路由（非一元 JSON）。

``workspace.download`` 成功时返回文件流，失败时仍为一元 JSON，
不能走 ``http_routes.mapped`` 表，故放在 ``http_routes.workspace`` 单独挂载。
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from jiuwenswarm.gateway.channel_manager.web.web_http_dispatch import dispatch_http_request
from jiuwenswarm.gateway.channel_manager.web.web_http_server import (
    resolve_web_http_unary_timeout,
)

logger = logging.getLogger(__name__)


def register_workspace_http_routes(app: FastAPI, channel: Any) -> None:
    """挂载 workspace 专用 HTTP 路由（当前仅 download 流式回包）。"""

    @app.get(
        "/api/v1/workspace/download",
        tags=["workspace"],
        summary="下载文件或目录 zip（成功为文件流；失败为一元 JSON）",
    )
    async def workspace_download(request: Request) -> Response:
        return await workspace_download_response(request, channel)


def _content_disposition_attachment(filename: str) -> str:
    safe = (filename or "download.bin").replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in safe).strip("._")
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = "download.bin"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"


async def workspace_download_response(request: Request, channel: Any) -> Response:
    """成功流式返回字节；错误保持一元 JSON envelope（§2.5.5）。"""
    # 延迟导入，避免与 web_http_app 形成顶层循环依赖。
    from jiuwenswarm.gateway.channel_manager.web.web_http_app import (
        _coerce_query_value,
        _envelope_from_res,
        _merge_header_params,
        _request_client_host,
        _response_headers,
        _web_http_metadata,
    )

    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    method = "workspace.download"
    params: dict[str, Any] = {}
    for key in (
        "relative_path",
        "group_id",
        "bot_id",
        "user_id",
        "service_id",
        "agent_id",
    ):
        if key in request.query_params:
            params[key] = _coerce_query_value(request.query_params[key])
    params = _merge_header_params(request, params)
    if not str(params.get("relative_path") or "").strip():
        return JSONResponse(
            {
                "request_id": req_id,
                "ok": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "relative_path required",
                    "details": {},
                },
                "metadata": _web_http_metadata(method),
            },
            status_code=400,
            headers=_response_headers(req_id, method),
        )

    outbound = None
    try:
        outbound, req_id, _sid = await dispatch_http_request(
            channel,
            method=method,
            params=params,
            headers=request.headers,
            request_id=req_id,
            is_stream=False,
            use_sse=False,
            bind_session_param=False,
            client_host=_request_client_host(request),
        )
        timeout = max(float(resolve_web_http_unary_timeout()), 180.0)
        frame = await outbound.wait_response(req_id, timeout=timeout)
        body, status = _envelope_from_res(frame, req_id, rpc_method=method)
        if not body.get("ok"):
            return JSONResponse(
                body, status_code=status, headers=_response_headers(req_id, method)
            )
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        raw_b64 = data.get("content_b64") or ""
        try:
            content = base64.b64decode(raw_b64)
        except Exception:  # noqa: BLE001
            return JSONResponse(
                {
                    "request_id": req_id,
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "invalid download payload",
                        "details": {},
                    },
                    "metadata": _web_http_metadata(method),
                },
                status_code=500,
                headers=_response_headers(req_id, method),
            )
        filename = str(data.get("filename") or "download.bin")
        content_type = str(data.get("content_type") or "application/octet-stream")
        headers = _response_headers(req_id, method)
        headers["Content-Disposition"] = _content_disposition_attachment(filename)
        headers["Content-Length"] = str(len(content))
        headers["Cache-Control"] = "no-store"
        return Response(
            content=content,
            media_type=content_type,
            headers=headers,
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[WebHTTP] workspace.download failed: %s", exc)
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
