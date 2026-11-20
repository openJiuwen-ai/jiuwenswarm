"""回归测试：WebSocket 隧道把浏览器短暂的不读取当作背压，而不是断连。

浏览器渲染大段历史时会短暂停止读取 socket（远程访问下尤其明显），此时
非阻塞 ``send`` 返回 ``EAGAIN``。隧道曾以 1 秒为界直接关闭连接，正在回放的
历史和 ``config.get`` 响应会一起丢失。这里用一个只读很慢的客户端复现该场景。
"""

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
_UPGRADE_REQUEST = (
    "GET /ws HTTP/1.1\r\n"
    "Host: {host}\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    "Sec-WebSocket-Version: 13\r\n\r\n"
)
# 需要大于代理侧内核发送缓冲的上限（Linux tcp_wmem 默认最大 4MB），
# 才能让代理的 write 真正进入“不可写”分支。
_PAYLOAD = b"x" * (8 * 1024 * 1024)


@contextmanager
def _flooding_upstream(payload: bytes) -> Iterator[int]:
    """接受一次 Upgrade，回 101 后一次性推送 ``payload``。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

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
                conn.sendall(_UPGRADE_RESPONSE)
                conn.sendall(payload)
                stop.wait(20)
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5)


@contextmanager
def _serve_ws_proxy(upstream_port: int, directory: Path) -> Iterator[int]:
    class _TestProxyHandler(_SpaStaticHandler):
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


def _open_tunnel(proxy_port: int, *, recv_buffer: int) -> socket.socket:
    """完成 /ws Upgrade，并限制客户端接收窗口以尽早触发写阻塞。"""
    client = socket.socket()
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recv_buffer)
    client.settimeout(30)
    client.connect(("127.0.0.1", proxy_port))
    client.sendall(
        _UPGRADE_REQUEST.format(host=f"127.0.0.1:{proxy_port}").encode("ascii")
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = client.recv(1)
        if not chunk:
            break
        head += chunk
    assert b" 101 " in head.split(b"\r\n", 1)[0]
    return client


def _read_exactly(client: socket.socket, size: int) -> bytes:
    received = bytearray()
    while len(received) < size:
        chunk = client.recv(65536)
        if not chunk:
            break
        received.extend(chunk)
    return bytes(received)


def test_slow_reader_does_not_close_tunnel(tmp_path: Path) -> None:
    with (
        _flooding_upstream(_PAYLOAD) as upstream_port,
        _serve_ws_proxy(upstream_port, tmp_path) as proxy_port,
    ):
        client = _open_tunnel(proxy_port, recv_buffer=16 * 1024)
        try:
            # 模拟浏览器忙着渲染历史：2.5 秒不读取。旧实现对端 1 秒不可写就
            # 关闭隧道，这里只能读到部分数据；修复后连接保持并补发完整数据。
            time.sleep(2.5)
            assert _read_exactly(client, len(_PAYLOAD)) == _PAYLOAD
        finally:
            client.close()
