# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import base64
import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.common import utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from gateway.channel_manager.web.enterprise_files import register_enterprise_file_routes
from jiuwenswarm.server.runtime.attachments.upload_storage import (
    UPLOAD_READ_CHUNK_BYTES, read_session_upload_chunk,
)
from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import WorkspaceFileAdapter
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


@pytest.fixture
def upload(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    with TenantContext.scoped(utils.build_tenant_key("g", "b", "u")):
        root = TenantContext.current_tenant_root()
        directory = utils.get_agent_sessions_dir() / "session-a" / "uploads"
        directory.mkdir(parents=True)
        path = directory / "中文图片.png"
        data = b"\x89PNG\r\n" + bytes(range(256)) * 4000
        path.write_bytes(data)
        yield SimpleNamespace(root=root, path=path, data=data, directory=directory)


def test_read_chunks_match_uploaded_bytes(upload):
    chunks = []
    for offset in range(0, len(upload.data), UPLOAD_READ_CHUNK_BYTES):
        chunk = read_session_upload_chunk("session-a", str(upload.path), offset, UPLOAD_READ_CHUNK_BYTES)
        assert chunk["offset"] == offset and chunk["size"] == len(upload.data)
        assert chunk["name"] == upload.path.name and chunk["mime_type"] == "image/png"
        chunks.append(base64.b64decode(chunk["data_base64"]))
    assert b"".join(chunks) == upload.data and chunk["eof"]


@pytest.mark.parametrize("case", ["other_session", "other_tenant", "workspace", "nested", "relative", "traversal"])
def test_upload_reader_rejects_paths_outside_exact_session(upload, tmp_path, case):
    candidates = {
        "other_session": upload.directory.parent.parent / "other" / "uploads" / upload.path.name,
        "other_tenant": tmp_path / "other-tenant" / upload.path.name,
        "workspace": upload.root / "agent" / "jiuwenclaw_workspace" / upload.path.name,
        "nested": upload.directory / "subdir" / upload.path.name,
        "relative": upload.path.name,
        "traversal": str(upload.directory) + "/../uploads/" + upload.path.name,
    }
    with pytest.raises(PermissionError):
        read_session_upload_chunk("session-a", str(candidates[case]), 0, 10)


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, UPLOAD_READ_CHUNK_BYTES + 1), (2000000, 1)])
def test_upload_reader_bounds_chunks(upload, offset, limit):
    with pytest.raises(ValueError):
        read_session_upload_chunk("session-a", str(upload.path), offset, limit)


@pytest.mark.parametrize("level", ["file", "uploads", "session"])
def test_upload_reader_never_follows_symlinks(upload, tmp_path, level):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / upload.path.name).write_bytes(b"private outside bytes")
    if level == "file":
        upload.path.unlink()
        upload.path.symlink_to(outside / upload.path.name)
    else:
        target = upload.directory if level == "uploads" else upload.directory.parent
        target.rename(target.with_name(target.name + "-saved"))
        target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_session_upload_chunk("session-a", str(upload.path), 0, 20)


def test_upload_reader_directory_swap_keeps_open_descriptor(upload, tmp_path, monkeypatch):
    original_open = os.open
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / upload.path.name).write_bytes(b"private outside bytes")

    def swap_at_leaf(path, flags, *args, **kwargs):
        if path == upload.path.name and kwargs.get("dir_fd") is not None:
            upload.directory.rename(upload.directory.with_name("uploads-saved"))
            upload.directory.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_at_leaf)
    result = read_session_upload_chunk("session-a", str(upload.path), 0, 20)
    assert base64.b64decode(result["data_base64"]) == upload.data[:20]


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory"])
def test_upload_reader_rejects_linked_or_special_files(upload, tmp_path, kind):
    upload.path.unlink()
    if kind == "hardlink":
        outside = tmp_path / "private.txt"
        outside.write_bytes(b"private outside bytes")
        os.link(outside, upload.path)
    elif kind == "fifo":
        os.mkfifo(upload.path)
    else:
        upload.path.mkdir()
    with pytest.raises(PermissionError):
        read_session_upload_chunk("session-a", str(upload.path), 0, 20)


@pytest.mark.asyncio
async def test_enterprise_raw_file_reuses_tenant_rpc_and_streaming(upload, monkeypatch):
    from gateway.routing import e2a_proxy

    calls = []

    async def fetch(**kwargs):
        calls.append(kwargs)
        identity = kwargs["metadata"]
        routing = identity["routing"]
        tenant = utils.build_tenant_key(routing["group_id"], routing["bot_id"], kwargs["user_id"])
        with TenantContext.scoped(tenant):
            response = await WorkspaceFileAdapter().handle(AgentRequest(
                request_id="read", channel_id="web", req_method=kwargs["req_method"],
                session_id=kwargs["session_id"], user_id=kwargs["user_id"],
                params=kwargs["params"], metadata=identity,
            ))
        return response.ok, response.payload

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", fetch)
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace(channel_id="web", agent_client=object()))
    params = {"path": str(upload.path), "session_id": "session-a", "user_id": "u", "group_id": "g", "bot_id": "b"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        response = await client.get("/file-api/raw-file", params=params)
        assert response.status_code == 200 and response.content == upload.data
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"].startswith("inline;")
        head = await client.head("/file-api/raw-file", params=params)
        assert head.status_code == 200 and not head.content
        assert int(head.headers["content-length"]) == len(upload.data)
        ranged = await client.get("/file-api/raw-file", params=params, headers={"Range": "bytes=3-17"})
        assert ranged.status_code == 206 and ranged.content == upload.data[3:18]
        invalid = await client.get("/file-api/raw-file", params=params, headers={"Range": "bytes=99999999-"})
        assert invalid.status_code == 416
        for field in ("user_id", "group_id", "bot_id", "session_id"):
            denied = await client.get("/file-api/raw-file", params={**params, field: "other"})
            assert denied.status_code == 403
        missing = await client.get("/file-api/raw-file", params={**params, "path": str(upload.directory / "missing.png")})
        assert missing.status_code == 404
        bad = await client.get("/file-api/raw-file", params={"path": str(upload.path)})
        assert bad.status_code == 400
    assert all(call["req_method"] == ReqMethod.FILE_READ_UPLOAD_CHUNK for call in calls)
    assert all(0 < call["params"]["limit"] <= UPLOAD_READ_CHUNK_BYTES for call in calls)


@pytest.mark.asyncio
async def test_raw_file_returns_unavailable_without_local_fallback(upload, monkeypatch):
    from gateway.routing import e2a_proxy

    async def unavailable(**kwargs):
        return False, {"code": "SERVICE_UNAVAILABLE"}

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", unavailable)
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace(channel_id="web", agent_client=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        response = await client.get("/file-api/raw-file", params={
            "path": str(upload.path), "session_id": "session-a", "user_id": "u", "group_id": "g", "bot_id": "b",
        })
    assert response.status_code == 503
