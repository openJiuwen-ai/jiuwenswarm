# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Sandbox API routes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.sandbox import (
    BackgroundExecRequest,
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobRequest,
    KillBackgroundJobResult,
    PolicyMode,
    SandboxRef,
    SandboxSpec,
)
from jiuwenbox.server.limits import (
    MAX_COMMAND_ITEMS,
    MAX_ENV_ITEMS,
    MAX_EXCLUDE_PATTERNS,
    MAX_SEARCH_PATTERN_CHARS,
    MAX_STDIN_CHARS,
    UPLOAD_CHUNK_BYTES,
    SandboxLimitError,
)
from jiuwenbox.server.sandbox_manager import (
    SandboxBackgroundExecRequest,
    SandboxExecRequest,
    SandboxListRequest,
)

router = APIRouter(tags=["sandboxes"])
configure_logging()
logger = logging.getLogger(__name__)


def _download_headers(sandbox_path: str) -> dict[str, str]:
    """Force a download and stop the browser from sniffing the body.

    The filename is the path's final segment, percent-encoded so a crafted
    path cannot inject header fields.
    """
    name = PurePosixPath(sandbox_path.replace("\\", "/")).name
    if name in {"", ".", ".."}:
        name = "download"
    encoded = quote(name, safe="")
    return {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "X-Content-Type-Options": "nosniff",
    }


def _mgr():
    from jiuwenbox.server.app import get_manager
    return get_manager()


class CreateSandboxRequest(BaseModel):
    command: list[str] = Field(default_factory=list, max_length=MAX_COMMAND_ITEMS)
    env: dict[str, str] = Field(default_factory=dict, max_length=MAX_ENV_ITEMS)
    policy: dict[str, Any] | None = None
    policy_mode: PolicyMode = PolicyMode.OVERRIDE
    sandbox_id: str | None = None


class ExecRequest(BaseModel):
    command: list[str] = Field(min_length=1, max_length=MAX_COMMAND_ITEMS)
    workdir: str | None = None
    env: dict[str, str] | None = Field(default=None, max_length=MAX_ENV_ITEMS)
    stdin: str | None = Field(default=None, max_length=MAX_STDIN_CHARS)
    timeout_seconds: int | None = None


class ListFilesQuery(BaseModel):
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True


@router.post("/sandboxes", response_model=SandboxRef, status_code=201)
async def create_sandbox(request: CreateSandboxRequest):
    if request.sandbox_id is None or request.sandbox_id.strip() == "":
        sandbox_id = None
    else:
        sandbox_id = request.sandbox_id
    spec = SandboxSpec(env=request.env, sandbox_id=sandbox_id)
    return await _mgr().create_sandbox(
        spec,
        policy_data=request.policy,
        policy_mode=request.policy_mode,
    )


@router.get("/sandboxes", response_model=list[SandboxRef])
async def list_sandboxes():
    return await _mgr().list_sandboxes()


@router.get("/sandboxes/{sandbox_id}", response_model=SandboxRef)
async def get_sandbox(sandbox_id: str):
    return await _mgr().get_sandbox(sandbox_id)


@router.delete("/sandboxes/{sandbox_id}", status_code=204)
async def delete_sandbox(sandbox_id: str):
    await _mgr().delete_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/start", response_model=SandboxRef)
async def start_sandbox(sandbox_id: str):
    return await _mgr().start_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/stop", response_model=SandboxRef)
async def stop_sandbox(sandbox_id: str):
    return await _mgr().stop_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/restart", response_model=SandboxRef)
async def restart_sandbox(sandbox_id: str):
    return await _mgr().restart_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/exec", response_model=ExecResult)
async def exec_in_sandbox(sandbox_id: str, request: ExecRequest, http_request: Request):
    stdin_data = request.stdin.encode() if request.stdin else None
    execution = _mgr().exec_in_sandbox(
        sandbox_id=sandbox_id,
        request=SandboxExecRequest(
            command=list(request.command),
            workdir=request.workdir,
            env=request.env,
            stdin_data=stdin_data,
            timeout=request.timeout_seconds,
        ),
    )
    # HTTP 客户端取消不会自动取消 ASGI handler. 每个 exec 使用独立
    # IPC 连接, 只取消当前执行即可通知 runner/daemon 回收当前命令.
    task = asyncio.create_task(execution)
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if done:
                break
            if await http_request.is_disconnected():
                return Response(status_code=499)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class BackgroundJobListResponse(BaseModel):
    items: list[BackgroundJobSummary]


