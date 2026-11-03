# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Identity uid/gid map for a sandbox user namespace.

bubblewrap's own map contains a single id, so ``setuid`` to any other id
fails. This map keeps every id in ``0 .. 65535`` equal to the same host id.
Checkout can then ``setuid``/``setgid`` to a resolved user such as ``app``.
"""

from __future__ import annotations

import json
import os
import select
import time

# Covers the usual allocated range, including nobody (65534).
IDENTITY_ID_MAP_COUNT = 65536


def identity_id_map_text() -> bytes:
    """One uid_map/gid_map line: inner id N is host id N."""
    return f"0 0 {IDENTITY_ID_MAP_COUNT}\n".encode()


def child_pid_from_bwrap_info(payload: bytes) -> int:
    """Read ``child-pid`` from bwrap ``--info-fd`` JSON."""
    message = json.loads(payload.decode())
    pid = int(message["child-pid"])
    if pid <= 0:
        raise ValueError(f"bwrap child-pid {pid} is not usable")
    return pid


def _write_proc_file(path: str, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY, 0o644)
    try:
        written = os.write(fd, payload)
    finally:
        os.close(fd)
    if written != len(payload):
        raise OSError(f"short write to {path}")


def _read_fd_until_eof(fd: int, timeout: float) -> bytes:
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("timed out waiting for bwrap --info-fd")
        ready, _, _ = select.select([fd], [], [], remain)
        if not ready:
            raise TimeoutError("timed out waiting for bwrap --info-fd")
        chunk = os.read(fd, 4096)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def install_identity_id_map(info_fd: int, block_fd: int, *, timeout: float = 5.0) -> None:
    """Write the identity map, then let bwrap continue.

    ``info_fd`` is the read end of ``--info-fd``. ``block_fd`` is the write
    end of ``--userns-block-fd``. bwrap waits there instead of installing
    its one-id map.
    """
    if os.geteuid() != 0:
        raise RuntimeError("identity uid map requires root")
    payload = _read_fd_until_eof(info_fd, timeout)
    pid = child_pid_from_bwrap_info(payload)
    proc_dir = f"/proc/{pid}"
    try:
        _write_proc_file(f"{proc_dir}/setgroups", b"deny\n")
    except FileNotFoundError:
        pass
    mapping = identity_id_map_text()
    _write_proc_file(f"{proc_dir}/uid_map", mapping)
    _write_proc_file(f"{proc_dir}/gid_map", mapping)
    os.write(block_fd, b"\n")
