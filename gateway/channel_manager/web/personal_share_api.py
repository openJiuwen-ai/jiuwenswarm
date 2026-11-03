# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Personal /share-api adapter for Nginx; export state remains in the shared manager."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from gateway.channel_manager.web.share_image_export import (
    get_share_image_export_manager,
)
from gateway.channel_manager.web.share_snapshot import build_share_snapshot, get_share_session_metadata


_ACCESS_COOKIE = "jiuwenswarm_share_access"
_UPLOAD_TIMEOUT_SECONDS = 60 * 60
_UPLOAD_IDLE_SECONDS = 300
_MAX_UPLOADS = 4


def _access_cookie(request: Request) -> str:
    token = request.cookies.get(_ACCESS_COOKIE, "")
    return token if re.fullmatch(r"[a-f0-9]{64}", token) else ""


def _access_scope(request: Request, token: str = "") -> str:
    token = token or _access_cookie(request)
    if not token:
        return ""
    principal = str(getattr(request.state, "share_principal", "") or "")
    return hashlib.sha256(f"{principal}:{token}".encode()).hexdigest()


def _set_access_cookie(response: JSONResponse, request: Request, token: str) -> None:
    response.set_cookie(
        _ACCESS_COOKIE, token, path="/share-api/jobs", httponly=True,
        secure=request.url.scheme == "https", samesite="strict",
    )


class _UploadLimits:
    """Bound buffering before the manager takes ownership of the result file."""

    def __init__(self) -> None:
        self.jobs: set[str] = set()

    @asynccontextmanager
    async def claim(self, job_id: str):
        if job_id in self.jobs:
            raise HTTPException(409, "share_export_upload_in_progress")
        if len(self.jobs) >= _MAX_UPLOADS:
            raise HTTPException(429, "share_export_upload_busy")
        self.jobs.add(job_id)
        try:
            yield
        finally:
            self.jobs.discard(job_id)


async def _read_upload(request: Request, stream: Any, size: int) -> None:
    received = 0
    iterator = request.stream().__aiter__()
    async with asyncio.timeout(_UPLOAD_TIMEOUT_SECONDS):
        while True:
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout=_UPLOAD_IDLE_SECONDS)
            except StopAsyncIteration:
                break
            received += len(chunk)
            if received > size:
                raise ValueError("share_export_upload_incomplete")
            await asyncio.to_thread(stream.write, chunk)
        if received != size:
            raise ValueError("share_export_upload_incomplete")


def _manager():
    return get_share_image_export_manager()


def _error(exc: Exception) -> JSONResponse:
    code = 404 if isinstance(exc, FileNotFoundError) else 403 if isinstance(exc, PermissionError) else 400
    return JSONResponse({"error": str(exc)}, status_code=code)


