from __future__ import annotations

import json
import sys
import threading
import types
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

# TEST ONLY: loopback URLs bind ephemeral local servers; no external network
# access occurs.

import pytest

from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)

bootstrap_module = types.ModuleType("jiuwenswarm.agents.harness.team.bootstrap")
bootstrap_module.configure_agent_teams_home = lambda: None
sys.modules.setdefault(bootstrap_module.__name__, bootstrap_module)

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler, _format_ws_diagnostics


def test_format_ws_diagnostics_forwards_parts_and_fields():
    assert _format_ws_diagnostics({"client": "127.0.0.1"}, reason="peer closed") == (
        "client='127.0.0.1' reason='peer closed'"
    )


def test_raw_file_serves_persisted_session_image(tmp_path):
    agent_root = tmp_path / "agent"
    image_path = agent_root / "sessions" / "session-1" / "uploads" / "image.png"
    image_path.parent.mkdir(parents=True)
    image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
    image_path.write_bytes(image_bytes)

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = agent_root
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", f"/file-api/raw-file?path={quote(str(image_path))}")
        response = connection.getresponse()
        try:
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/png"
            assert response.read() == image_bytes
        finally:
            connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_raw_file_serves_an_image_in_the_selected_project_directory(tmp_path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    project_dir = tmp_path / "Videos"
    image_path = project_dir / "image.jpg"
    image_path.parent.mkdir()
    image_bytes = b"\xff\xd8\xffjpg-data"
    image_path.write_bytes(image_bytes)
    outside = tmp_path / "outside" / "secret.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"secret")
    (agent_root / "projects.json").write_text(
        json.dumps({"version": 1, "projects": [{"project_id": "proj_videos", "project_dir": str(project_dir)}]}),
        encoding="utf-8",
    )

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = agent_root
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", f"/file-api/raw-file?path={quote(str(image_path))}")
        response = connection.getresponse()
        try:
            assert response.status == 200
            assert response.read() == image_bytes
        finally:
            connection.close()
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", f"/file-api/raw-file?path={quote(str(outside))}")
        denied = connection.getresponse()
        try:
            assert denied.status == 403
            denied.read()
        finally:
            connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_raw_file_serves_an_organization_summary(tmp_path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    report = (
        tmp_path / ".organizations" / "gorg-1" / "sessions" / "session-1"
        / "organization-workspace" / "summary" / "report.txt"
    )
    report.parent.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = agent_root
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        organizations_root = tmp_path / ".organizations"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("HEAD", f"/file-api/raw-file?path={quote(str(report))}")
        response = connection.getresponse()
        try:
            assert response.status == 200
            response.read()
        finally:
            connection.close()
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", f"/file-api/raw-file?path={quote(str(outside))}")
        denied = connection.getresponse()
        try:
            assert denied.status == 403
            denied.read()
        finally:
            connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_agentos_download_is_proxied_to_web_channel(tmp_path):
    received_paths: list[str] = []

    class WebChannelHandler(BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            received_paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            del format, args

    web_channel = ThreadingHTTPServer(("127.0.0.1", 0), WebChannelHandler)
    web_channel_thread = threading.Thread(target=web_channel.serve_forever)
    web_channel_thread.start()

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path / "agent"
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = f"http://127.0.0.1:{web_channel.server_port}"
        ws_target = ""

    frontend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    frontend_thread = threading.Thread(target=frontend.serve_forever)
    frontend_thread.start()
    try:
        with patch("jiuwenswarm.channels.web.app_web._uses_agentos_routing", return_value=True):
            connection = HTTPConnection("127.0.0.1", frontend.server_port)
            connection.request("HEAD", "/file-api/download?token=signed-token&user_id=user-1")
            response = connection.getresponse()
            try:
                assert response.status == 200
            finally:
                response.read()
                connection.close()
        assert received_paths == ["/file-api/download?token=signed-token&user_id=user-1"]
    finally:
        frontend.shutdown()
        frontend_thread.join()
        frontend.server_close()
        web_channel.shutdown()
        web_channel_thread.join()
        web_channel.server_close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_verified_download_get_and_head_use_conventional_staged_file_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    source = tmp_path / "approved report.md"
    content = b"approved-content"
    source.write_bytes(content)
    owner = VerifiedDownloadAssetOwner(
        root=tmp_path / "assets",
        now_fn=lambda: 100.0,
        start_sweeper=False,
    )
    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=160.0,
    )
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    monkeypatch.setattr(
        "jiuwenswarm.common.file_download_token.time.time",
        lambda: 100.0,
    )
    monkeypatch.setattr(WebFileDownloadManager, "_instance", manager)
    token = manager.generate_verified_asset_token(
        asset,
        file_name=source.name,
        session_id="session-a",
    )

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(method, f"/file-api/download?token={quote(token)}")
        response = connection.getresponse()
        try:
            assert response.status == 200
            assert response.headers["Content-Length"] == str(len(content))
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Content-Disposition"] == (
                "attachment; filename*=UTF-8''approved%20report.md"
            )
            assert response.read() == (content if method == "GET" else b"")
        finally:
            connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_parse_single_byte_range_rejects_invalid_variants():
    from jiuwenswarm.channels.web.app_web import _parse_single_byte_range

    # size 10
    assert _parse_single_byte_range("bytes=0-9", 10) == (0, 9)
    assert _parse_single_byte_range("bytes=4-", 10) == (4, 9)
    assert _parse_single_byte_range("bytes=-4", 10) == (6, 9)
    assert _parse_single_byte_range("bytes=2-100", 10) == (2, 9)
    # invalid / unsupported forms
    assert _parse_single_byte_range("bytes=10-", 10) is None  # start >= size
    assert _parse_single_byte_range("bytes=5-2", 10) is None  # end < start
    assert _parse_single_byte_range("bytes=0-1,3-4", 10) is None  # multipart
    assert _parse_single_byte_range("bytes=", 10) is None
    assert _parse_single_byte_range("chars=0-", 10) is None
    assert _parse_single_byte_range("bytes=0-9", 0) is None  # empty file
    assert _parse_single_byte_range("bytes=-0", 10) is None  # suffix zero


@pytest.mark.parametrize(
    "range_header,expected_status,expected_body",
    [
        (None, 200, b"0123456789"),
        ("bytes=0-9", 206, b"0123456789"),  # exact full range is still a 206
        ("bytes=4-", 206, b"456789"),
        ("bytes=2-5", 206, b"2345"),
        ("bytes=-4", 206, b"6789"),
        ("bytes=12-", 416, b""),
        ("bytes=5-2", 416, b""),
        ("bytes=0-1,4-5", 416, b""),
    ],
)
def test_raw_file_answers_byte_range_requests(tmp_path, range_header, expected_status, expected_body):
    """Browsers can only seek /file-api/raw-file media (designer video preview
    timeline) when the endpoint honors Range requests (issue #6015)."""
    agent_root = tmp_path / "agent"
    video_path = agent_root / "sessions" / "session-1" / "uploads" / "video.mp4"
    video_path.parent.mkdir(parents=True)
    video_bytes = b"0123456789"
    video_path.write_bytes(video_bytes)

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = agent_root
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        headers = {"Range": range_header} if range_header else {}
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", f"/file-api/raw-file?path={quote(str(video_path))}", headers=headers)
        response = connection.getresponse()
        try:
            assert response.status == expected_status
            assert response.headers["Accept-Ranges"] == "bytes"
            assert response.headers["Content-Length"] == str(len(expected_body))
            if expected_status == 206:
                start = video_bytes.index(expected_body)
                assert response.headers["Content-Range"] == (
                    f"bytes {start}-{start + len(expected_body) - 1}/{len(video_bytes)}"
                )
            elif expected_status == 416:
                assert response.headers["Content-Range"] == f"bytes */{len(video_bytes)}"
            else:
                assert "Content-Range" not in response.headers
            assert response.read() == expected_body
        finally:
            connection.close()
        # HEAD with the same Range answers the same status without a body.
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "HEAD", f"/file-api/raw-file?path={quote(str(video_path))}", headers=headers
        )
        head = connection.getresponse()
        try:
            assert head.status == expected_status
            assert head.headers["Accept-Ranges"] == "bytes"
            assert head.read() == b""
        finally:
            connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
