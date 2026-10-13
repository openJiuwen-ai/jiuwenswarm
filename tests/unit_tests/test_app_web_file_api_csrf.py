# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-site POSTs to /file-api/* must be rejected via Fetch Metadata.

Every mutating /file-api/* route (upload, file-content, skill generation,
ws-debug-config, ...) previously accepted a request with no Origin, Fetch
Metadata, CSRF token, or desktop-token check at all: a plain HTML form on an
attacker-controlled page could submit a cross-site POST to the local
jiuwenswarm web server and trigger state-changing operations with the
victim's ambient localhost access (CWE-352).
"""

from __future__ import annotations

import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


@pytest.fixture
def file_api_server(tmp_path: Path):
    class Handler(_SpaStaticHandler):
        project_root = tmp_path
        workspace_root = tmp_path / "agent"
        agent_teams_root = tmp_path / "agent-teams"
        logs_root = tmp_path / "logs"
        auto_harness_root = tmp_path / "auto-harness"
        api_target = ""
        ws_target = ""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _post_file_api(port: int, path: str, sec_fetch_site: str | None):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Content-Type": "application/json", "Content-Length": "2"}
        if sec_fetch_site is not None:
            headers["Sec-Fetch-Site"] = sec_fetch_site
        connection.request("POST", path, body=b"{}", headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, body
    finally:
        connection.close()


def test_cross_site_post_is_rejected(file_api_server) -> None:
    port = file_api_server
    status, body = _post_file_api(
        port, "/file-api/skills/create-from-knowledge", "cross-site"
    )
    assert status == 403
    assert b"cross_site_request_forbidden" in body


def test_same_origin_post_reaches_the_route_dispatch(file_api_server) -> None:
    port = file_api_server
    # An unrecognized /file-api/* path falls through _handle_file_api_post to
    # a plain 404, so this exercises the guard without needing the real
    # skill/file backends wired up: what matters is that a same-origin
    # request is never short-circuited with the CSRF guard's 403.
    status, _body = _post_file_api(port, "/file-api/does-not-exist", "same-origin")
    assert status == 404


def test_missing_sec_fetch_site_header_reaches_the_route_dispatch(
    file_api_server,
) -> None:
    """Older browsers and non-browser clients that omit the header must not be blocked."""
    port = file_api_server
    status, _body = _post_file_api(port, "/file-api/does-not-exist", None)
    assert status == 404


def test_cross_site_post_is_rejected_for_every_mutating_file_api_route(
    file_api_server,
) -> None:
    """The guard applies to all of _handle_file_api_post, not just one route."""
    port = file_api_server
    for path in (
        "/file-api/skills/upload-temp",
        "/file-api/skills/import",
        "/file-api/rebuild-agent-data",
        "/file-api/upload",
        "/file-api/file-content",
        "/file-api/ws-debug-config",
    ):
        status, body = _post_file_api(port, path, "cross-site")
        assert status == 403, path
        assert b"cross_site_request_forbidden" in body, path
