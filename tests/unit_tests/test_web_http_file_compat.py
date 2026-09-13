# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway Web HTTP ``/file-api/*`` and ``/share-api/*`` compat."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
    build_file_download_info,
)
from jiuwenswarm.channels.web.history_store import ChatHistoryStore, set_default_store
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_http_app import create_web_http_app
from jiuwenswarm.gateway.channel_manager.web.file_http import FileHttpRoots
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


@pytest.fixture()
def file_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    agent_root = tmp_path / "agent"
    workspace = agent_root / "jiuwenclaw_workspace"
    workspace.mkdir(parents=True)
    (workspace / "hello.md").write_text("# hi\n", encoding="utf-8")
    image_path = agent_root / "sessions" / "s1" / "uploads" / "image.png"
    image_path.parent.mkdir(parents=True)
    image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
    image_path.write_bytes(image_bytes)

    roots = FileHttpRoots(
        project_root=tmp_path,
        workspace_root=agent_root,
        agent_teams_root=tmp_path / "agent-teams",
        logs_root=tmp_path / "logs",
        auto_harness_root=tmp_path / "auto-harness",
    )
    for d in (roots.agent_teams_root, roots.logs_root, roots.auto_harness_root):
        d.mkdir(parents=True, exist_ok=True)

    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    app = create_web_http_app(channel)
    app.state.file_http_roots = roots
    client = TestClient(app)
    return client, roots, image_path, image_bytes


def test_list_and_read_markdown(file_client):
    client, _roots, _img, _bytes = file_client
    r = client.get("/file-api/list-files", params={"dir": "agent/jiuwenclaw_workspace"})
    assert r.status_code == 200
    names = {f["name"] for f in r.json()["files"]}
    assert "hello.md" in names

    r = client.get(
        "/file-api/file-content",
        params={"path": "agent/jiuwenclaw_workspace/hello.md", "encoding": "utf-8"},
    )
    assert r.status_code == 200
    assert r.text.startswith("# hi")


def test_legacy_agent_data_path_returns_generated_content(file_client):
    """Old pre-rename path ``agent/workspace/agent-data.json`` must serve generated data."""
    client, roots, _img, _bytes = file_client
    assert not (roots.project_root / "agent" / "workspace").exists()

    r = client.get(
        "/file-api/file-content",
        params={"path": "agent/workspace/agent-data.json", "encoding": "utf-8"},
    )
    assert r.status_code == 200
    # generated at the new path and served from there
    assert (roots.workspace_root / "jiuwenclaw_workspace" / "agent-data.json").is_file()
    assert "hello.md" in r.text


def test_raw_file(file_client):
    client, roots, image_path, image_bytes = file_client
    rel = str(image_path.relative_to(roots.project_root)).replace("\\", "/")
    r = client.get("/file-api/raw-file", params={"path": rel})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == image_bytes


def test_path_traversal_forbidden(file_client):
    client, _roots, _img, _bytes = file_client
    r = client.get("/file-api/list-files", params={"dir": "../"})
    assert r.status_code == 403