@router.post("/sandboxes/{sandbox_id}/exec_background", response_model=BackgroundExecResult)
async def exec_background_in_sandbox(
    sandbox_id: str,
    request: BackgroundExecRequest,
):
    stdin_data = request.stdin.encode() if request.stdin else None
    return await _mgr().exec_background_in_sandbox(
        sandbox_id=sandbox_id,
        request=SandboxBackgroundExecRequest(
            command=list(request.command),
            job_id=request.job_id,
            workdir=request.workdir,
            env=request.env,
            stdin_data=stdin_data,
            timeout=request.timeout_seconds,
        ),
    )


@router.get(
    "/sandboxes/{sandbox_id}/background",
    response_model=BackgroundJobListResponse,
)
async def list_background_jobs_in_sandbox(
    sandbox_id: str,
    running_only: bool = False,
):
    items = await _mgr().list_background_jobs_in_sandbox(
        sandbox_id,
        running_only=running_only,
    )
    _mgr().limits.enforce_result_count(len(items))
    return BackgroundJobListResponse(items=items)


@router.get(
    "/sandboxes/{sandbox_id}/background/{job_id}",
    response_model=BackgroundJobStatus,
)
async def get_background_job_in_sandbox(sandbox_id: str, job_id: str):
    return await _mgr().get_background_job_in_sandbox(sandbox_id, job_id)


@router.post(
    "/sandboxes/{sandbox_id}/background/{job_id}/kill",
    response_model=KillBackgroundJobResult,
)
async def kill_background_job_in_sandbox(
    sandbox_id: str,
    job_id: str,
    request: KillBackgroundJobRequest = KillBackgroundJobRequest(),
):
    return await _mgr().kill_background_job_in_sandbox(
        sandbox_id,
        job_id,
        signal=request.signal,
    )


@router.get("/sandboxes/{sandbox_id}/logs")
async def get_logs(sandbox_id: str):
    logs = await _mgr().get_logs(sandbox_id)
    _mgr().limits.enforce_text_response(logs)
    return PlainTextResponse(logs)


@router.post("/sandboxes/{sandbox_id}/upload", status_code=204)
async def upload_file(
    sandbox_id: str,
    file: UploadFile = File(...),
    sandbox_path: str = Query(...),
):
    """Upload a file into the sandbox filesystem.

    In process mode, this writes to the bind-mounted host path.
    """
    limit = _mgr().limits.max_file_bytes
    declared_size = file.size
    if declared_size is not None and declared_size > limit:
        raise SandboxLimitError(
            "SANDBOX_UPLOAD_TOO_LARGE",
            f"upload file is {declared_size} bytes; limit is {limit} bytes",
            status_code=413,
        )
    content = bytearray()
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > limit:
            raise SandboxLimitError(
                "SANDBOX_UPLOAD_TOO_LARGE",
                f"upload file exceeds limit of {limit} bytes",
                status_code=413,
            )
    await _mgr().upload_file_to_sandbox(sandbox_id, sandbox_path, bytes(content))
    return Response(status_code=204)


@router.get("/sandboxes/{sandbox_id}/download")
async def download_file(
    sandbox_id: str,
    sandbox_path: str = Query(...),
):
    """Download a file from the sandbox filesystem."""
    try:
        content = await _mgr().download_file_from_sandbox(sandbox_id, sandbox_path)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "File not found"})

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=_download_headers(sandbox_path),
    )


@router.get("/sandboxes/{sandbox_id}/files")
async def list_files(
    sandbox_id: str,
    query: Annotated[ListFilesQuery, Query()],
):
    """List files and directories inside a sandbox path."""
    try:
        items = await _mgr().list_files_in_sandbox(
            sandbox_id=sandbox_id,
            request=SandboxListRequest(
                sandbox_path=query.sandbox_path,
                recursive=query.recursive,
                max_depth=query.max_depth,
                include_files=query.include_files,
                include_dirs=query.include_dirs,
            ),
        )
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "Directory not found"})
    _mgr().limits.enforce_result_count(len(items))
    return {"items": items}


@router.get("/sandboxes/{sandbox_id}/search")
async def search_files(
    sandbox_id: str,
    sandbox_path: str = Query(...),
    pattern: str = Query(..., min_length=1, max_length=MAX_SEARCH_PATTERN_CHARS),
    exclude_patterns: list[str] | None = Query(None, max_length=MAX_EXCLUDE_PATTERNS),
):
    """Search files under a sandbox path with shell-style glob patterns."""
    try:
        items = await _mgr().search_files_in_sandbox(
            sandbox_id=sandbox_id,
            sandbox_path=sandbox_path,
            pattern=pattern,
            exclude_patterns=exclude_patterns,
        )
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "Directory not found"})
    _mgr().limits.enforce_result_count(len(items))
    return {"items": items}
