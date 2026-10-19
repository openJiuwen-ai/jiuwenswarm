# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stdlib HTTP + WebSocket harness for live-browser subagent store checks."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def encode_ws_text(text: str) -> bytes:
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(length.to_bytes(8, "big"))
    return bytes(header) + payload


def decode_ws_text(sock: socket.socket) -> str | None:
    header = sock.recv(2)
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        ext = sock.recv(2)
        if len(ext) < 2:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = sock.recv(8)
        if len(ext) < 8:
            return None
        length = int.from_bytes(ext, "big")
    mask = sock.recv(4) if masked else b""
    if masked and len(mask) < 4:
        return None
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    if masked:
        data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    if opcode == 0x8:
        return None
    return data.decode("utf-8")


def accept_ws_key(key: str) -> str:
    digest = hashlib.sha1((key + _GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def three_child_ws_payloads(session_id: str) -> list[dict[str, Any]]:
    """Backend-shaped Web payloads for the three-child H sequence."""
    events: list[dict[str, Any]] = []
    children = ("sa-a", "sa-b", "sa-c")
    for index, child_id in enumerate(children):
        events.append(
            {
                "event_type": "chat.subtask_update",
                "session_id": session_id,
                "parent_session_id": session_id,
                "subagent_id": child_id,
                "display_name": f"Child {child_id}",
                "status": "running",
                "legacy_status": "running",
                "revision": 1,
                "created_at": 1000 + index,
                "updated_at": 1000 + index,
            }
        )
        events.append(
            {
                "event_type": "chat.subagent_activity",
                "session_id": session_id,
                "parent_session_id": session_id,
                "subagent_id": child_id,
                "activity_id": f"{child_id}-think",
                "task_id": f"task-{child_id}",
                "seq": 1,
                "kind": "thinking",
                "summary": f"{child_id} thinking",
                "at_ms": 1100 + index,
            }
        )
    events.append(
        {
            "event_type": "chat.subtask_update",
            "session_id": session_id,
            "parent_session_id": session_id,
            "subagent_id": "sa-a",
            "display_name": "Child sa-a",
            "status": "idle",
            "legacy_status": "completed",
            "turn_outcome": "completed",
            "can_send_input": True,
            "lifecycle": "live",
            "revision": 2,
            "created_at": 1000,
            "updated_at": 2000,
        }
    )
    events.append(
        {
            "event_type": "chat.subtask_update",
            "session_id": session_id,
            "parent_session_id": session_id,
            "subagent_id": "sa-c",
            "display_name": "Child sa-c",
            "status": "closed",
            "legacy_status": "error",
            "closed_reason": "failed",
            "revision": 2,
            "created_at": 1002,
            "updated_at": 2100,
        }
    )
    return events


class _HarnessHandler(SimpleHTTPRequestHandler):
    snapshot: dict[str, Any] | None = None
    snapshot_event = threading.Event()

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/snapshot":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400)
            return
        type(self).snapshot = payload
        type(self).snapshot_event.set()
        self.send_response(204)
        self.end_headers()


class LiveWsHarness:
    """Serve the frontend harness and push the three-child WS sequence."""

    def __init__(self, root: Path, session_id: str = "session-h") -> None:
        self.root = root
        self.session_id = session_id
        self.http: ThreadingHTTPServer | None = None
        self.ws_sock: socket.socket | None = None
        self._http_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.client_snapshot: dict[str, Any] | None = None

    @property
    def http_url(self) -> str:
        if self.http is None:
            raise RuntimeError("http server is not started")
        host, port = self.http.server_address
        return f"http://127.0.0.1:{port}/index.html?session={self.session_id}&ws=ws://127.0.0.1:{self.ws_port}"

    @property
    def ws_port(self) -> int:
        if self.ws_sock is None:
            raise RuntimeError("ws server is not started")
        return int(self.ws_sock.getsockname()[1])

    def start(self) -> None:
        _HarnessHandler.snapshot = None
        _HarnessHandler.snapshot_event.clear()
        self.http = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            lambda *args, **kwargs: _HarnessHandler(*args, directory=str(self.root), **kwargs),
        )
        self._http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self._http_thread.start()
        self.ws_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ws_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ws_sock.bind(("127.0.0.1", 0))
        self.ws_sock.listen(1)
        self._ws_thread = threading.Thread(target=self._serve_ws, daemon=True)
        self._ws_thread.start()

    def _serve_ws(self) -> None:
        if self.ws_sock is None:
            return
        conn, _addr = self.ws_sock.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            headers = {}
            for line in request.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            key = headers.get("sec-websocket-key")
            if not key:
                return
            accept = accept_ws_key(key)
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            conn.sendall(response.encode("ascii"))
            first = decode_ws_text(conn)
            if first is None:
                return
            self.ready.set()
            for payload in three_child_ws_payloads(self.session_id):
                conn.sendall(encode_ws_text(json.dumps(payload)))
            conn.sendall(encode_ws_text(json.dumps({"type": "done"})))
            snapshot_msg = decode_ws_text(conn)
            if snapshot_msg:
                try:
                    parsed = json.loads(snapshot_msg)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("type") == "snapshot":
                    snap = parsed.get("snapshot")
                    if isinstance(snap, dict):
                        self.client_snapshot = snap
            self.done.set()

    def wait_snapshot(self, timeout: float = 20.0) -> dict[str, Any]:
        _HarnessHandler.snapshot_event.wait(timeout)
        if isinstance(_HarnessHandler.snapshot, dict):
            return _HarnessHandler.snapshot
        self.done.wait(timeout)
        if isinstance(self.client_snapshot, dict):
            return self.client_snapshot
        raise TimeoutError("live WS harness did not receive a browser snapshot")

    def close(self) -> None:
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
        if self.ws_sock is not None:
            self.ws_sock.close()


__all__ = ["LiveWsHarness", "three_child_ws_payloads"]
