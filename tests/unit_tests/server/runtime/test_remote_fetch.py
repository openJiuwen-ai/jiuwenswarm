"""Security tests for the bounded file-import downloader."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from jiuwenswarm.server.runtime.attachments import remote_fetch


def _dns_public(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _dns_private(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]


def _dns_by_host(host, *_args, **_kwargs):
    address = "127.0.0.1" if host in {"127.0.0.1", "localhost"} else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


class _FakeResponse:
    def __init__(
        self,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        status: int = 200,
        *,
        fail_after_first_read: bool = False,
        clock: dict[str, float] | None = None,
        advance: float = 0.0,
    ) -> None:
        self._body = body
        self._offset = 0
        self.headers = headers or {}
        self.status = status
        self._reads = 0
        self._fail_after_first_read = fail_after_first_read
        self._clock = clock
        self._advance = advance

    def read(self, _size: int = -1) -> bytes:
        raise AssertionError("read() can outlive the deadline; use read1()")

    def read1(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._clock is not None and self._advance:
            self._clock["now"] += self._advance
        if self._fail_after_first_read and self._reads > 1:
            raise ConnectionResetError("connection reset")
        if self._offset >= len(self._body):
            return b""
        end = len(self._body) if size < 0 else self._offset + size
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

    def getheader(self, name: str):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class _FakeConnection:
    def __init__(self, response: _FakeResponse, sink: list) -> None:
        self._response = response
        self._sink = sink
        self.sock = None
        self.closed = False
        self.address = ""

    def request(self, method: str, url: str, headers: dict | None = None) -> None:
        self._sink.append(
            {"method": method, "url": url, "headers": headers, "address": self.address}
        )

    def getresponse(self) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        self.closed = True


def _patch_transport(monkeypatch, responses):
    """Patch DNS and the connection factory with one response per request."""
    monkeypatch.setattr(socket, "getaddrinfo", _dns_public)
    connections: list[tuple[str, _FakeConnection]] = []
    queue = list(responses)

    def factory(parsed, address, timeout, holder):
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        sink: list = []
        connection = _FakeConnection(response, sink)
        connection.address = address
        connections.append((address, connection))
        return connection

    monkeypatch.setattr(remote_fetch, "_build_connection", factory)
    return connections


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\system32\\config",
        "a/b/c.txt",
        "bad\x00name.txt",
        "con:file",
        ".",
        "..",
    ],
)
def test_sanitize_filename_keeps_only_a_safe_basename(raw: str) -> None:
    name = remote_fetch.sanitize_filename(raw)

    assert name not in {"", ".", ".."}
    assert "/" not in name
    assert "\\" not in name
    assert "\x00" not in name
    assert ":" not in name


def test_sanitize_filename_truncates_long_names() -> None:
    name = remote_fetch.sanitize_filename("a" * 500 + ".zip")

    assert len(name) <= remote_fetch._MAX_FILENAME_LENGTH
    assert name.endswith(".zip")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.txt",
        "https:///missing-host",
        "https://user:pass@example.com/a.txt",
        "https://example.com:not-a-port/a.txt",
    ],
)
def test_validate_import_url_rejects_unsafe_forms(url: str) -> None:
    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.validate_import_url(url)


def test_validate_import_url_rejects_private_addresses(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns_private)

    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.validate_import_url("http://internal.example/a.txt")


def test_validate_import_url_accepts_public_address(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns_public)

    parsed = remote_fetch.validate_import_url("http://example.com/a.txt")

    assert parsed.hostname == "example.com"


def test_validate_import_url_rejects_when_any_address_is_private(monkeypatch) -> None:
    def mixed_dns(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_dns)

    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.validate_import_url("http://example.com/a.txt")


def test_download_connects_to_the_validated_address(monkeypatch, tmp_path) -> None:
    connections = _patch_transport(monkeypatch, [_FakeResponse(b"payload")])

    target = remote_fetch.download_url_to_workspace(
        "http://example.com/a.txt",
        tmp_path,
        preferred_name="../../escape.txt",
    )

    assert target.parent == tmp_path.resolve()
    assert target.name == "escape.txt"
    assert target.read_bytes() == b"payload"
    assert connections[0][0] == "93.184.216.34"
    assert connections[0][1].closed is True


def test_download_follows_public_redirect(monkeypatch, tmp_path) -> None:
    connections = _patch_transport(
        monkeypatch,
        [
            _FakeResponse(headers={"Location": "http://example.com/b.txt"}, status=302),
            _FakeResponse(b"final"),
        ],
    )

    target = remote_fetch.download_url_to_workspace(
        "http://example.com/a.txt",
        tmp_path,
        preferred_name="a.txt",
    )

    assert len(connections) == 2
    assert target.read_bytes() == b"final"


def test_redirect_to_private_host_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns_by_host)
    connection = _FakeConnection(
        _FakeResponse(headers={"Location": "http://127.0.0.1/secret"}, status=302),
        [],
    )
    calls = {"count": 0}

    def factory(parsed, address, timeout, holder):
        calls["count"] += 1
        return connection

    monkeypatch.setattr(remote_fetch, "_build_connection", factory)

    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
        )

    assert calls["count"] == 1
    assert list(tmp_path.iterdir()) == []


def test_failed_download_keeps_the_original_file(monkeypatch, tmp_path) -> None:
    original = tmp_path / "report.pdf"
    original.write_bytes(b"original")
    _patch_transport(
        monkeypatch,
        [_FakeResponse(b"replacement", fail_after_first_read=True)],
    )

    with pytest.raises(RuntimeError, match="download failed"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/report.pdf",
            tmp_path,
            preferred_name="report.pdf",
            overwrite=True,
        )

    assert original.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["report.pdf"]


def test_successful_overwrite_replaces_the_original(monkeypatch, tmp_path) -> None:
    original = tmp_path / "report.pdf"
    original.write_bytes(b"original")
    _patch_transport(monkeypatch, [_FakeResponse(b"replacement")])

    target = remote_fetch.download_url_to_workspace(
        "http://example.com/report.pdf",
        tmp_path,
        preferred_name="report.pdf",
        overwrite=True,
    )

    assert target == original
    assert original.read_bytes() == b"replacement"


def test_non_overwrite_does_not_clobber(monkeypatch, tmp_path) -> None:
    original = tmp_path / "a.txt"
    original.write_bytes(b"original")
    _patch_transport(monkeypatch, [_FakeResponse(b"new")])

    target = remote_fetch.download_url_to_workspace(
        "http://example.com/a.txt",
        tmp_path,
        preferred_name="a.txt",
    )

    assert target.name == "a-1.txt"
    assert target.read_bytes() == b"new"
    assert original.read_bytes() == b"original"


def test_download_rejects_oversize_body_and_removes_partial(monkeypatch, tmp_path) -> None:
    _patch_transport(monkeypatch, [_FakeResponse(b"x" * 32)])

    with pytest.raises(RuntimeError, match="exceeds"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            max_bytes=16,
        )

    assert list(tmp_path.iterdir()) == []


def test_download_rejects_oversize_content_length(monkeypatch, tmp_path) -> None:
    _patch_transport(
        monkeypatch,
        [_FakeResponse(b"", {"Content-Length": "4096"})],
    )

    with pytest.raises(RuntimeError, match="too large"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            max_bytes=16,
        )

    assert list(tmp_path.iterdir()) == []


def test_incomplete_body_keeps_the_original_file(monkeypatch, tmp_path) -> None:
    original = tmp_path / "a.txt"
    original.write_bytes(b"original")
    _patch_transport(
        monkeypatch,
        [_FakeResponse(b"abc", {"Content-Length": "10"})],
    )

    with pytest.raises(RuntimeError, match="incomplete body"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            overwrite=True,
        )

    assert original.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_total_deadline_stops_a_slow_sender(monkeypatch, tmp_path) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(remote_fetch.time, "monotonic", lambda: clock["now"])
    _patch_transport(
        monkeypatch,
        [_FakeResponse(b"x" * 32, clock=clock, advance=2.0)],
    )

    with pytest.raises(RuntimeError, match="timed out"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            timeout=1.0,
        )

    assert list(tmp_path.iterdir()) == []


def test_https_connection_pins_address_and_keeps_sni(monkeypatch) -> None:
    calls: dict[str, object] = {}
    holder = remote_fetch._SocketHolder()

    class _FakeTLSSocket:
        def __init__(self) -> None:
            self.handshakes = 0
            self.closed = False

        def do_handshake(self) -> None:
            self.handshakes += 1
            calls["holder_at_handshake"] = holder.get()

        def close(self) -> None:
            self.closed = True

    tls_socket = _FakeTLSSocket()

    class _FakeContext:
        def wrap_socket(
            self,
            sock,
            server_hostname=None,
            do_handshake_on_connect=True,
        ):
            calls["sni"] = server_hostname
            calls["do_handshake_on_connect"] = do_handshake_on_connect
            return tls_socket

    connection = remote_fetch._PinnedHTTPSConnection(
        "example.com",
        pinned_address="93.184.216.34",
        socket_holder=holder,
        port=443,
        timeout=5.0,
        context=_FakeContext(),
    )

    def fake_create(address, timeout, source_address):
        calls["address"] = address
        return object()

    monkeypatch.setattr(connection, "_create_connection", fake_create)
    connection.connect()

    assert calls["address"] == ("93.184.216.34", 443)
    assert calls["sni"] == "example.com"
    assert calls["do_handshake_on_connect"] is False
    assert calls["holder_at_handshake"] is tls_socket
    assert tls_socket.handshakes == 1
    assert connection.sock is tls_socket


def test_connect_phase_is_covered(monkeypatch) -> None:
    class _SlowConnection:
        sock = None

        def request(self, *_args, **_kwargs):
            time.sleep(0.08)

        def getresponse(self):
            raise AssertionError("deadline must stop the request")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        remote_fetch,
        "_build_connection",
        lambda *_args, **_kwargs: _SlowConnection(),
    )
    parsed = remote_fetch.urlparse("http://example.com/a.txt")

    with pytest.raises(RuntimeError, match="timed out"):
        remote_fetch._request_once(
            parsed,
            ("93.184.216.34",),
            deadline=time.monotonic() + 0.02,
        )


def test_dns_lookup_is_bounded(monkeypatch, tmp_path) -> None:
    def slow_dns(*_args, **_kwargs):
        time.sleep(0.2)
        return _dns_public()

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    monkeypatch.setattr(remote_fetch, "DNS_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="dns resolution timed out"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            timeout=5.0,
        )

    assert list(tmp_path.iterdir()) == []


class _TrickleServer:
    """Tiny TCP server that sends a header, then one byte at a time."""

    def __init__(
        self,
        header: bytes,
        trickle: bytes,
        interval: float = 0.05,
        suffix: bytes = b"",
    ) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._header = header
        self._trickle = trickle
        self._interval = interval
        self._suffix = suffix
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._socket.settimeout(2.0)
            connection, _ = self._socket.accept()
        except OSError:
            return
        with connection:
            try:
                connection.recv(65536)
                connection.sendall(self._header)
                for byte in self._trickle:
                    if self._stop.wait(self._interval):
                        return
                    connection.sendall(bytes([byte]))
                if self._suffix:
                    connection.sendall(self._suffix)
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def test_slow_body_is_bounded_by_deadline(monkeypatch, tmp_path) -> None:
    server = _TrickleServer(
        b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n",
        b"x" * 1000,
    )
    monkeypatch.setattr(
        remote_fetch,
        "_resolve_public_addresses",
        lambda parsed, deadline=None: ("127.0.0.1",),
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            remote_fetch.download_url_to_workspace(
                f"http://example.com:{server.port}/a.txt",
                tmp_path,
                preferred_name="a.txt",
                timeout=0.4,
            )
    finally:
        elapsed = time.monotonic() - started
        server.close()

    assert elapsed < 2.0
    assert list(tmp_path.iterdir()) == []


def test_slow_headers_are_cut_by_watchdog(monkeypatch, tmp_path) -> None:
    server = _TrickleServer(b"HTTP/1.1 200 OK\r\nX-Slow: ", b"a" * 1000)
    monkeypatch.setattr(
        remote_fetch,
        "_resolve_public_addresses",
        lambda parsed, deadline=None: ("127.0.0.1",),
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            remote_fetch.download_url_to_workspace(
                f"http://example.com:{server.port}/a.txt",
                tmp_path,
                preferred_name="a.txt",
                timeout=0.4,
            )
    finally:
        elapsed = time.monotonic() - started
        server.close()

    assert elapsed < 2.0
    assert list(tmp_path.iterdir()) == []


def test_slow_chunked_body_with_connection_close(monkeypatch, tmp_path) -> None:
    """``getresponse`` closes connection.sock; the watchdog still reaches the fd."""
    original = tmp_path / "a.txt"
    original.write_bytes(b"original")
    server = _TrickleServer(
        b"HTTP/1.1 200 OK\r\n"
        b"Connection: close\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n",
        b"f" * 1000,
    )
    monkeypatch.setattr(
        remote_fetch,
        "_resolve_public_addresses",
        lambda parsed, deadline=None: ("127.0.0.1",),
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            remote_fetch.download_url_to_workspace(
                f"http://example.com:{server.port}/a.txt",
                tmp_path,
                preferred_name="a.txt",
                overwrite=True,
                timeout=0.4,
            )
    finally:
        elapsed = time.monotonic() - started
        server.close()

    assert elapsed < 2.0
    assert original.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_slow_chunked_payload_with_connection_close(monkeypatch, tmp_path) -> None:
    """A valid chunked response with a slowly sent payload still hits the deadline."""
    original = tmp_path / "a.txt"
    original.write_bytes(b"original")
    server = _TrickleServer(
        b"HTTP/1.1 200 OK\r\n"
        b"Connection: close\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"40\r\n",
        b"a" * 64,
        interval=0.05,
        suffix=b"\r\n0\r\n\r\n",
    )
    monkeypatch.setattr(
        remote_fetch,
        "_resolve_public_addresses",
        lambda parsed, deadline=None: ("127.0.0.1",),
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            remote_fetch.download_url_to_workspace(
                f"http://example.com:{server.port}/a.txt",
                tmp_path,
                preferred_name="a.txt",
                overwrite=True,
                timeout=0.3,
            )
    finally:
        elapsed = time.monotonic() - started
        server.close()

    assert elapsed < 2.0
    assert original.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]
