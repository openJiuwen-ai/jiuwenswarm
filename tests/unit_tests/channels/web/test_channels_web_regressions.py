# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jiuwenswarm.channels.desktop.office_addins import certificate
from jiuwenswarm.channels.desktop.office_addins.certificate import TlsFiles
from jiuwenswarm.channels.desktop.office_addins.listener import TlsListener
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
import ast
import pytest


# --- from test_app_web_ws_tunnel.py ---

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

_PAYLOAD = bytes(range(256)) * 8192  # 2 MB: more than the socket buffers hold, and many TLS records

def _binary_frame(payload: bytes) -> bytes:
    return bytes([0x82, 127]) + len(payload).to_bytes(8, "big") + payload

def _read_exactly(read: Callable[[int], bytes], size: int, chunk: int, pause: float) -> bytes:
    received = bytearray()
    while len(received) < size:
        data = read(min(chunk, size - len(received)))
        if not data:
            break
        received.extend(data)
        time.sleep(pause)
    return bytes(received)

@contextmanager
def _streaming_upstream(frame: bytes, chunk: int, pause: float) -> Iterator[tuple[int, list[bytes]]]:
    """Answer the Upgrade with 101, send ``frame``, and collect ``frame``'s length of bytes from the client."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received: list[bytes] = []

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(15)
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            conn.sendall(_UPGRADE_RESPONSE)
            threading.Thread(target=conn.sendall, args=(frame,), daemon=True).start()
            received.append(_read_exactly(conn.recv, len(frame), chunk, pause))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1]), received
    finally:
        thread.join(timeout=20)
        listener.close()

@contextmanager
def _serve_tls_ws_proxy(upstream_port: int, directory: Path) -> Iterator[tuple[int, ssl.SSLContext]]:
    """The same handler on the desktop's HTTPS listener, as Office's task panes reach it."""

    class _TestProxyHandler(_SpaStaticHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

    _TestProxyHandler.ws_target = f"ws://127.0.0.1:{upstream_port}"
    files = TlsFiles(directory / "tls")
    certificate.generate(files, dt.datetime.now(dt.timezone.utc))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    listener = TlsListener(port, partial(_TestProxyHandler, directory=str(directory)))
    assert listener.serve(files)
    try:
        yield port, ssl.create_default_context(cafile=str(files.ca))
    finally:
        listener.close()

class _SplitRecordTlsClient:
    """A TLS client whose records reach the server a few kilobytes at a time, as over a real network."""

    def __init__(self, port: int, context: ssl.SSLContext) -> None:
        self._raw = socket.create_connection(("127.0.0.1", port), timeout=15)
        self._incoming, self._outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_hostname="localhost")
        self._call(self._tls.do_handshake)

    def sendall(self, data: bytes) -> None:
        self._call(lambda: self._tls.write(data))

    def recv(self, size: int) -> bytes:
        return self._call(lambda: self._tls.read(size))

    def close(self) -> None:
        self._raw.close()

    def _call(self, operation: Callable[[], Any]) -> Any:
        while True:
            try:
                result = operation()
            except ssl.SSLWantReadError:
                self._flush()
                data = self._raw.recv(65536)
                if not data:
                    raise ConnectionError("proxy closed the connection") from None
                self._incoming.write(data)
                continue
            self._flush()
            return result

    def _flush(self) -> None:
        data = self._outgoing.read()
        for start in range(0, len(data), 4000):
            self._raw.sendall(data[start:start + 4000])
            time.sleep(0.001)

@pytest.mark.parametrize("tls", [True, False], ids=["https", "http"])
@pytest.mark.parametrize("slow", [False, True], ids=["fast-readers", "slow-readers"])
def test_tunnel_carries_large_frames_both_ways(tmp_path: Path, tls: bool, slow: bool) -> None:
    chunk, pause = (16384, 0.005) if slow else (65536, 0.0)
    frame = _binary_frame(_PAYLOAD)
    with _streaming_upstream(frame, chunk, pause) as (upstream_port, upstream_received):
        with ExitStack() as stack:
            if tls:
                proxy_port, context = stack.enter_context(_serve_tls_ws_proxy(upstream_port, tmp_path))
                client = stack.enter_context(closing(_SplitRecordTlsClient(proxy_port, context)))
            else:
                proxy_port = stack.enter_context(_serve_ws_proxy(upstream_port, tmp_path))
                client = stack.enter_context(socket.create_connection(("127.0.0.1", proxy_port), timeout=15))
            client.sendall(
                "GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode("ascii")
            )
            head = b""
            while b"\r\n\r\n" not in head:
                head += client.recv(4096)
            head, _, early = head.partition(b"\r\n\r\n")
            assert b" 101 " in head

            downstream = early + _read_exactly(client.recv, len(frame) - len(early), chunk, pause)
            client.sendall(frame)

    assert len(downstream) == len(frame) and downstream == frame
    assert [len(data) for data in upstream_received] == [len(frame)] and upstream_received[0] == frame


# --- from test_subprocess_encoding_channels_web.py ---

ROOT = Path(__file__).resolve().parents[4]

CHANNELS_WEB_DIR = ROOT / "jiuwenswarm" / "channels" / "web"

SAFE_ERROR_HANDLERS = frozenset({"replace", "ignore", "backslashreplace"})

def _subprocess_run_targets(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Collect the names that refer to ``subprocess`` and to ``subprocess.run``.

    Handles ``import subprocess``, ``import subprocess as sp`` and
    ``from subprocess import run`` so a rename cannot hide a call site.
    """
    module_aliases = {"subprocess"}
    direct_run_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "subprocess":
                    module_aliases.add(item.asname or item.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for item in node.names:
                if item.name == "run":
                    direct_run_names.add(item.asname or item.name)
    return module_aliases, direct_run_names

def _iter_subprocess_run_calls(tree: ast.AST):
    module_aliases, direct_run_names = _subprocess_run_targets(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in direct_run_names:
            yield node
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ):
            yield node

def _keyword(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None

def _constant_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None

@pytest.mark.unit
def test_text_mode_picker_calls_declare_an_explicit_encoding() -> None:
    """Every ``subprocess.run(..., text=True)`` in channels/web pins a codec.

    The error handler is asserted as well as the codec: an explicit
    ``encoding`` alone still raises from subprocess' reader thread when the
    bytes do not match it, and that is exactly the failure this test exists to
    prevent.
    """
    offenders: list[str] = []
    for path in sorted(CHANNELS_WEB_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _iter_subprocess_run_calls(tree):
            text_kw = _keyword(call, "text")
            if not (isinstance(text_kw, ast.Constant) and text_kw.value is True):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if _keyword(call, "encoding") is None:
                offenders.append(f"{rel}:{call.lineno} (missing encoding=)")
                continue
            errors = _constant_str(_keyword(call, "errors"))
            if errors not in SAFE_ERROR_HANDLERS:
                offenders.append(f"{rel}:{call.lineno} (errors={errors!r})")

    assert not offenders, (
        "subprocess.run(..., text=True) must pin 'encoding=' plus a tolerant "
        "'errors=' (one of " + ", ".join(sorted(SAFE_ERROR_HANDLERS)) + "), so "
        "picker output is never decoded with a codec chosen by the "
        "interpreter's UTF-8 mode: " + ", ".join(offenders)
    )

