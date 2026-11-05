"""The Web UI server forwards Gateway file import and export for local clients (Gateway F_13)."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

UPLOAD_PATH = "/extension-uploads/" + "a" * 32 + "/theme.zip"
DOWNLOAD_PATH = "/extension-downloads/" + "b" * 32
HALF = 256 * 1024


@contextmanager
def _serve(handler) -> Iterator[int]:
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


def _upstream(seen: list[dict], first_bytes: threading.Event):
    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def do_PUT(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            first = self.rfile.read(HALF)
            first_bytes.set()
            rest = self.rfile.read(length - len(first))
            seen.append({"method": "PUT", "path": self.path, "body": first + rest})
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            seen.append({"method": "GET", "path": self.path})
            body = b"backup-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''backup.zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Upstream


@contextmanager
def _proxy(upstream_port: int, directory: Path, *, remote: bool = False) -> Iterator[int]:
    class _Proxy(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def setup(self) -> None:
            super().setup()
            if remote:
                self.client_address = ("203.0.113.9", 40000)

    _Proxy.api_target = f"http://127.0.0.1:{upstream_port}"
    with _serve(partial(_Proxy, directory=str(directory))) as port:
        yield port


def test_import_bodies_are_streamed_to_the_gateway(tmp_path):
    seen: list[dict] = []
    first_bytes = threading.Event()
    body = bytes(range(256)) * (2 * HALF // 256)
    with _serve(_upstream(seen, first_bytes)) as upstream, _proxy(upstream, tmp_path) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest("PUT", UPLOAD_PATH)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        connection.send(body[:HALF])
        # A buffering proxy would hold the first half until the whole body arrived.
        assert first_bytes.wait(5)
        connection.send(body[HALF:])
        response = connection.getresponse()
        response.read()
        connection.close()
    assert response.status == 204
    assert seen == [{"method": "PUT", "path": UPLOAD_PATH, "body": body}]


def test_downloads_are_forwarded_with_their_headers(tmp_path):
    seen: list[dict] = []
    with _serve(_upstream(seen, threading.Event())) as upstream, _proxy(upstream, tmp_path) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", DOWNLOAD_PATH)
        response = connection.getresponse()
        content = response.read()
        connection.close()
    assert response.status == 200 and content == b"backup-bytes"
    assert response.getheader("Content-Disposition") == "attachment; filename*=UTF-8''backup.zip"
    assert seen == [{"method": "GET", "path": DOWNLOAD_PATH}]


def test_remote_clients_cannot_import_or_export(tmp_path):
    seen: list[dict] = []
    with _serve(_upstream(seen, threading.Event())) as upstream, _proxy(upstream, tmp_path, remote=True) as port:
        for method, path, body in (("PUT", UPLOAD_PATH, b"1234"), ("GET", DOWNLOAD_PATH, None)):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body=body)
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 403, method
    assert seen == []
