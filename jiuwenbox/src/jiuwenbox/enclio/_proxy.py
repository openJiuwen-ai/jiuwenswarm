# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One-run HTTP, CONNECT and SOCKS5 proxy using an isolated listener FD."""

import errno
import ipaddress
import os
import re
import select
import signal
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from ._address import endpoint, public_ip
from ._filters import install_proxy_filter
from ._linux import (
    PROXY_PORT, PR_SET_DUMPABLE, close_extra, parent_death, prctl, receive_fd,
)

_MAX_CONNECTIONS = 32
_MAX_HEAD = 8192
_MAX_FIELDS = 128
_MAX_BODY = 64 * 1024 * 1024
_TOKEN = re.compile(rb"[!#$%&'*+.^_\x60|~0-9A-Za-z-]+")
_AUTHORITY = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::([0-9]{1,5}))?", re.ASCII)
_HOP = {
    b"connection", b"proxy-connection", b"proxy-authorization", b"proxy-authenticate",
    b"keep-alive", b"te", b"trailer", b"upgrade",
}


@dataclass(frozen=True)
class _Request:
    destination: tuple[str, str, int]
    headers: bytes
    content_length: int
    chunked: bool


def _authority(value: str, default_port: int | None) -> tuple[str, str, int]:
    match = _AUTHORITY.fullmatch(value)
    if not match:
        raise ValueError("invalid_authority")
    name, port = match.groups()
    number = int(port) if port is not None else default_port
    if name.startswith("["):
        return endpoint("ip", name[1:-1], number)
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return endpoint("dns", name, number)
    return endpoint("ip", name, number)


def _parse_http(head: bytes) -> _Request:
    rows = head.split(b"\r\n")
    method, uri, version = rows[0].split(b" ")
    if not _TOKEN.fullmatch(method) or version not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise ValueError("invalid_request_line")
    if len(rows) - 1 > _MAX_FIELDS or any(ch <= 32 or ch >= 127 for ch in uri):
        raise ValueError("invalid_request_line")
    headers = {}
    for row in rows[1:]:
        key, sep, value = row.partition(b":")
        key = key.lower()
        value = value.strip(b" \t")
        if not sep or not _TOKEN.fullmatch(key) or key in headers:
            raise ValueError("invalid_header")
        if any((ch < 32 and ch != 9) or ch == 127 for ch in value):
            raise ValueError("invalid_header")
        headers[key] = value

    if method == b"CONNECT":
        destination = _authority(uri.decode("ascii"), None)
        path = b""
        default_port = None
    else:
        parsed = urlsplit(uri.decode("ascii"))
        if not uri.lower().startswith(b"http://") or parsed.scheme != "http" or "#" in parsed.geturl():
            raise ValueError("absolute_http_required")
        if b"#" in uri or "\\" in parsed.netloc:
            raise ValueError("invalid_url")
        destination = _authority(parsed.netloc, 80)
        path = (parsed.path or "/").encode("ascii")
        if parsed.query:
            path += b"?" + parsed.query.encode("ascii")
        default_port = 80

    host = headers.get(b"host")
    if host is None or _authority(host.decode("ascii"), default_port) != destination:
        raise ValueError("host_mismatch")
    length = headers.get(b"content-length")
    encoding = headers.get(b"transfer-encoding")
    if length is not None:
        if not re.fullmatch(rb"[0-9]{1,9}", length) or int(length) > _MAX_BODY:
            raise ValueError("invalid_body")
    if encoding is not None and (encoding.lower() != b"chunked" or length is not None):
        raise ValueError("invalid_body")
    if method == b"CONNECT" and (length is not None or encoding is not None):
        raise ValueError("invalid_body")
    nominated = {name.strip().lower() for name in headers.get(b"connection", b"").split(b",")} - {b""}
    if (nominated & {b"host", b"content-length", b"transfer-encoding", b"expect"}
            or any(not _TOKEN.fullmatch(name) for name in nominated)):
        raise ValueError("invalid_connection_header")
    if method == b"CONNECT":
        return _Request(destination, b"", 0, False)

    forwarded = [
        key + b": " + value for key, value in headers.items()
        if key not in _HOP and key not in nominated
    ]
    head = b"\r\n".join([b" ".join((method, path, b"HTTP/1.1")), *forwarded, b"Connection: close", b"", b""])
    return _Request(destination, head, int(length or b"0"), encoding is not None)


