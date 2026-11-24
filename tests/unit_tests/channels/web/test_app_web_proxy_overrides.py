"""The Web UI proxy honours every override its ``_proxy_http`` signature accepts.

The signature merges develop's target, path, login-surface and SSE options with release's
streaming; each option must take effect rather than be accepted and dropped.
"""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

FIRST_EVENT = b"data: one\n\n"
SECOND_EVENT = b"data: two\n\n"


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


def _upstream(seen: list[dict], *, events: threading.Event | None = None):
    """A backend that records each request; with ``events`` it answers with an event stream."""

    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def do_GET(self) -> None:  # noqa: N802
            seen.append({"path": self.path, "surface": self.headers.get_all("X-Jiuwen-Web-Surface") or []})
            if events is None:
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(FIRST_EVENT)
            self.wfile.flush()
            events.wait(5)
            self.wfile.write(SECOND_EVENT)

    return _Upstream


@contextmanager
def _proxy(api_port: int, directory: Path, **overrides) -> Iterator[int]:
    """A Web UI server whose every GET goes through ``_proxy_http(**overrides)``."""

    class _Proxy(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

        def do_GET(self) -> None:  # noqa: N802
            self._proxy_http(**overrides)

    _Proxy.api_target = f"http://127.0.0.1:{api_port}"
    with _serve(partial(_Proxy, directory=str(directory))) as port:
        yield port


def test_a_target_override_sends_the_request_to_that_backend(tmp_path):
    default_seen: list[dict] = []
    other_seen: list[dict] = []
    with _serve(_upstream(default_seen)) as default_port, _serve(_upstream(other_seen)) as other_port:
        with _proxy(default_port, tmp_path, target=f"http://127.0.0.1:{other_port}", path="/api/login") as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/idp/login")
            response = connection.getresponse()
            assert response.status == 200 and response.read() == b"ok"
            connection.close()
    assert default_seen == [], "the default backend is not used when a target is given"
    assert [item["path"] for item in other_seen] == ["/api/login"]


def test_the_login_surface_replaces_a_header_sent_by_the_browser(tmp_path):
    seen: list[dict] = []
    with _serve(_upstream(seen)) as upstream_port:
        with _proxy(upstream_port, tmp_path, login_surface="user") as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/idp/me", headers={"x-jiuwen-web-surface": "admin"})
            assert connection.getresponse().status == 200
            connection.close()
    assert seen[0]["surface"] == ["user"], "only the host's login surface reaches the backend"


def test_an_event_stream_is_forwarded_as_it_arrives_and_then_closed(tmp_path):
    seen: list[dict] = []
    release = threading.Event()
    try:
        with _serve(_upstream(seen, events=release)) as upstream_port:
            with _proxy(upstream_port, tmp_path, stream_sse=True) as port:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request("GET", "/api/events")
                response = connection.getresponse()
                assert response.getheader("Connection") == "close", "a stream without length ends with the connection"
                assert response.read(len(FIRST_EVENT)) == FIRST_EVENT, "the first event arrives before the stream ends"
                release.set()
                assert response.read() == SECOND_EVENT
                connection.close()
    finally:
        release.set()
