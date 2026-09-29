# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Sandbox API routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.sandbox import (
    AccessExtra,
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
from jiuwenbox.server.access import parse_extra_paths
from jiuwenbox.server.sandbox_manager import SandboxBackgroundExecRequest, SandboxExecRequest, SandboxListRequest

router = APIRouter(tags=["sandboxes"])
configure_logging()
logger = logging.getLogger(__name__)


def _mgr():
    from jiuwenbox.server.app import get_manager
    return get_manager()


class CreateSandboxRequest(BaseModel):
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    policy: dict[str, Any] | None = None
    policy_mode: PolicyMode = PolicyMode.OVERRIDE
    sandbox_id: str | None = None


class ExecRequest(BaseModel):
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin: str | None = None
    timeout_seconds: int | None = None
    extra: AccessExtra | None = None


class DownloadRequest(BaseModel):
    sandbox_path: str
    extra: AccessExtra | None = None


class ListFilesBody(BaseModel):
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True
    extra: AccessExtra | None = None


class SearchFilesBody(BaseModel):
    sandbox_path: str
    pattern: str
    exclude_patterns: list[str] | None = None
    extra: AccessExtra | None = None


class ListFilesQuery(BaseModel):
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True
    extra: str | None = None


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
async def exec_in_sandbox(sandbox_id: str, request: ExecRequest):
    stdin_data = request.stdin.encode() if request.stdin else None
    return await _mgr().exec_in_sandbox(
        sandbox_id=sandbox_id,
        request=SandboxExecRequest(
            command=list(request.command),
            workdir=request.workdir,
            env=request.env,
            stdin_data=stdin_data,
            timeout=request.timeout_seconds,
            extra=request.extra,
        ),
    )


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
            extra=getattr(request, "extra", None),
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
    return PlainTextResponse(logs)


@router.post("/sandboxes/{sandbox_id}/upload", status_code=204)
async def upload_file(
    sandbox_id: str,
    file: UploadFile = File(...),
    sandbox_path: str = Query(...),
    extra: str | None = Form(None),
):
    """Upload a file into the sandbox filesystem.

    ``extra`` is a JSON object ``{"paths": [...]}`` (multipart form field).
    Windows requires a non-empty list; Linux ignores it.
    """
    extra_obj = None if extra is None else extra
    if extra_obj is not None:
        parse_extra_paths(extra_obj, required=False)
    content = await file.read()
    await _mgr().upload_file_to_sandbox(
        sandbox_id, sandbox_path, content, extra=extra_obj,
    )
    return Response(status_code=204)


@router.post("/sandboxes/{sandbox_id}/download")
async def download_file_post(sandbox_id: str, request: DownloadRequest):
    try:
        content = await _mgr().download_file_from_sandbox(
            sandbox_id, request.sandbox_path, extra=request.extra,
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": f"File not found: {request.sandbox_path}"},
        )
    return Response(content=content, media_type="application/octet-stream")


@router.get("/sandboxes/{sandbox_id}/download", deprecated=True)
async def download_file(
    sandbox_id: str,
    sandbox_path: str = Query(...),
    extra: str | None = Query(None),
):
    """Deprecated GET download. Pass URL-encoded extra JSON; missing/empty → 403 on Windows."""
    try:
        content = await _mgr().download_file_from_sandbox(
            sandbox_id, sandbox_path, extra=extra,
        )
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"File not found: {sandbox_path}"})

    return Response(content=content, media_type="application/octet-stream")


@router.post("/sandboxes/{sandbox_id}/files/list")
async def list_files_post(sandbox_id: str, body: ListFilesBody):
    try:
        items = await _mgr().list_files_in_sandbox(
            sandbox_id=sandbox_id,
            request=SandboxListRequest(
                sandbox_path=body.sandbox_path,
                recursive=body.recursive,
                max_depth=body.max_depth,
                include_files=body.include_files,
                include_dirs=body.include_dirs,
                extra=body.extra,
            ),
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {body.sandbox_path}"},
        )
    return {"items": items}


@router.get("/sandboxes/{sandbox_id}/files", deprecated=True)
async def list_files(
    sandbox_id: str,
    query: Annotated[ListFilesQuery, Query()],
):
    """Deprecated GET list. Pass URL-encoded extra JSON; missing/empty → 403 on Windows."""
    try:
        items = await _mgr().list_files_in_sandbox(
            sandbox_id=sandbox_id,
            request=SandboxListRequest(
                sandbox_path=query.sandbox_path,
                recursive=query.recursive,
                max_depth=query.max_depth,
                include_files=query.include_files,
                include_dirs=query.include_dirs,
                extra=query.extra,
            ),
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {query.sandbox_path}"},
        )
    return {"items": items}


@router.post("/sandboxes/{sandbox_id}/files/search")
async def search_files_post(sandbox_id: str, body: SearchFilesBody):
    try:
        items = await _mgr().search_files_in_sandbox(
            sandbox_id=sandbox_id,
            sandbox_path=body.sandbox_path,
            pattern=body.pattern,
            exclude_patterns=body.exclude_patterns,
            extra=body.extra,
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {body.sandbox_path}"},
        )
    return {"items": items}


@router.get("/sandboxes/{sandbox_id}/search", deprecated=True)
async def search_files(
    sandbox_id: str,
    sandbox_path: str = Query(...),
    pattern: str = Query(...),
    exclude_patterns: list[str] | None = Query(None),
    extra: str | None = Query(None),
):
    """Deprecated GET search. Pass URL-encoded extra JSON; missing/empty → 403 on Windows."""
    try:
        items = await _mgr().search_files_in_sandbox(
            sandbox_id=sandbox_id,
            sandbox_path=sandbox_path,
            pattern=pattern,
            exclude_patterns=exclude_patterns,
            extra=extra,
        )
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"Directory not found: {sandbox_path}"})
    return {"items": items}
