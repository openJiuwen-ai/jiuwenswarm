"""Sandbox API routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from jiuwenbox.models.sandbox import ExecResult, PolicyMode, SandboxRef, SandboxSpec
from jiuwenbox.server.sandbox_manager import SandboxExecRequest, SandboxListRequest

router = APIRouter(tags=["sandboxes"])
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _mgr():
    from jiuwenbox.server.app import get_manager
    return get_manager()


class CreateSandboxRequest(BaseModel):
    command: list[str] = Field(default_factory=list)
    workdir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    policy: dict[str, Any] | None = None
    policy_mode: PolicyMode = PolicyMode.OVERRIDE


class ExecRequest(BaseModel):
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin: str | None = None
    timeout_seconds: int | None = None


class ListFilesQuery(BaseModel):
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True


@router.post("/sandboxes", response_model=SandboxRef, status_code=201)
async def create_sandbox(request: CreateSandboxRequest):
    spec = SandboxSpec(
        workdir=request.workdir,
        env=request.env,
    )
    return await _mgr().create_sandbox(
        spec,
        command=request.command,
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
        ),
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
):
    """Upload a file into the sandbox filesystem.

    In process mode, this writes to the bind-mounted host path.
    """
    content = await file.read()
    await _mgr().upload_file_to_sandbox(sandbox_id, sandbox_path, content)
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
        return JSONResponse(status_code=404, content={"error": f"File not found: {sandbox_path}"})

    return Response(content=content, media_type="application/octet-stream")


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
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {query.sandbox_path}"},
        )
    return {"items": items}


@router.get("/sandboxes/{sandbox_id}/search")
async def search_files(
    sandbox_id: str,
    sandbox_path: str = Query(...),
    pattern: str = Query(...),
    exclude_patterns: list[str] | None = Query(None),
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
        return JSONResponse(status_code=404, content={"error": f"Directory not found: {sandbox_path}"})
    return {"items": items}
