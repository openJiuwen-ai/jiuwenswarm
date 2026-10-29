# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Enterprise file transport using the existing upstream routing identity."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from jiuwenswarm.common.obs_files import (
    AttachmentStore,
    MAX_FILE_BYTES,
    _obs_http_client,
    object_prefix,
)
from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.common.request_identity import normalize_routing_identity
from jiuwenswarm.server.runtime.attachments.upload_storage import safe_upload_filename


def file_routing_identity(request: Request) -> dict:
    return normalize_routing_identity(
        {key: request.headers.get(f"x-{key.replace('_', '-')}")
         for key in ("group_id", "bot_id", "user_id")},
        request.query_params,
    )


async def serve_presigned_download(request: Request, url: str, identity: dict) -> Response:
    """Proxy OBS's signed GET; no Gateway/AgentServer shared signing key."""
    try:
        store = AttachmentStore.from_env()
        tenant = build_tenant_key(identity["group_id"], identity["bot_id"], identity["user_id"])
        store.download_key(url, tenant)
    except (ValueError, KeyError):
        return JSONResponse({"error": "forbidden_download_url"}, status_code=403)
    except RuntimeError:
        return JSONResponse({"error": "object_store_unavailable"}, status_code=503)

    transport = _obs_http_client()
    upstream = None
    closed = False

    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            if upstream is not None:
                try:
                    upstream.close()
                finally:
                    upstream.release_conn()
        finally:
            transport.clear()

    try:
        range_header = request.headers.get("range", "")
        headers = {"Range": range_header} if range_header else {}
        # OBS signs a GET, not a HEAD. Probe one byte without downloading the file.
        if request.method == "HEAD" and not range_header:
            headers["Range"] = "bytes=0-0"
        upstream = await asyncio.to_thread(
            transport.request, "GET", url, headers=headers,
            redirect=False, preload_content=False,
        )
        if request.method == "HEAD" and not range_header and upstream.status == 416:
            # Empty objects cannot satisfy bytes=0-0; their ordinary GET is valid.
            try:
                upstream.close()
            finally:
                upstream.release_conn()
            upstream = await asyncio.to_thread(
                transport.request, "GET", url, headers={},
                redirect=False, preload_content=False,
            )
        if upstream.status not in {200, 206}:
            status = upstream.status if upstream.status in {403, 404, 410, 416} else 502
            close()
            return JSONResponse({"error": "obs_download_failed"}, status_code=status)
        session_id = request.query_params.get("session_id")
        if session_id and upstream.headers.get("x-amz-meta-session-id") != session_id:
            close()
            return JSONResponse({"error": "download_context_mismatch"}, status_code=403)
        name = safe_upload_filename(
            request.query_params.get("name") or unquote(urlsplit(url).path.rsplit("/", 1)[-1]),
            fallback="download",
        )
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline = request.query_params.get("inline", "").lower() in {"1", "true"}
        safe_inline = mime.startswith(("image/", "audio/", "video/")) and mime != "image/svg+xml"
        disposition = "inline" if inline and safe_inline else "attachment"
        response_headers = {
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "Accept-Ranges": "bytes", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
        }
        for key in ("Content-Length", "Content-Range"):
            if upstream.headers.get(key):
                response_headers[key] = upstream.headers[key]
        status = upstream.status
        if request.method == "HEAD":
            close()
            return Response(status_code=status, headers=response_headers, media_type=mime)
    except Exception:
        close()
        return JSONResponse({"error": "obs_download_failed"}, status_code=502)

    async def stream():
        try:
            while True:
                data = await asyncio.to_thread(upstream.read, 65536)
                if not data:
                    break
                yield data
        finally:
            close()

    return StreamingResponse(stream(), status_code=status, headers=response_headers, media_type=mime)


