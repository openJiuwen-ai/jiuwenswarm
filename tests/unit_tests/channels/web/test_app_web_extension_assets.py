# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise extension media requests through the built frontend HTTP server."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


@contextmanager
def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("filename,content_type,content", [
    ("avatar.svg", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("promotion.png", "image/png", b"\x89PNG\r\n\x1a\nimage"),
    ("missing.svg", "application/json", b'{"code":"NOT_FOUND"}'),
])
def test_extension_assets_forward_media_and_backend_errors_instead_of_spa_html(
    tmp_path, method, filename, content_type, content,
):
    requests = []
    path = f"/extension-assets/example.package/current-generation/{filename}?v=1"
    status = 404 if filename == "missing.svg" else 200

    class AssetHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            requests.append((self.command, self.path))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        do_HEAD = do_GET

        def log_message(self, format, *args):  # noqa: A002
            pass

    (tmp_path / "index.html").write_text("<html>SPA entry</html>", encoding="utf-8")
    with _serve(AssetHandler) as upstream_port:
        class FrontendHandler(_SpaStaticHandler):
            api_target = f"http://127.0.0.1:{upstream_port}"

            def log_message(self, format, *args):  # noqa: A002
                pass

        with _serve(partial(FrontendHandler, directory=str(tmp_path))) as frontend_port:
            connection = HTTPConnection("127.0.0.1", frontend_port, timeout=5)
            try:
                connection.request(method, path)
                response = connection.getresponse()
                assert response.status == status
                assert response.headers["Content-Type"] == content_type
                assert response.headers["Content-Length"] == str(len(content))
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                assert response.read() == (content if method == "GET" else b"")
            finally:
                connection.close()
    assert requests == [(method, path)]


def test_asset_prefix_does_not_capture_similarly_named_frontend_routes(tmp_path):
    class FrontendHandler(_SpaStaticHandler):
        api_target = "http://127.0.0.1:1"

        def log_message(self, format, *args):  # noqa: A002
            pass

    (tmp_path / "index.html").write_text("<html>SPA entry</html>", encoding="utf-8")
    with _serve(partial(FrontendHandler, directory=str(tmp_path))) as port:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/extension-assets-help")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b"<html>SPA entry</html>"
        finally:
            connection.close()