class ProxyFlow:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self.last = time.monotonic()

    def wait(self, readers: list, writers: list, phase: float) -> tuple[list, list]:
        while True:
            timeout = min(self.deadline, self.last + 30, phase) - time.monotonic()
            if timeout <= 0:
                raise TimeoutError
            ready_read, ready_write, _ = select.select(readers, writers, [], timeout)
            if ready_read or ready_write:
                return ready_read, ready_write
            # The upload thread can refresh last while the response thread is
            # in select. Recheck activity without extending absolute deadlines.

    def read(self, source: socket.socket, size: int, phase: float) -> bytes:
        self.wait([source], [], phase)
        data = source.recv(size, socket.MSG_DONTWAIT)
        self.last = time.monotonic()
        return data

    def send(self, destination: socket.socket, data: bytes) -> None:
        view = memoryview(data)
        while view:
            self.wait([], [destination], self.deadline)
            view = view[destination.send(view, socket.MSG_DONTWAIT):]
            self.last = time.monotonic()


class _Input:
    def __init__(self, source: socket.socket, flow: ProxyFlow):
        self.source = source
        self.flow = flow
        self.buffer = bytearray()

    def exact(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            data = self.flow.read(self.source, size - len(self.buffer), deadline)
            if not data:
                raise EOFError
            self.buffer.extend(data)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def until(self, boundary: bytes, limit: int, deadline: float) -> bytes:
        while True:
            end = self.buffer.find(boundary)
            if end >= 0 and end + len(boundary) <= limit:
                return self.exact(end + len(boundary), deadline)[:-len(boundary)]
            if len(self.buffer) >= limit:
                raise ValueError("request_too_large")
            data = self.flow.read(self.source, limit - len(self.buffer), deadline)
            if not data:
                raise EOFError
            self.buffer.extend(data)


def connect_endpoint(target: tuple[str, str, int], rules: frozenset, deadline: float) -> socket.socket:
    # 检查目标地址是否符合网络策略，检查域名解析结果符合公网地址规则，建立TCP连接
    if target not in rules:
        raise PermissionError("endpoint_denied")
    kind, name, port = target
    if kind == "ip":
        family = socket.AF_INET6 if ":" in name else socket.AF_INET
        addresses = [(family, (name, port, 0, 0) if family == socket.AF_INET6 else (name, port))]
    else:
        previous, _ = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - started))
        try:
            results = socket.getaddrinfo(
                name + ".", port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP,
            )
        finally:
            # 恢复原定时器，并扣除DNS解析已消耗的时间
            remaining = max(0.001, previous - (time.monotonic() - started)) if previous else 0
            signal.setitimer(signal.ITIMER_REAL, remaining)
        if not 0 < len(results) <= 128:
            raise ValueError("dns_budget")
        addresses = []
        for family, _, _, _, address in results:
            if family not in (socket.AF_INET, socket.AF_INET6) or (family == socket.AF_INET6 and address[3]):
                raise ValueError("invalid_dns_address")
            numeric = public_ip(address[0])
            addresses.append((family, (numeric, port, 0, 0) if family == socket.AF_INET6 else (numeric, port)))
    for family, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # CPython's settimeout can issue FIONBIO. The proxy filter blocks
        # ioctl, so request a nonblocking socket at creation.
        remote = socket.socket(family, socket.SOCK_STREAM | socket.SOCK_NONBLOCK, socket.IPPROTO_TCP)
        try:
            error = remote.connect_ex(address)
            if error == errno.EINPROGRESS and select.select([], [remote], [], min(remaining, 10))[1]:
                error = remote.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if not error:
                return remote
        except OSError:
            pass
        remote.close()
    raise OSError("connect_failed")