def register_enterprise_file_routes(app: FastAPI, channel) -> None:
    @app.api_route("/file-api/raw-file", methods=["GET", "HEAD"])
    async def raw_file(request: Request) -> Response:
        from jiuwenswarm.common.request_identity import apply_routing_metadata
        from jiuwenswarm.common.schema.message import ReqMethod
        from gateway.channel_manager.web.container_file_http import (
            _decode_verified_chunk, _error_json, serve_file_chunks,
        )
        from gateway.routing.e2a_proxy import fetch_agent_unary

        identity = file_routing_identity(request)
        session_id = request.query_params.get("session_id", "")
        path = request.query_params.get("path", "")
        if not all(identity.get(key) for key in ("group_id", "bot_id", "user_id")) or not session_id or not path:
            return JSONResponse({"error": "upload_identity_session_path_required"}, status_code=400)

        async def read_chunk(offset: int, limit: int):
            ok, result = await fetch_agent_unary(
                agent_client=getattr(channel, "agent_client", None),
                req_method=ReqMethod.FILE_READ_UPLOAD_CHUNK,
                params={"path": path, "offset": offset, "limit": limit},
                metadata=apply_routing_metadata(None, identity),
                session_id=session_id, user_id=identity["user_id"],
                channel_id=channel.channel_id, label="file.read_upload_chunk",
            )
            if not ok:
                code = str(result.get("code") or "SERVICE_UNAVAILABLE")
                return _error_json(
                    error="upload read failed", code=code,
                    status_code=503 if code == "SERVICE_UNAVAILABLE" else None,
                )
            decoded = _decode_verified_chunk(
                result, expected_offset=offset, expected_limit=limit
            )
            if decoded is not None:
                return decoded
            return JSONResponse(
                {"error": "invalid_upload_response"}, status_code=502
            )

        return await serve_file_chunks(request, read_chunk, inline=True)

    @app.post("/file-api/upload-obs")
    async def upload(request: Request) -> JSONResponse:
        identity = file_routing_identity(request)
        if not all(identity.get(key) for key in ("group_id", "bot_id", "user_id")):
            return JSONResponse(
                {"ok": False, "error": "routing_identity_required"}, status_code=400
            )
        # Bound the body before JSON/base64 decoding, including chunked requests.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (MAX_FILE_BYTES * 4 // 3) + 8192:
                return JSONResponse(
                    {"ok": False, "error": "file_too_large"}, status_code=413
                )
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            filename = safe_upload_filename(
                str(payload.get("filename") or ""), fallback="upload"
            )
            content = base64.b64decode(
                payload.get("content_base64") or "", validate=True
            )
            prefix = object_prefix(
                identity["group_id"],
                identity["bot_id"],
                identity["user_id"],
            )
            if not content or len(content) > MAX_FILE_BYTES:
                raise ValueError("file must contain 1..50 MiB")
        except (ValueError, TypeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_upload"}, status_code=400
            )
        try:
            store = AttachmentStore.from_env()
            result = await asyncio.to_thread(store.upload, content, filename, prefix)
        except Exception:
            # Never return object-store credentials, signed URLs or upstream error details.
            return JSONResponse(
                {"ok": False, "error": "object_store_unavailable"}, status_code=503
            )
        return JSONResponse({"ok": True, **result})

    @app.delete("/file-api/upload-obs")
    async def remove_upload(request: Request) -> JSONResponse:
        identity = file_routing_identity(request)
        if not all(identity.get(key) for key in ("group_id", "bot_id", "user_id")):
            return JSONResponse(
                {"ok": False, "error": "routing_identity_required"}, status_code=400
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse(
                    {"ok": False, "error": "invalid_upload"}, status_code=400
                )
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
                raise ValueError("attachment URL required")
            prefix = object_prefix(
                identity["group_id"], identity["bot_id"], identity["user_id"]
            )
            store = AttachmentStore.from_env()
            await asyncio.to_thread(store.remove_upload, payload["url"], prefix)
        except (ValueError, TypeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_upload"}, status_code=400
            )
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "object_store_unavailable"}, status_code=503
            )
        return JSONResponse({"ok": True})

    @app.api_route("/file-api/download", methods=["GET", "HEAD"])
    async def download(request: Request) -> Response:
        url = request.query_params.get("url", "")
        if not url or len(url) > 16384:
            return JSONResponse({"error": "invalid_download_url"}, status_code=400)
        return await serve_presigned_download(request, url, file_routing_identity(request))
