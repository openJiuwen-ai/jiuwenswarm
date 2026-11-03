# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Personal /share-api adapter for Nginx; export state remains in the shared manager."""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from gateway.channel_manager.web.share_image_export import (
    get_share_image_export_manager,
)
from gateway.channel_manager.web.share_snapshot import build_share_snapshot, get_share_session_metadata


def _manager():
    return get_share_image_export_manager()


def _error(exc: Exception) -> JSONResponse:
    code = 404 if isinstance(exc, FileNotFoundError) else 403 if isinstance(exc, PermissionError) else 400
    return JSONResponse({"error": str(exc)}, status_code=code)


async def _json_body(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 64 * 1024:
            raise HTTPException(413, "request_too_large")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid_json")
    return payload


class _ShareDownload(FileResponse):
    """Confirm browser downloads only after their actual HTTP stream completes."""

    def __init__(self, path: Path, filename: str, job_id: str, attempt: str | None) -> None:
        media_type = "application/zip" if filename.lower().endswith(".zip") else "image/png"
        super().__init__(path, filename=filename, media_type=media_type)
        self.headers["accept-ranges"] = "none"
        self.manager, self.job_id, self.attempt = _manager(), job_id, attempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A claimed attempt is one complete transfer, not a partial byte range.
        scope = {
            **scope,
            "headers": [
                (key, value) for key, value in scope.get("headers", [])
                if key.lower() not in {b"range", b"if-range"}
            ],
        }
        if scope.get("method") == "HEAD":
            await super().__call__(scope, receive, send)
            return
        if not self.manager.mark_browser_download_started(self.job_id, self.attempt):
            response = JSONResponse({"error": "share_export_download_attempt_invalid"}, status_code=409)
            await response(scope, receive, send)
            return
        succeeded = False

        async def observe(message):
            nonlocal succeeded
            await send(message)
            if message.get("type") == "http.response.pathsend" or (
                message.get("type") == "http.response.body" and not message.get("more_body", False)
            ):
                succeeded = True

        try:
            await super().__call__(scope, receive, observe)
        finally:
            self.manager.mark_browser_download_finished(self.job_id, succeeded, self.attempt)


class _ShareRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                response = JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
            response.headers["Cache-Control"] = "no-store"
            return response

        return handle


def register_personal_share_routes(app: FastAPI, channel: Any) -> None:
    """Mount only for personal Web; enterprise keeps its existing read-only routes."""

    router = APIRouter(route_class=_ShareRoute)

    @router.get("/share-api/jobs")
    async def active_job(request: Request):
        sid = str(request.query_params.get("session_id") or "").strip()
        if not sid:
            raise HTTPException(400, "missing_session_id")
        return JSONResponse(_manager().get_active_status(sid))

    @router.post("/share-api/jobs")
    async def create_job(request: Request):
        body = await _json_body(request)
        sid = body.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise HTTPException(400, "missing_session_id")
        sid = sid.strip()
        # Verify access through the same routed session read as normal Web RPCs.
        metadata = await get_share_session_metadata(request, channel, sid)
        job_id = body.get("job_id")
        if job_id is not None:
            status = _manager().get_status(str(job_id), include_client_token=True)
            if status is None or status.get("session_id") != sid:
                raise HTTPException(404, "job_not_found")
            return JSONResponse({**status, "reused": True})
        status = _manager().get_active_status(sid, include_client_token=True)
        if status is not None:
            return JSONResponse({**status, "reused": True})
        locale = body.get("locale")
        # This Gateway port has no static runner. Plain Web owns rendering;
        # Python Web and Electron retain their existing server/desktop paths.
        if body.get("allow_browser_render") is not True:
            raise HTTPException(400, "share_export_browser_render_required")
        snapshot, filename = await build_share_snapshot(request, channel, sid, metadata=metadata)
        status = await asyncio.to_thread(
            _manager().create_job, session_id=sid, snapshot=snapshot, filename=filename,
            locale=locale.strip() if isinstance(locale, str) and locale.strip() else "zh",
            renderer="browser", base_url="",
        )
        return JSONResponse(status, status_code=200 if status.get("reused") else 202)

    @router.get("/share-api/jobs/{job_id}")
    async def job_status(job_id: str):
        status = _manager().get_status(job_id)
        if status is None:
            raise HTTPException(404, "job_not_found")
        return JSONResponse(status)

    @router.get("/share-api/jobs/{job_id}/snapshot")
    async def snapshot(job_id: str):
        path = _manager().get_snapshot_path(job_id)
        if path is None:
            raise HTTPException(404, "job_file_not_found")
        return FileResponse(path, media_type="application/json")

    @router.post("/share-api/jobs/{job_id}/browser")
    async def browser_update(job_id: str, request: Request):
        try:
            return JSONResponse(_manager().update_browser_job(job_id, await _json_body(request)))
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return _error(exc)

    @router.post("/share-api/jobs/{job_id}/browser-result")
    async def browser_result(job_id: str, request: Request):
        token = request.headers.get("x-share-export-token", "")
        status = _manager().get_status(job_id, include_client_token=True)
        if status is None:
            raise HTTPException(404, "job_not_found")
        expected_token = str(status.get("client_token") or "").encode()
        if (
            status.get("renderer") != "browser"
            or not token
            or not secrets.compare_digest(token.encode(), expected_token)
        ):
            raise HTTPException(403, "share_export_client_not_authorized")
        try:
            size = int(request.headers.get("content-length") or "0")
            if not 0 < size <= 1024 ** 3:
                raise ValueError("share_export_result_invalid")
            with tempfile.TemporaryFile() as stream:
                received = 0
                iterator = request.stream().__aiter__()
                try:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(anext(iterator), timeout=60 * 60)
                        except StopAsyncIteration:
                            break
                        received += len(chunk)
                        if received > size:
                            raise ValueError("share_export_upload_incomplete")
                        await asyncio.to_thread(stream.write, chunk)
                    if received != size:
                        raise ValueError("share_export_upload_incomplete")
                except (ClientDisconnect, TimeoutError, ValueError):
                    # Let the manager own failure/duplicate-upload arbitration.
                    stream.seek(0)
                    try:
                        await asyncio.to_thread(_manager().store_browser_result, job_id, token,
                            str(request.query_params.get("filename") or ""), stream, size)
                    except (OSError, ValueError, RuntimeError):
                        pass
                    raise
                stream.seek(0)
                result = await asyncio.to_thread(_manager().store_browser_result, job_id, token,
                    str(request.query_params.get("filename") or ""), stream, size)
            return JSONResponse(result)
        except (OSError, ValueError, RuntimeError, ClientDisconnect) as exc:
            return _error(exc)

    @router.api_route("/share-api/jobs/{job_id}/download", methods=["GET", "HEAD"])
    async def download(job_id: str, request: Request):
        result = _manager().get_result(job_id)
        if result is None:
            status = _manager().get_status(job_id)
            return JSONResponse(
                {"error": "job_not_completed" if status else "job_not_found"},
                status_code=409 if status else 404,
            )
        path, filename = result
        return _ShareDownload(path, filename, job_id, request.query_params.get("download_attempt_id"))

    app.include_router(router)
