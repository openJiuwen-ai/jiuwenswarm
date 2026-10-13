from __future__ import annotations

import json
import sys
import threading
import types
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

bootstrap_module = types.ModuleType("jiuwenswarm.agents.harness.team.bootstrap")
bootstrap_module.configure_agent_teams_home = lambda: None
sys.modules.setdefault(bootstrap_module.__name__, bootstrap_module)

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


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


def test_raw_file_rejects_file_over_configured_limit(tmp_path, monkeypatch):
    file_path = tmp_path / "large.bin"
    file_path.write_bytes(b"12345")
    monkeypatch.setattr(app_web, "_web_file_limit_bytes", lambda: 4)

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path
        agent_teams_root = tmp_path
        logs_root = tmp_path
        auto_harness_root = tmp_path
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/file-api/raw-file?path=large.bin")
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read())["code"] == "WEB_FILE_TOO_LARGE"
        connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_file_content_rejects_oversized_write_without_changing_file(tmp_path, monkeypatch):
    file_path = tmp_path / "note.md"
    file_path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(app_web, "_web_file_limit_bytes", lambda: 4)

    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path
        agent_teams_root = tmp_path
        logs_root = tmp_path
        auto_harness_root = tmp_path
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        body = json.dumps({"path": "note.md", "content": "12345"}).encode()
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/file-api/file-content",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        assert file_path.read_text(encoding="utf-8") == "old"
        connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _handler(tmp_path):
    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    return Handler


def test_raw_file_rejects_symlink(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink is not permitted in this environment")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(tmp_path))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/file-api/raw-file?path=link.txt")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 403
        assert json.loads(body)["error"] == "symlink_not_allowed"
        assert b"secret" not in body
        connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_file_content_missing_file_does_not_leak_absolute_path(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(tmp_path))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/file-api/file-content?path=missing.txt")
        response = connection.getresponse()
        body = response.read()
        payload = json.loads(body)
        assert response.status == 404
        assert payload == {"error": "file_not_found"}
        assert str(tmp_path) not in body.decode("utf-8")
        connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
