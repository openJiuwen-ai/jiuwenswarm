"""Regression tests for the Web UI WebSocket tunnel handshake."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

_UPGRADE_RESPONSE = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Accept: test\r\n\r\n"
)


@contextmanager
def _slow_upstream(delay: float) -> Iterator[int]:
    """Accept one Upgrade and answer 101 only after ``delay`` seconds."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    finished = threading.Event()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            try:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                time.sleep(delay)
                conn.sendall(_UPGRADE_RESPONSE)
                finished.wait(5)
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        finished.set()
        listener.close()
        thread.join(timeout=5)


@contextmanager
def _serve_ws_proxy(
    upstream_port: int,
    directory: Path,
    handler_cls: type[_SpaStaticHandler] = _SpaStaticHandler,
) -> Iterator[int]:
    """Serve the Web UI reverse proxy with ``/ws`` pointed at the fake gateway."""

    class _TestProxyHandler(handler_cls):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

    _TestProxyHandler.ws_target = f"ws://127.0.0.1:{upstream_port}"
    handler = partial(_TestProxyHandler, directory=str(directory))
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


def _upgrade_status_line(proxy_port: int) -> str:
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=15) as client:
        client.sendall(
            (
                "GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{proxy_port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        return client.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")


def test_tunnel_waits_for_a_slow_gateway_upgrade(tmp_path: Path) -> None:
    # A gateway that needs 0.5s to accept used to hit the 0.25s connect-probe
    # timeout, so every browser reconnect failed with 502.
    with _slow_upstream(0.5) as upstream_port, _serve_ws_proxy(upstream_port, tmp_path) as proxy_port:
        assert " 101 " in _upgrade_status_line(proxy_port)


def test_tunnel_handshake_is_still_bounded(tmp_path: Path) -> None:
    class _ShortHandshakeHandler(_SpaStaticHandler):
        _WS_HANDSHAKE_TIMEOUT = 0.2

    with (
        _slow_upstream(1.0) as upstream_port,
        _serve_ws_proxy(upstream_port, tmp_path, _ShortHandshakeHandler) as proxy_port,
    ):
        assert " 502 " in _upgrade_status_line(proxy_port)
