# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""IPC protocol shared between box-server and the in-sandbox daemon.

The protocol is intentionally tiny: every message is a single 4-byte
big-endian length prefix followed by exactly that many bytes of payload.
JSON objects describe the request/response semantics, while raw bytes
(stdin contents, captured stdout/stderr) ride alongside as additional
length-prefixed frames so we can carry binary payloads safely.

Stays standard-library only so the daemon (loaded with ``python3 -S``
inside the sandbox) does not have to drag in the rest of jiuwenbox to
participate in IPC.
"""

from __future__ import annotations

import socket
import struct
from typing import Any

# Names match between server and daemon; do not rename without bumping the
# protocol version below. Both the daemon script and the Landlock launcher
# live under ``/jiuwenbox`` (a fresh tmpfs created per sandbox by the
# runtime). The directory is reserved by ``PolicyEngine`` - user policies
# cannot reference any path under it - so collisions with
# ``read_only`` / ``read_write`` / ``bind_mounts`` / ``device`` entries
# are rejected at policy-validation time, and ``/jiuwenbox`` is also kept
# *outside* the Landlock allowlist so user code spawned by the daemon
# cannot read either script after Landlock applies.
SANDBOX_RESERVED_DIR = "/jiuwenbox"
SANDBOX_DAEMON_SANDBOX_PATH = f"{SANDBOX_RESERVED_DIR}/sandbox-daemon.py"
SANDBOX_CONTROL_SOCKET_NAME = "control.sock"
SANDBOX_LAUNCHER_PATH = f"{SANDBOX_RESERVED_DIR}/landlock-launcher.py"

# Box-server binds the listener Unix socket on the host filesystem and
# passes the listener fd into the sandbox via
# ``subprocess.Popen(pass_fds=...)``. Bubblewrap's user command path never
# closes arbitrary inherited fds, so the listener flows untouched through
# bwrap → launcher → daemon. The daemon recovers the fd number from this
# environment variable and runs ``accept`` against it directly. This
# keeps the IPC channel entirely outside the sandbox view: there is no
# path inside the sandbox that points at the control socket, so user
# code spawned by the daemon cannot reach it (and the daemon spawns user
# children with ``close_fds=True`` so the inherited fd is not exposed
# either).
LISTENER_FD_ENV = "JIUWENBOX_CONTROL_LISTENER_FD"

# Daemon argv vector. ``-S`` shaves the ``import site`` cost so the daemon
# starts faster; the daemon is stdlib-only so ``site`` is unnecessary.
SANDBOX_DAEMON_COMMAND = ["python3", "-S", SANDBOX_DAEMON_SANDBOX_PATH]

# Outgoing request types accepted by the daemon.
REQUEST_TYPE_EXEC = "exec"
REQUEST_TYPE_SHUTDOWN = "shutdown"
# File-ops fast paths. Box-server uses these instead of spawning bash/
# python helpers via ``REQUEST_TYPE_EXEC`` so upload/download/list pay no
# python cold-start or fork+exec cost. The daemon performs the operation
# directly in its own process, so namespaces, mount layout, the policy
# uid/gid, seccomp and Landlock all apply identically to what a child
# would see.
REQUEST_TYPE_WRITE_FILE = "write_file"
REQUEST_TYPE_READ_FILE = "read_file"
REQUEST_TYPE_LIST_DIR = "list_dir"
REQUEST_TYPE_EXEC_BACKGROUND = "exec_background"
REQUEST_TYPE_BG_STATUS = "bg_status"
REQUEST_TYPE_BG_KILL = "bg_kill"

PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 1 * 1024 * 1024          # 1 MiB JSON header upper bound
MAX_STDIN_BYTES = 64 * 1024 * 1024          # 64 MiB stdin upper bound
MAX_STDOUT_BYTES = 64 * 1024 * 1024         # 64 MiB stdout upper bound
MAX_STDERR_BYTES = 64 * 1024 * 1024         # 64 MiB stderr upper bound
MAX_FILE_BYTES = 256 * 1024 * 1024          # 256 MiB read/write upper bound

ACCEPT_BACKLOG = 128


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``sock`` or raise ``ConnectionError``."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"socket closed after {len(buf)}/{n} bytes",
            )
        buf.extend(chunk)
    return bytes(buf)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    """Send a single length-prefixed frame on ``sock``."""
    if len(payload) > 0xFFFFFFFF:
        raise ValueError(f"frame size {len(payload)} exceeds 4 GiB")
    sock.sendall(struct.pack(">I", len(payload)))
    if payload:
        sock.sendall(payload)


def recv_frame(sock: socket.socket, max_size: int) -> bytes:
    """Receive a single length-prefixed frame, enforcing ``max_size``."""
    header = recv_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size > max_size:
        raise ValueError(
            f"incoming frame size {size} exceeds limit {max_size}",
        )
    return recv_exact(sock, size)


def encode_request(
    *,
    request_type: str,
    payload: dict[str, Any] | None = None,
) -> bytes:
    import json
    body: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": request_type,
    }
    if payload:
        body.update(payload)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")