def _tunnel(flow: ProxyFlow, left: socket.socket, right: socket.socket, pending: bytes) -> None:
    peers = {left: right, right: left}
    buffers = {left: bytearray(), right: bytearray(pending)}

    def buffer_for(peer: socket.socket) -> bytearray:
        data = buffers.get(peer)
        if data is None:
            raise ValueError("unknown_tunnel_buffer")
        return data

    def peer_for(source: socket.socket) -> socket.socket:
        peer = peers.get(source)
        if peer is None:
            raise ValueError("unknown_tunnel_peer")
        return peer

    readers = set(peers)
    shutdown_pending = set()
    while readers or any(buffers.values()) or shutdown_pending:
        for peer in tuple(shutdown_pending):
            if not buffer_for(peer):
                peer.shutdown(socket.SHUT_WR)
                shutdown_pending.remove(peer)
        readable = [source for source in readers if not buffer_for(peer_for(source))]
        writable = [peer for peer, data in buffers.items() if data]
        if not readable and not writable:
            break
        ready_read, ready_write = flow.wait(readable, writable, flow.deadline)
        for source in ready_read:
            destination = peer_for(source)
            buffer = buffer_for(destination)
            payload = source.recv(65536, socket.MSG_DONTWAIT)
            if payload:
                buffer.extend(payload)
            else:
                readers.remove(source)
                shutdown_pending.add(destination)
            flow.last = time.monotonic()
        for peer in ready_write:
            buffer = buffer_for(peer)
            sent = peer.send(buffer, socket.MSG_DONTWAIT)
            del buffer[:sent]
            flow.last = time.monotonic()


def _upload(source: _Input, remote: socket.socket, request: _Request) -> None:
    def forward(size: int) -> None:
        while size:
            block = source.exact(min(size, 65536), source.flow.deadline)
            source.flow.send(remote, block)
            size -= len(block)

    try:
        if not request.chunked:
            forward(request.content_length)
            return
        total = 0
        chunks = 0
        while True:
            line = source.until(b"\r\n", 128, source.flow.deadline)
            if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}", line):
                raise ValueError("invalid_chunk_size")
            size = int(line, 16)
            total += size
            chunks += 1
            if total > _MAX_BODY or chunks > 65536:
                raise ValueError("body_budget")
            if not size:
                if source.exact(2, source.flow.deadline) != b"\r\n":
                    raise ValueError("trailers_unsupported")
                source.flow.send(remote, b"0\r\n\r\n")
                return
            source.flow.send(remote, f"{size:x}\r\n".encode("ascii"))
            forward(size)
            if source.exact(2, source.flow.deadline) != b"\r\n":
                raise ValueError("invalid_chunk_end")
            source.flow.send(remote, b"\r\n")
    except (ValueError, OSError, EOFError):
        # An incomplete body cannot become a valid next upstream request.
        try:
            remote.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _socks(source: _Input, first: bytes, head_deadline: float) -> tuple[str, str, int]:
    if first != b"\x05":
        raise ValueError("socks_version")
    count = source.exact(1, head_deadline)[0]
    methods = source.exact(count, head_deadline)
    if 0 not in methods:
        source.flow.send(source.source, b"\x05\xff")
        raise EOFError
    source.flow.send(source.source, b"\x05\x00")
    version, command, reserved, typ = source.exact(4, head_deadline)
    if (version, command, reserved) != (5, 1, 0):
        raise ValueError("socks_connect_required")
    if typ == 3:
        size = source.exact(1, head_deadline)[0]
        name = source.exact(size, head_deadline).decode("ascii")
    elif typ in (1, 4):
        name = str(ipaddress.ip_address(source.exact(4 if typ == 1 else 16, head_deadline)))
    else:
        raise ValueError("socks_address_type")
    port = int.from_bytes(source.exact(2, head_deadline), "big")
    return endpoint("dns" if typ == 3 else "ip", name, port)


