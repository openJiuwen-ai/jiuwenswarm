# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise extension media requests through the built frontend HTTP server.

The second half (Gateway F_08) checks Range requests, HEAD and that proxied bodies stream.
"""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


# ── Range, HEAD and streaming (Gateway F_08) ─────────────────────────────────

ASSET_PATH = "/extension-assets/test.pkg/generation/media.bin"
BODY = bytes(range(256)) * 4096  # 1 MiB


@contextmanager
def _serve_streaming(handler) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _streaming_upstream(seen: list[dict], release: threading.Event | None = None):
    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def _reply(self, with_body: bool) -> None:
            seen.append({"method": self.command, "path": self.path, "range": self.headers.get("Range")})
            body = BODY
            status = 200
            requested = self.headers.get("Range")
            if requested:
                start, end = (int(part) for part in requested.removeprefix("bytes=").split("-"))
                body = BODY[start:end + 1]
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads")
            self.end_headers()
            if not with_body:
                return
            half = len(body) // 2
            self.wfile.write(body[:half])
            self.wfile.flush()
            if release is not None:
                release.wait(10)
            self.wfile.write(body[half:])

        def do_GET(self) -> None:  # noqa: N802
            self._reply(True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._reply(False)

    return _Upstream


@contextmanager
def _streaming_proxy(upstream_port: int, directory: Path) -> Iterator[int]:
    class _Proxy(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

    _Proxy.api_target = f"http://127.0.0.1:{upstream_port}"
    with _serve_streaming(partial(_Proxy, directory=str(directory))) as port:
        yield port


def _streaming_request(port: int, method: str, headers: dict[str, str] | None = None, timeout: float = 15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, ASSET_PATH, headers=headers or {})
    return conn, conn.getresponse()


def test_extension_assets_are_forwarded_to_the_gateway(tmp_path):
    seen: list[dict] = []
    with _serve_streaming(_streaming_upstream(seen)) as upstream, _streaming_proxy(upstream, tmp_path) as port:
        conn, response = _streaming_request(port, "GET")
        body = response.read()
        conn.close()
    assert response.status == 200
    assert body == BODY
    assert response.getheader("Content-Security-Policy") == "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
    assert seen == [{"method": "GET", "path": ASSET_PATH, "range": None}]


def test_range_requests_reach_the_gateway_and_return_partial_content(tmp_path):
    seen: list[dict] = []
    with _serve_streaming(_streaming_upstream(seen)) as upstream, _streaming_proxy(upstream, tmp_path) as port:
        conn, response = _streaming_request(port, "GET", {"Range": "bytes=100-199"})
        body = response.read()
        conn.close()
    assert response.status == 206
    assert body == BODY[100:200]
    assert response.getheader("Content-Range") == f"bytes 100-199/{len(BODY)}"
    assert seen[0]["range"] == "bytes=100-199"


def test_head_returns_headers_without_a_body(tmp_path):
    seen: list[dict] = []
    with _serve_streaming(_streaming_upstream(seen)) as upstream, _streaming_proxy(upstream, tmp_path) as port:
        conn, response = _streaming_request(port, "HEAD")
        body = response.read()
        conn.close()
    assert response.status == 200
    assert body == b""
    assert response.getheader("Content-Length") == str(len(BODY))
    assert seen[0]["method"] == "HEAD"


def test_proxied_bodies_stream_before_the_upstream_finishes(tmp_path):
    seen: list[dict] = []
    release = threading.Event()
    with _serve_streaming(_streaming_upstream(seen, release)) as upstream, _streaming_proxy(upstream, tmp_path) as port:
        # A buffering proxy would hold the headers until the upstream finished,
        # which only happens after this client has read the first half.
        conn, response = _streaming_request(port, "GET", timeout=5)
        first = response.read(len(BODY) // 2)
        release.set()
        rest = response.read()
        conn.close()
    assert first == BODY[:len(BODY) // 2]
    assert first + rest == BODY