def test_default_roots_allow_legacy_agent_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Personal layout: ``agent/jiuwenclaw_workspace`` under user workspace, not only multi-tenant agent root."""
    user_root = tmp_path / "home" / ".jiuwenswarm"
    agent_ws = user_root / "agent" / "jiuwenclaw_workspace"
    agent_ws.mkdir(parents=True)
    (agent_ws / "note.md").write_text("ok", encoding="utf-8")
    tenant_agent = user_root / "service_default" / "agent_default" / "agent"
    tenant_agent.mkdir(parents=True)

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: user_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_root_dir",
        lambda: tenant_agent,
    )

    from jiuwenswarm.gateway.channel_manager.web.file_http import default_file_http_roots, list_files

    roots = default_file_http_roots()
    code, payload = list_files(roots, "agent/jiuwenclaw_workspace")
    assert code == 200
    assert any(f["name"] == "note.md" for f in payload["files"])


def test_download_token_and_range(file_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client, _roots, _img, _bytes = file_client
    secret = "s" * 32
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_SECRET", secret)
    recv = tmp_path / "recv"
    recv.mkdir()
    monkeypatch.setenv("JIUWENSWARM_WEB_RECEIVED_FILES", str(recv))
    WebFileDownloadManager.reset_instance()
    target = recv / "dl.bin"
    target.write_bytes(b"0123456789")
    info = build_file_download_info(str(target), "dl.bin", "sess")
    token = info["download_token"]

    r = client.head(f"/file-api/download?token={quote(token)}")
    assert r.status_code == 200
    assert r.headers["content-length"] == "10"

    r = client.get(
        f"/file-api/download?token={quote(token)}",
        headers={"Range": "bytes=2-5"},
    )
    assert r.status_code == 206
    assert r.content == b"2345"


def test_share_enterprise_uses_history_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    store = ChatHistoryStore.memory()
    set_default_store(store)
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    client = TestClient(create_web_http_app(channel))

    import asyncio

    async def _seed() -> None:
        await store.record_user(
            request_id="r1", session_id="share-1", query="hello", ts=1.0, user="u1",
        )

    asyncio.run(_seed())

    r = client.get("/share-api/snapshot", params={"session_id": "share-1", "user": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot"]["session_id"] == "share-1"
    assert isinstance(body["snapshot"]["records"], list)


def test_push_lands_on_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_WEB_RECEIVED_FILES", str(tmp_path / "recv"))
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_SECRET", "t" * 32)
    WebFileDownloadManager.reset_instance()

    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    client = TestClient(create_web_http_app(channel))
    r = client.post(
        "/file-api/push",
        files={"file": ("a.txt", b"hello-push", "application/octet-stream")},
        data={"session_id": "s1", "filename": "a.txt"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["download_token"]
    assert Path(body["file_path"]).is_file()

    r2 = client.get("/file-api/download", params={"token": body["download_token"]})
    assert r2.status_code == 200
    assert r2.content == b"hello-push"


def test_push_sanitizes_path_traversal_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from jiuwenswarm.gateway.channel_manager.web.file_http import (
        is_path_under_directory,
        received_files_dir,
    )

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    recv = tmp_path / "recv"
    monkeypatch.setenv("JIUWENSWARM_WEB_RECEIVED_FILES", str(recv))
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    client = TestClient(create_web_http_app(channel))
    r = client.post(
        "/file-api/push",
        files={"file": ("evil.txt", b"bad", "application/octet-stream")},
        data={"session_id": "s1", "filename": "../../../outside.txt"},
    )
    assert r.status_code == 200
    saved = Path(r.json()["file_path"]).resolve()
    assert is_path_under_directory(received_files_dir(), saved)
    assert saved.name.endswith("_outside.txt")
    assert not (tmp_path / "outside.txt").exists()


def test_catalog_includes_file_routes(file_client):
    client, *_ = file_client
    r = client.get("/api/v1/catalog")
    assert r.status_code == 200
    paths = {row["path"] for row in r.json()["data"]["routes"]}
    assert "/file-api/list-files" in paths
    assert "/share-api/snapshot" in paths


def test_download_obs_proxy_streams(file_client, monkeypatch: pytest.MonkeyPatch):
    client, *_ = file_client
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    class _Cfg:
        endpoint = "127.0.0.1:9000"
        public_base_url = ""

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/plain", "Content-Length": "5"}

        def iter_content(self, chunk_size=65536):
            yield b"hello"

        def close(self):
            pass

    monkeypatch.setattr(
        "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
        lambda: _Cfg(),
    )
    with patch(
        "jiuwenswarm.gateway.message_handler.outbound_file_materialize.requests.get",
        return_value=_Resp(),
    ) as get_obs:
        r = client.get(
            "/file-api/download",
            params={
                "url": "http://127.0.0.1:9000/b/downloads/x.txt",
                "name": "x.txt",
            },
        )
    assert r.status_code == 200
    assert r.content == b"hello"
    get_obs.assert_called_once()
    assert get_obs.call_args.kwargs.get("allow_redirects") is False

    with patch(
        "jiuwenswarm.gateway.message_handler.outbound_file_materialize.requests.get",
        return_value=_Resp(),
    ):
        h = client.head(
            "/file-api/download",
            params={"url": "http://127.0.0.1:9000/b/downloads/x.txt", "name": "x.txt"},
        )
    assert h.status_code == 200


def test_download_obs_proxy_personal_not_available(
    file_client, monkeypatch: pytest.MonkeyPatch
):
    client, *_ = file_client
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    class _Cfg:
        endpoint = "127.0.0.1:9000"
        public_base_url = ""

    monkeypatch.setattr(
        "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
        lambda: _Cfg(),
    )
    r = client.get(
        "/file-api/download",
        params={"url": "http://127.0.0.1:9000/b/downloads/x.txt", "name": "x.txt"},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "not_available"


def test_download_obs_proxy_rejects_foreign_host(file_client, monkeypatch: pytest.MonkeyPatch):
    client, *_ = file_client
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    class _Cfg:
        endpoint = "127.0.0.1:9000"
        public_base_url = ""

    monkeypatch.setattr(
        "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
        lambda: _Cfg(),
    )
    r = client.get(
        "/file-api/download",
        params={"url": "http://evil.example/steal", "name": "x.txt"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden_path"