async def _json_body(request: Request) -> dict[str, Any]:
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 64 * 1024:
                raise HTTPException(413, "request_too_large")
    except ClientDisconnect as exc:
        raise HTTPException(400, "client_disconnected") from exc
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

    async def authenticate(request: Request) -> None:
        hook = getattr(channel, "authenticate_handshake", None)
        if hook is None:
            return
        path = request.url.path
        if request.url.query:
            path += f"?{request.url.query}"
        remote = request.client.host if request.client else ""
        result = await hook(path=path, headers=request.headers, remote=remote, channel="web")
        if result is not None:
            if not result.success:
                raise HTTPException(401, "unauthorized")
            request.state.share_principal = str(result.user_id or "")

    async def authorize_job(request: Request, status: dict[str, Any]) -> None:
        if not _manager().can_access_job(str(status.get("job_id") or ""), _access_scope(request)):
            raise HTTPException(403, "share_export_access_denied")
        await get_share_session_metadata(request, channel, str(status.get("session_id") or ""))

    router = APIRouter(route_class=_ShareRoute, dependencies=[Depends(authenticate)])
    uploads = _UploadLimits()

    @router.get("/share-api/jobs")
    async def active_job(request: Request):
        sid = str(request.query_params.get("session_id") or "").strip()
        if not sid:
            raise HTTPException(400, "missing_session_id")
        status = _manager().get_active_status(sid)
        if status is not None:
            await authorize_job(request, status)
        response = JSONResponse(status)
        if not _access_cookie(request):
            _set_access_cookie(response, request, secrets.token_hex(32))
        return response

    @router.post("/share-api/jobs")
    async def create_job(request: Request):
        body = await _json_body(request)
        sid = body.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise HTTPException(400, "missing_session_id")
        sid = sid.strip()
        # Keep the normal routed read; browser ownership is checked separately.
        metadata = await get_share_session_metadata(request, channel, sid)
        job_id = body.get("job_id")
        if job_id is not None:
            status = _manager().get_status(str(job_id), include_client_token=True)
            if status is None or status.get("session_id") != sid:
                raise HTTPException(404, "job_not_found")
            await authorize_job(request, status)
            return JSONResponse({**status, "reused": True})
        status = _manager().get_active_status(sid, include_client_token=True)
        if status is not None:
            await authorize_job(request, status)
            return JSONResponse({**status, "reused": True})
        locale = body.get("locale")
        # This Gateway port has no static runner. Plain Web owns rendering;
        # Python Web and Electron retain their existing server/desktop paths.
        if body.get("allow_browser_render") is not True:
            raise HTTPException(400, "share_export_browser_render_required")
        snapshot, filename = await build_share_snapshot(request, channel, sid, metadata=metadata)
        cookie = _access_cookie(request) or secrets.token_hex(32)
        try:
            status = await asyncio.to_thread(
                _manager().create_job, session_id=sid, snapshot=snapshot, filename=filename,
                locale=locale.strip() if isinstance(locale, str) and locale.strip() else "zh",
                renderer="browser", base_url="", access_scope=_access_scope(request, cookie),
            )
        except PermissionError as exc:
            raise HTTPException(403, "share_export_access_denied") from exc
        response = JSONResponse(status, status_code=200 if status.get("reused") else 202)
        _set_access_cookie(response, request, cookie)
        return response

    @router.get("/share-api/jobs/{job_id}")
    async def job_status(job_id: str, request: Request):
        status = _manager().get_status(job_id)
        if status is None:
            raise HTTPException(404, "job_not_found")
        await authorize_job(request, status)
        return JSONResponse(status)

    @router.get("/share-api/jobs/{job_id}/snapshot")
    async def snapshot(job_id: str, request: Request):
        status = _manager().get_status(job_id)
        if status is None:
            raise HTTPException(404, "job_not_found")
        await authorize_job(request, status)
        path = _manager().get_snapshot_path(job_id)
        if path is None:
            raise HTTPException(404, "job_file_not_found")
        return FileResponse(path, media_type="application/json")

    @router.post("/share-api/jobs/{job_id}/browser")
    async def browser_update(job_id: str, request: Request):
        status = _manager().get_status(job_id)
        if status is None:
            raise HTTPException(404, "job_not_found")
        await authorize_job(request, status)
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
        await authorize_job(request, status)
        expected_token = str(status.get("client_token") or "").encode()
        if (
            status.get("renderer") != "browser"
            or not token
            or not secrets.compare_digest(token.encode(), expected_token)
        ):
            raise HTTPException(403, "share_export_client_not_authorized")
        if status.get("state") in {"completed", "failed"}:
            raise HTTPException(409, "share_export_job_already_finished")
        try:
            async with uploads.claim(job_id):
                size = int(request.headers.get("content-length") or "0")
                if not 0 < size <= 1024 ** 3:
                    raise ValueError("share_export_result_invalid")
                with tempfile.TemporaryFile() as stream:
                    try:
                        await _read_upload(request, stream, size)
                    except (ClientDisconnect, TimeoutError, ValueError):
                        # The manager owns failure and duplicate-upload arbitration.
                        stream.seek(0)
                        try:
                            await asyncio.to_thread(
                                _manager().store_browser_result, job_id, token,
                                str(request.query_params.get("filename") or ""), stream, size,
                            )
                        except (OSError, ValueError, RuntimeError):
                            pass
                        raise
                    stream.seek(0)
                    result = await asyncio.to_thread(
                        _manager().store_browser_result, job_id, token,
                        str(request.query_params.get("filename") or ""), stream, size,
                    )
                return JSONResponse(result)
        except (OSError, ValueError, RuntimeError, ClientDisconnect) as exc:
            return _error(exc)

    @router.api_route("/share-api/jobs/{job_id}/download", methods=["GET", "HEAD"])
    async def download(job_id: str, request: Request):
        status = _manager().get_status(job_id)
        if status is None:
            raise HTTPException(404, "job_not_found")
        await authorize_job(request, status)
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
