"""Regression tests for the Web UI WebSocket tunnel handshake."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_tunnel_keeps_reverse_stream_alive_during_upload_backpressure(tmp_path: Path, monkeypatch) -> None:
    # Fill the upload socket while the gateway pauses its reads for longer than
    # the old 1s write deadline. Downstream data must still flow, and queued
    # upload bytes must survive the pause unchanged.
    original_connect = socket.create_connection
    upload = (b"\x82\x7e\x08\x00" + b"a" * 2048) * 2048
    reply = b"\x82\x05hello"
    close = b"\x88\x02\x03\xe8"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def small_send_buffer(address, *args, **kwargs):
        conn = original_connect(address, *args, **kwargs)
        if address[1] == port:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        return conn

    monkeypatch.setattr(socket, "create_connection", small_send_buffer)

    def receive_exact(conn, size):
        data = bytearray()
        while len(data) < size:
            part = conn.recv(min(65536, size - len(data)))
            assert part, "tunnel closed before forwarding all bytes"
            data.extend(part)
        return bytes(data)

    def gateway():
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(10)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            conn.sendall(_UPGRADE_RESPONSE)
            time.sleep(1.5)
            conn.sendall(reply)
            # Do not release upload backpressure until the client has received
            # the reverse stream and explicitly lets us resume.
            assert resume.wait(5)
            assert receive_exact(conn, len(upload)) == upload
            conn.sendall(close)

    resume = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            upstream = pool.submit(gateway)
            with _serve_ws_proxy(port, tmp_path) as proxy_port:
                with original_connect(("127.0.0.1", proxy_port), timeout=10) as client:
                    client.sendall(
                        b"GET /ws HTTP/1.1\r\nHost: localhost\r\n"
                        b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                    )
                    assert receive_exact(client, len(_UPGRADE_RESPONSE)) == _UPGRADE_RESPONSE
                    sending = pool.submit(client.sendall, upload)
                    try:
                        assert receive_exact(client, len(reply)) == reply
                    finally:
                        resume.set()
                    sending.result(timeout=10)
                    assert receive_exact(client, len(close)) == close
                    upstream.result(timeout=10)
    finally:
        resume.set()
        listener.close()