def serve_client(client: socket.socket, rules: frozenset, run_deadline: float) -> None:
    # 负责建链和转发
    start = time.monotonic()
    flow = ProxyFlow(min(run_deadline, start + 300))
    source = _Input(client, flow)
    head_deadline = min(flow.deadline, start + 10)
    first = source.exact(1, head_deadline)
    socks = first == b"\x05"
    committed = False
    try:
        if socks:
            destination = _socks(source, first, head_deadline)
            request = None
        else:
            source.buffer[:0] = first
            request = _parse_http(source.until(b"\r\n\r\n", _MAX_HEAD, head_deadline))
            destination = request.destination
        with connect_endpoint(destination, rules, min(flow.deadline, time.monotonic() + 20)) as remote:
            committed = True
            if socks:
                flow.send(client, b"\x05\x00\x00\x01" + bytes(6))
            elif not request.headers:
                flow.send(client, b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if socks or not request.headers:
                _tunnel(flow, client, remote, bytes(source.buffer))
                return
            flow.send(remote, request.headers)
            # Receiving the upstream response concurrently allows 100 Continue
            # and final responses sent before the upload is complete.
            if request.content_length or request.chunked:
                worker = threading.Thread(target=_upload, args=(source, remote, request), daemon=True)
                worker.start()
            while True:
                data = flow.read(remote, 65536, flow.deadline)
                if not data:
                    break
                flow.send(client, data)
    except (ValueError, OSError, EOFError):
        if committed:
            return
        if socks:
            flow.send(client, b"\x05\x02\x00\x01" + bytes(6))
        else:
            flow.send(client, b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


def serve(channel_fd: int, lifetime: int, payload: dict) -> None:
    close_extra(channel_fd, lifetime)
    prctl(PR_SET_DUMPABLE, 0)
    limit = payload["startup"]
    deadline = payload["deadline"] or float("inf")
    listen_message = b"EL1/" + payload["id"].encode("ascii")
    rules = frozenset(tuple(row) for row in payload["endpoints"])
    with socket.socket(fileno=channel_fd) as channel:
        channel.settimeout(limit)
        fd = receive_fd(channel, listen_message)
        with socket.socket(fileno=fd) as listener:
            if listener.family != socket.AF_INET or listener.type != socket.SOCK_STREAM:
                raise ValueError("invalid_listener")
            if (listener.getsockname() != ("127.0.0.1", PROXY_PORT)
                    or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
                raise ValueError("invalid_listener")
            listener.setblocking(False)
            install_proxy_filter()
            channel.send(b"A")
            channel.close()
            _accept(listener, rules, deadline)


def _accept(listener: socket.socket, rules: frozenset, deadline: float) -> None:
    workers: set[int] = set()
    parent = os.getpid()
    while time.monotonic() < deadline:
        for pid in tuple(workers):
            if os.waitpid(pid, os.WNOHANG)[0]:
                workers.remove(pid)
        if not select.select([listener] if len(workers) < _MAX_CONNECTIONS else [], [], [], 0.05)[0]:
            continue
        client, _ = listener.accept()
        with client:
            try:
                pid = os.fork()
            except BlockingIOError:
                continue
            expiry = min(deadline, time.monotonic() + 300)
            if pid:
                workers.add(pid)
                continue
            parent_death(parent)
            listener.close()
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            # Shorten this to the DNS stage inside connect_endpoint when doing NSS.
            signal.setitimer(signal.ITIMER_REAL, max(0.001, expiry - time.monotonic()))
            try:
                serve_client(client, rules, deadline)
            except (ValueError, OSError, EOFError, RuntimeError):
                pass
            getattr(os, "_exit")(0)
