# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Mount legacy ``/file-api/*`` and ``/share-api/*`` on Gateway Web HTTP."""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import logging
import os
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from jiuwenswarm.gateway.channel_manager.web.file_http import (
    FileHttpRoots,
    build_share_snapshot,
    content_disposition,
    default_file_http_roots,
    generate_agent_data,
    guess_mime,
    is_download_path_allowed,
    list_files,
    list_markdown,
    parse_single_byte_range,
    process_obs_upload_body,
    read_file_text,
    resolve_raw_file_path,
    save_pushed_file,
    safe_filename,
    write_markdown_content,
)

logger = logging.getLogger(__name__)

_OPENAPI_TAG = "file / share HTTP"
_DEFAULT_MAX_PUSH_BYTES = 64 * 1024 * 1024


def _max_push_upload_bytes() -> int:
    raw = os.getenv("GATEWAY_WEB_HTTP_MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return _DEFAULT_MAX_PUSH_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_PUSH_BYTES
    return value if value > 0 else _DEFAULT_MAX_PUSH_BYTES


def _is_loopback_client(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = str(getattr(client, "host", "") or "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _roots(app: FastAPI) -> FileHttpRoots:
    cached = getattr(app.state, "file_http_roots", None)
    if isinstance(cached, FileHttpRoots):
        return cached
    roots = default_file_http_roots()
    app.state.file_http_roots = roots
    return roots


def _json(status: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def catalog_file_compat_entries() -> list[dict[str, Any]]:
    paths = [
        ("GET", "/file-api/list-files", "列目录"),
        ("GET", "/file-api/list-markdown", "列 markdown"),
        ("GET", "/file-api/file-content", "读文本"),
        ("POST", "/file-api/file-content", "写 markdown"),
        ("GET", "/file-api/raw-file", "原始字节"),
        ("GET", "/file-api/download", "token 或 OBS 代理下载"),
        ("GET", "/file-api/ws-debug-config", "WS 调试读"),
        ("POST", "/file-api/ws-debug-config", "WS 调试写"),
        ("POST", "/file-api/rebuild-agent-data", "重建 agent-data"),
        ("POST", "/file-api/upload-obs", "企业 MinIO 上传"),
        ("POST", "/file-api/push", "企业本地落盘+token"),
        ("GET", "/share-api/snapshot", "分享 snapshot"),
    ]
    return [
        {
            "http_method": method,
            "path": path,
            "rpc_method": None,
            "phase": "compat",
            "note": note,
        }
        for method, path, note in paths
    ]


def register_file_compat_routes(app: FastAPI) -> None:
    """Register ``/file-api/*`` and ``/share-api/snapshot`` on the Web HTTP app."""

    # In-process debug flag (was class var on app_web handler).
    if not hasattr(app.state, "ws_disable_compress"):
        app.state.ws_disable_compress = False

    @app.get(
        "/file-api/list-markdown",
        tags=[_OPENAPI_TAG],
        summary="列出目录下 markdown",
    )
    async def file_list_markdown(
        request: Request,
        dir_path: str = Query("", alias="dir"),
    ) -> JSONResponse:
        status, body = list_markdown(_roots(request.app), dir_path)
        return _json(status, body)

    @app.get(
        "/file-api/list-files",
        tags=[_OPENAPI_TAG],
        summary="列出目录文件",
    )
    async def file_list_files(
        request: Request,
        dir_path: str = Query("", alias="dir"),
    ) -> JSONResponse:
        status, body = list_files(_roots(request.app), dir_path)
        return _json(status, body)

    @app.get(
        "/file-api/file-content",
        tags=[_OPENAPI_TAG],
        summary="读取文本文件",
    )
    async def file_content_get(
        request: Request,
        path: str = Query(""),
        encoding: str = Query("utf-8"),
    ) -> Response:
        status, err, body, used = read_file_text(_roots(request.app), path, encoding)
        if err is not None:
            return _json(status, err)
        headers = {"Cache-Control": "no-store"}
        if used:
            headers["X-Original-Encoding"] = used
        return Response(
            content=body or b"",
            status_code=200,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )

    @app.post(
        "/file-api/file-content",
        tags=[_OPENAPI_TAG],
        summary="写入 markdown",
    )
    async def file_content_post(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return _json(400, {"error": "invalid_json"})
        if not isinstance(payload, dict):
            return _json(400, {"error": "invalid_json"})
        status, body = write_markdown_content(
            _roots(request.app),
            payload.get("path"),
            payload.get("content"),
        )
        return _json(status, body)

    @app.get(
        "/file-api/raw-file",
        tags=[_OPENAPI_TAG],
        summary="原始文件字节",
    )
    async def file_raw_get(
        request: Request,
        path: str = Query(""),
    ) -> Response:
        return _file_raw_response(request, path, head=False)

    @app.head("/file-api/raw-file", include_in_schema=False)
    async def file_raw_head(
        request: Request,
        path: str = Query(""),
    ) -> Response:
        return _file_raw_response(request, path, head=True)

    def _file_raw_response(request: Request, path: str, *, head: bool) -> Response:
        status, err, full_path = resolve_raw_file_path(_roots(request.app), path)
        if err is not None or full_path is None:
            return _json(status, err or {"error": "file_not_found"})
        mime = guess_mime(full_path)
        if head:
            return Response(
                status_code=200,
                media_type=mime,
                headers={
                    "Content-Length": str(full_path.stat().st_size),
                    "Cache-Control": "no-store",
                },
            )
        return FileResponse(
            path=str(full_path),
            media_type=mime,
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/file-api/download",
        tags=[_OPENAPI_TAG],
        summary="token 或 OBS 代理下载（支持 Range）",
    )
    async def file_download_get(
        request: Request,
        token: str = Query(""),
        url: str = Query(""),
        name: str = Query(""),
        inline: str = Query(""),
    ) -> Response:
        return await _file_download_response(
            request, token, inline, head=False, obs_url=url, file_name=name
        )

    @app.head("/file-api/download", include_in_schema=False)
    async def file_download_head(
        request: Request,
        token: str = Query(""),
        url: str = Query(""),
        name: str = Query(""),
        inline: str = Query(""),
    ) -> Response:
        return await _file_download_response(
            request, token, inline, head=True, obs_url=url, file_name=name
        )

    async def _file_download_response(
        request: Request,
        token: str,
        inline: str,
        *,
        head: bool,
        obs_url: str = "",
        file_name: str = "",
    ) -> Response:
        obs = (obs_url or "").strip()
        if obs:
            # Mirror upload-obs / push: personal edition must not expose OBS proxy.
            if not is_enterprise():
                return _json(404, {"error": "not_available"})
            from jiuwenswarm.gateway.message_handler.outbound_file_materialize import (
                proxy_obs_download_response_async,
            )

            return await proxy_obs_download_response_async(
                obs_url=obs,
                filename=file_name,
                inline=inline.lower() in {"1", "true"},
                head=head,
                range_header=request.headers.get("range") or request.headers.get("Range"),
            )
        if not token:
            return _json(400, {"error": "missing_token"})
        try:
            from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                validate_file_download_token,
            )
        except ImportError:
            return _json(500, {"error": "download_module_unavailable"})

        payload = validate_file_download_token(token)
        if payload is None:
            return _json(403, {"error": "invalid_or_expired_token"})

        file_path = str(payload.get("path") or "")
        if not file_path or not os.path.isfile(file_path):
            return _json(404, {"error": "file_not_found"})
        if not is_download_path_allowed(Path(file_path), _roots(request.app)):
            return _json(403, {"error": "forbidden_path"})

        file_size = os.path.getsize(file_path)
        # 优先用 token 内展示名（save_pushed_file 磁盘名为 ``{ts}_{name}``）。
        token_name = str(payload.get("name") or "").strip()
        file_name = (
            safe_filename(token_name) if token_name else os.path.basename(file_path)
        )
        mime_type = guess_mime(file_name)
        inline_flag = inline.lower() in {"1", "true"}
        range_header = request.headers.get("range") or request.headers.get("Range")
        byte_range = None
        if range_header:
            byte_range = parse_single_byte_range(range_header, file_size)
            if byte_range is None:
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{file_size}"},
                )

        start, end = byte_range or (0, max(0, file_size - 1))
        content_length = 0 if file_size == 0 else end - start + 1
        status_code = 206 if byte_range is not None else 200
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(content_length),
            "Accept-Ranges": "bytes",
            "Content-Disposition": content_disposition(file_name, inline=inline_flag),
            "Cache-Control": "no-store",
        }
        if byte_range is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        if head:
            return Response(status_code=status_code, headers=headers)

        def _iter() -> Iterator[bytes]:
            with open(file_path, "rb") as handle:
                handle.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(_iter(), status_code=status_code, headers=headers)

    @app.get(
        "/file-api/ws-debug-config",
        tags=[_OPENAPI_TAG],
        summary="WS 调试压缩开关（读）",
    )
    async def ws_debug_get(request: Request) -> JSONResponse:
        return _json(
            200,
            {"wsDisableCompress": bool(getattr(request.app.state, "ws_disable_compress", False))},
        )

    @app.post(
        "/file-api/ws-debug-config",
        tags=[_OPENAPI_TAG],
        summary="WS 调试压缩开关（写）",
    )
    async def ws_debug_post(request: Request) -> JSONResponse:
        if not _is_loopback_client(request):
            return _json(403, {"error": "forbidden"})
        try:
            payload = await request.json()
        except Exception:
            return _json(400, {"error": "invalid_json"})
        if not isinstance(payload, dict):
            return _json(400, {"error": "invalid_json"})
        flag = payload.get("wsDisableCompress")
        if not isinstance(flag, bool):
            return _json(400, {"error": "invalid_ws_disable_compress"})
        request.app.state.ws_disable_compress = flag
        logger.info("[file_http] ws disable compress updated: %s", flag)
        return _json(200, {"ok": True, "wsDisableCompress": flag})

    @app.post(
        "/file-api/rebuild-agent-data",
        tags=[_OPENAPI_TAG],
        summary="重建 agent-data.json",
    )
    async def rebuild_agent_data(request: Request) -> JSONResponse:
        try:
            generate_agent_data(_roots(request.app).project_root)
        except Exception as exc:  # noqa: BLE001
            return _json(500, {"error": "rebuild_failed", "detail": str(exc)})
        return _json(200, {"ok": True})

    @app.post(
        "/file-api/upload-obs",
        tags=[_OPENAPI_TAG],
        summary="企业 MinIO 上传",
    )
    async def upload_obs(request: Request) -> JSONResponse:
        if not is_enterprise():
            return _json(404, {"error": "not_available"})
        raw = await request.body()
        status, payload = process_obs_upload_body(raw)
        return _json(status, payload)

    @app.post(
        "/file-api/push",
        tags=[_OPENAPI_TAG],
        summary="企业文件落盘并签 download token（Gateway 本地）",
    )
    async def file_push(
        request: Request,
        file: UploadFile = File(...),
        session_id: str = Form("default"),
        filename: str = Form(""),
    ) -> JSONResponse:
        if not is_enterprise():
            return _json(404, {"error": "not_available"})
        try:
            raw = await file.read()
            if len(raw) > _max_push_upload_bytes():
                return _json(413, {"error": "file_too_large"})
            name = (filename or file.filename or "unnamed").strip() or "unnamed"
            result = save_pushed_file(
                file_bytes=raw,
                filename=name,
                session_id=session_id or "default",
            )
            return _json(200, result)
        except ValueError:
            return _json(400, {"error": "invalid_filename"})
        except Exception as exc:
            logger.error("[file_http] push failed: %s", exc, exc_info=True)
            return _json(500, {"error": "push_failed", "detail": str(exc)})

    @app.get(
        "/share-api/snapshot",
        tags=[_OPENAPI_TAG],
        summary="分享导出 snapshot",
    )
    async def share_snapshot(
        request: Request,
        session_id: str = Query(""),
        user: str | None = Query(None),
    ) -> JSONResponse:
        sid = (session_id or "").strip()
        if not sid:
            return _json(400, {"error": "missing_session_id"})
        user_q = (user or "").strip() or None
        if user_q is None:
            hdr = request.headers.get("x-user-id")
            if hdr and str(hdr).strip():
                user_q = str(hdr).strip()
        try:
            snapshot, filename = build_share_snapshot(session_id=sid, user=user_q)
        except FileNotFoundError:
            return _json(404, {"error": "history_not_found"})
        except ValueError as exc:
            return _json(400, {"error": str(exc)})
        return _json(200, {"filename": filename, "snapshot": snapshot})
