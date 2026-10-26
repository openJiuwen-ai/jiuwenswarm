# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Long-running in-sandbox daemon that handles ``exec`` requests.

The previous implementation was a placeholder that just blocked on
``signal.pause`` so the sandbox lifecycle stayed alive. To make exec
calls cheap, the daemon now hosts a Unix-socket IPC server inside the
sandbox: box-server connects to the socket and sends ``exec`` requests
that the daemon services by ``fork+exec``-ing the user command. Because
the daemon already has bubblewrap's namespaces, mounts, seccomp, and
Landlock applied, every spawned child inherits the same isolation - the
expensive ``bwrap`` setup happens once per sandbox lifecycle instead of
once per exec.

How the daemon is started
-------------------------
Inside the sandbox the Landlock launcher (see ``landlock_launcher.py``)
reads this script's source from ``/jiuwenbox`` *before* applying
Landlock and then runs the daemon code with ``compile``/``exec`` in the
launcher's own Python process. There is **no** second ``execve`` after
Landlock is locked in, which is what allows the ``/jiuwenbox`` directory
to be omitted from the Landlock allowlist: nothing in the sandbox needs
to read the daemon script from disk after Landlock applies. From the
kernel's point of view the daemon and the launcher are the same Linux
process (PID 1 of the sandbox PID namespace). The directory is reserved
by ``PolicyEngine`` so user policies cannot reference it; see
``_RESERVED_SANDBOX_PATHS`` in ``jiuwenbox/server/policy_engine.py``.

The control socket itself lives on the **host** filesystem; box-server
``bind()``s and ``listen()``s before spawning bubblewrap, then passes
the listener file descriptor into the sandbox via
``subprocess.Popen(pass_fds=...)``. Bubblewrap's user command path
never closes arbitrary inherited fds (only its own monitor/PID-1 paths
do), so the listener fd flows naturally through the bwrap → launcher
chain. The daemon recovers the fd from ``JIUWENBOX_CONTROL_LISTENER_FD``
and ``accept()``s against it. This keeps the IPC channel entirely
outside any sandbox-visible path, so user code spawned by the daemon
cannot reach (or delete) the listener.

Important security notes:

* Sandboxed payloads share the daemon's UID (typically ``nobody``).  The
  kernel's PID-1 signal protections are not sufficient on every platform,
  so seccomp additionally blocks ``kill``/``tkill``/``tgkill`` targeting
  infrastructure PIDs (1/2, process group, or broadcast).  Each user exec
  also runs in its own session so ``kill(0)`` cannot reach the daemon.

* Box-server shuts the daemon down via the ``shutdown`` IPC command
  (graceful) or ``SIGKILL`` from outside the namespace (forced).

* Children are spawned via :func:`subprocess.Popen` so the kernel does
  the standard ``fork``/``execve`` pair. The seccomp BPF filter and
  Landlock ruleset that bwrap and the launcher installed before running
  the daemon are inherited by every child - they cannot be relaxed or
  stripped from inside the sandbox. ``close_fds=True`` (the Python
  default) ensures the listener fd is **not** inherited by user code.

The module deliberately stays standard-library-only so it can be
launched with ``python3 -S`` (no ``import site``) for fastest cold
start, and so the launcher's ``compile``/``exec`` step does not need to
reach outside the standard library to load the daemon.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# These constants are duplicated from ``daemon_ipc`` so the in-sandbox
# daemon does not need to import the package; the script is mounted at
# ``/jiuwenbox/sandbox-daemon.py`` and executed directly. Any change must
# be mirrored in ``daemon_ipc.py``.
SANDBOX_RESERVED_DIR = "/jiuwenbox"
SANDBOX_DAEMON_SANDBOX_PATH = f"{SANDBOX_RESERVED_DIR}/sandbox-daemon.py"
SANDBOX_LAUNCHER_PATH = f"{SANDBOX_RESERVED_DIR}/landlock-launcher.py"
SANDBOX_DAEMON_COMMAND = ["python3", "-S", SANDBOX_DAEMON_SANDBOX_PATH]
LISTENER_FD_ENV = "JIUWENBOX_CONTROL_LISTENER_FD"

REQUEST_TYPE_EXEC = "exec"
REQUEST_TYPE_SHUTDOWN = "shutdown"
REQUEST_TYPE_WRITE_FILE = "write_file"
REQUEST_TYPE_READ_FILE = "read_file"
REQUEST_TYPE_LIST_DIR = "list_dir"
REQUEST_TYPE_EXEC_BACKGROUND = "exec_background"
REQUEST_TYPE_BG_STATUS = "bg_status"
REQUEST_TYPE_BG_KILL = "bg_kill"

PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 1 * 1024 * 1024
MAX_STDIN_BYTES = 64 * 1024 * 1024
MAX_STDOUT_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024

ACCEPT_TIMEOUT_SECONDS = 1.0
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger("jiuwenbox.sandbox_daemon")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
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


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > 0xFFFFFFFF:
        raise ValueError(f"frame size {len(payload)} exceeds 4 GiB")
    sock.sendall(struct.pack(">I", len(payload)))
    if payload:
        sock.sendall(payload)


def _recv_frame(sock: socket.socket, max_size: int) -> bytes:
    header = _recv_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size > max_size:
        raise ValueError(
            f"incoming frame size {size} exceeds limit {max_size}",
        )
    return _recv_exact(sock, size)


def _send_response(sock: socket.socket, response: dict[str, Any]) -> None:
    body = json.dumps(response, ensure_ascii=False).encode("utf-8")
    _send_frame(sock, body)


def _exec_response(
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    started: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "ok": started,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    if error:
        response["error"] = error
    return response


def _stringify_command(command: list[Any]) -> list[str]:
    return [str(item) for item in command]


def _normalize_env(env: Any) -> dict[str, str] | None:
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError("env must be a JSON object")
    return {str(key): str(value) for key, value in env.items()}


class DaemonState:
    """Shared mutable state guarded by ``lock``."""

    def __init__(self) -> None:
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self.lock = threading.Lock()
        self.completion = threading.Condition(self.lock)

    def begin_request(self) -> None:
        with self.lock:
            self.in_flight += 1

    def end_request(self) -> None:
        with self.lock:
            self.in_flight -= 1
            if self.in_flight <= 0:
                self.completion.notify_all()

    def wait_drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.lock:
            while self.in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.completion.wait(timeout=remaining)
            return True


def _handle_exec(conn: socket.socket, header: dict[str, Any], state: DaemonState) -> None:
    """Run a single user command and stream the result back to ``conn``."""
    try:
        command = header.get("command")
        if not isinstance(command, list) or not command:
            _send_response(
                conn,
                _exec_response(
                    exit_code=2,
                    stderr="exec request missing 'command'",
                    started=False,
                    error="bad_request",
                ),
            )
            return
        command = _stringify_command(command)

        env_override = _normalize_env(header.get("env"))
        workdir = header.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        timeout = header.get("timeout")
        if timeout is not None and not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a number")
        stdin_size = int(header.get("stdin_size") or 0)
        if stdin_size < 0 or stdin_size > MAX_STDIN_BYTES:
            raise ValueError(f"invalid stdin_size {stdin_size}")

        stdin_bytes = _recv_exact(conn, stdin_size) if stdin_size else b""

        merged_env = dict(os.environ)
        # Children must not see the listener fd or the env var pointing at
        # it; ``close_fds=True`` (Python default) closes the fd, but we also
        # strip the env var so user code cannot trivially fingerprint the
        # daemon.
        merged_env.pop(LISTENER_FD_ENV, None)
        if env_override is not None:
            merged_env.update(env_override)

        proc_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE if stdin_size else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": merged_env,
            "close_fds": True,
            # Isolate each exec in its own session so kill(0) from user
            # code cannot reach the long-running daemon process group.
            "start_new_session": True,
        }
        if workdir:
            proc_kwargs["cwd"] = workdir

        try:
            proc = subprocess.Popen(command, **proc_kwargs)
        except OSError as exc:
            # ``OSError`` already covers ``FileNotFoundError`` and
            # ``PermissionError``; keep one branch (G.ERR.09).
            _send_response(
                conn,
                _exec_response(
                    exit_code=127,
                    stderr=f"failed to spawn command: {exc}",
                    started=False,
                    error="spawn_failed",
                ),
            )
            return

        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=stdin_bytes if stdin_size else None,
                timeout=timeout,
            )
            response = _exec_response(
                exit_code=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                stdout_bytes, stderr_bytes = b"", b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            stderr_text = (
                f"{stderr_text}\nCommand timed out"
                if stderr_text
                else "Command timed out"
            )
            response = _exec_response(
                exit_code=124,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_text,
                error="timeout",
            )

        _send_response(conn, response)
    except (ValueError, ConnectionError) as exc:
        try:
            _send_response(
                conn,
                _exec_response(
                    exit_code=2,
                    stderr=str(exc),
                    started=False,
                    error="bad_request",
                ),
            )
        except OSError:
            pass
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unhandled error while handling exec request")
        try:
            _send_response(
                conn,
                _exec_response(
                    exit_code=1,
                    stderr=f"daemon internal error: {exc}",
                    started=False,
                    error="internal",
                ),
            )
        except OSError:
            pass


def _os_error_response(exc: OSError, fallback: str = "io_error") -> dict[str, Any]:
    """Build a JSON-friendly description of an ``OSError`` from a file op."""
    return {
        "v": PROTOCOL_VERSION,
        "ok": False,
        "error": fallback,
        "errno": exc.errno or 0,
        "stderr": exc.strerror or str(exc),
    }


def _handle_write_file(conn: socket.socket, header: dict[str, Any]) -> None:
    """Write a file on behalf of box-server, no child process involved.

    The daemon already runs inside the sandbox PID/mount/user namespaces
    with the policy uid/gid, Landlock ruleset, and seccomp filter applied,
    so doing this in-process is exactly equivalent to a sandboxed
    ``cat > path``: the same paths are reachable, the same uid owns the
    resulting file, and the same syscall filter is enforced. The win is
    that we avoid forking ``bash`` for every upload.
    """
    try:
        path = header.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("write_file request missing 'path'")
        content_size = int(header.get("content_size") or 0)
        if content_size < 0 or content_size > MAX_FILE_BYTES:
            raise ValueError(f"invalid content_size {content_size}")
        mkdir_parents = bool(header.get("mkdir_parents", True))
        mode = header.get("mode")
        if mode is not None and not isinstance(mode, int):
            raise ValueError("mode must be an integer")
    except ValueError as exc:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": str(exc),
            },
        )
        return

    try:
        content = _recv_exact(conn, content_size) if content_size else b""
    except ConnectionError as exc:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": f"truncated write_file payload: {exc}",
            },
        )
        return

    parent = os.path.dirname(path) or "/"
    if mkdir_parents and parent not in ("", "/"):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            _send_response(conn, _os_error_response(exc, "mkdir_failed"))
            return

    # Open with O_NOFOLLOW so a pre-existing symlink at ``path`` (which a
    # malicious user payload could have planted before the upload arrived)
    # cannot redirect the write to an attacker-chosen location. The link
    # would still be subject to Landlock, but refusing it outright keeps
    # the IPC fast path's behaviour aligned with what the previous
    # ``cat > $target`` pipeline produced.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode if mode is not None else 0o644)
    except OSError as exc:
        _send_response(conn, _os_error_response(exc, "open_failed"))
        return
    try:
        try:
            if content:
                view = memoryview(content)
                offset = 0
                while offset < len(view):
                    written = os.write(fd, view[offset:])
                    if written <= 0:
                        raise OSError(errno.EIO, "short write")
                    offset += written
        except OSError as exc:
            _send_response(conn, _os_error_response(exc, "write_failed"))
            return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    _send_response(conn, {"v": PROTOCOL_VERSION, "ok": True})


def _handle_read_file(conn: socket.socket, header: dict[str, Any]) -> None:
    """Read a file in-process and stream it back as a single frame.

    Matches the safety properties of the previous ``base64 -w 0 -- $path``
    helper but without a ``bash`` cold start. The header is sent first
    with ``content_size``; the body frame follows so binary content
    survives intact (no base64 round-trip, no ``replace`` decoding).
    """
    path = header.get("path")
    if not isinstance(path, str) or not path:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "read_file request missing 'path'",
            },
        )
        return

    try:
        if os.path.islink(path):
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "is_symlink",
                    "errno": errno.ELOOP,
                    "stderr": f"refusing to read symlink {path!r}",
                },
            )
            return
        try:
            stat = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            _send_response(conn, _os_error_response(exc, "not_found"))
            return
        except IsADirectoryError as exc:
            _send_response(conn, _os_error_response(exc, "is_directory"))
            return
        if os.path.isdir(path):
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "is_directory",
                    "errno": errno.EISDIR,
                    "stderr": f"{path!r} is a directory",
                },
            )
            return
        if stat.st_size > MAX_FILE_BYTES:
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "too_large",
                    "stderr": (
                        f"file size {stat.st_size} exceeds limit "
                        f"{MAX_FILE_BYTES}"
                    ),
                },
            )
            return
        try:
            # ``mode`` is required by G.FIO.01 even though the kernel
            # ignores it without ``O_CREAT``; a placeholder of ``0o600``
            # documents the (would-be) least-privilege permission.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            _send_response(conn, _os_error_response(exc, "open_failed"))
            return
        try:
            chunks: list[bytes] = []
            remaining = stat.st_size
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    except OSError as exc:
        _send_response(conn, _os_error_response(exc, "read_failed"))
        return

    _send_response(
        conn,
        {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "content_size": len(content),
        },
    )
    _send_frame(conn, content)


def _list_dir_entries(
    root: str,
    *,
    recursive: bool,
    max_depth: int | None,
    include_files: bool,
    include_dirs: bool,
) -> list[dict[str, Any]]:
    """Build the ``items`` payload for a list_dir response."""
    items: list[dict[str, Any]] = []
    pending: list[tuple[str, int]] = [(root, 0)]
    while pending:
        current, depth = pending.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel_depth = depth + 1
                if max_depth is not None and rel_depth > max_depth:
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and recursive:
                    pending.append((entry.path, rel_depth))
                if is_dir and not include_dirs:
                    continue
                if not is_dir and not include_files:
                    continue
                items.append({
                    "name": entry.name,
                    "path": entry.path,
                    "size": 0 if is_dir else stat.st_size,
                    "is_directory": is_dir,
                    "modified_time": datetime.datetime.fromtimestamp(
                        stat.st_mtime,
                    ).isoformat(),
                    "type": (
                        None
                        if is_dir
                        else (os.path.splitext(entry.name)[1] or None)
                    ),
                })
    items.sort(key=lambda item: item["path"])
    return items


def _handle_list_dir(conn: socket.socket, header: dict[str, Any]) -> None:
    """List a directory tree from the daemon process."""
    path = header.get("path")
    if not isinstance(path, str) or not path:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "list_dir request missing 'path'",
            },
        )
        return
    recursive = bool(header.get("recursive", False))
    raw_max_depth = header.get("max_depth")
    if raw_max_depth is None:
        max_depth: int | None = None
    elif isinstance(raw_max_depth, int) and raw_max_depth >= 0:
        max_depth = raw_max_depth
    else:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "max_depth must be a non-negative integer",
            },
        )
        return
    include_files = bool(header.get("include_files", True))
    include_dirs = bool(header.get("include_dirs", True))

    if not os.path.exists(path):
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "not_found",
                "errno": errno.ENOENT,
                "stderr": f"path {path!r} does not exist",
            },
        )
        return
    if not os.path.isdir(path):
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "not_a_directory",
                "errno": errno.ENOTDIR,
                "stderr": f"path {path!r} is not a directory",
            },
        )
        return

    try:
        items = _list_dir_entries(
            path,
            recursive=recursive,
            max_depth=max_depth,
            include_files=include_files,
            include_dirs=include_dirs,
        )
    except OSError as exc:
        _send_response(conn, _os_error_response(exc, "list_failed"))
        return

    _send_response(
        conn,
        {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "items": items,
        },
    )


@dataclass
class _BgJob:
    job_id: str
    command: list[str]
    proc: subprocess.Popen


_bg_jobs: dict[str, _BgJob | object] = {}
_bg_jobs_lock = threading.Lock()
_BG_JOB_RESERVED = object()


def _try_reserve_bg_job(job_id: str) -> bool:
    with _bg_jobs_lock:
        if job_id in _bg_jobs:
            return False
        _bg_jobs[job_id] = _BG_JOB_RESERVED
        return True


def _release_bg_job_reservation(job_id: str) -> None:
    with _bg_jobs_lock:
        if _bg_jobs.get(job_id) is _BG_JOB_RESERVED:
            del _bg_jobs[job_id]


def _commit_bg_job(job_id: str, job: _BgJob) -> None:
    with _bg_jobs_lock:
        _bg_jobs[job_id] = job


def _sync_bg_job(job: _BgJob) -> None:
    if job.proc.returncode is None:
        job.proc.poll()


def _bg_job_response(
    *,
    ok: bool,
    job_id: str,
    job: _BgJob | None = None,
    started: bool = True,
    error: str | None = None,
    stderr: str = "",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "ok": ok,
        "job_id": job_id,
        "started": started,
    }
    if error:
        response["error"] = error
    if stderr:
        response["stderr"] = stderr
    if job is not None:
        _sync_bg_job(job)
        response["pid"] = job.proc.pid
        response["running"] = job.proc.returncode is None
        response["exit_code"] = job.proc.returncode
    return response


def _handle_exec_background(conn: socket.socket, header: dict[str, Any]) -> None:
    try:
        job_id = header.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("exec_background request missing 'job_id'")
        job_id = job_id.strip()

        command = header.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("exec_background request missing 'command'")
        command = _stringify_command(command)

        env_override = _normalize_env(header.get("env"))
        workdir = header.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        stdin_size = int(header.get("stdin_size") or 0)
        if stdin_size < 0 or stdin_size > MAX_STDIN_BYTES:
            raise ValueError(f"invalid stdin_size {stdin_size}")

        stdin_bytes = _recv_exact(conn, stdin_size) if stdin_size else b""

        if not _try_reserve_bg_job(job_id):
            _send_response(
                conn,
                _bg_job_response(
                    ok=False,
                    job_id=job_id,
                    started=False,
                    error="job_exists",
                    stderr=f"background job {job_id!r} already exists",
                ),
            )
            return

        reserved = True
        try:
            merged_env = dict(os.environ)
            merged_env.pop(LISTENER_FD_ENV, None)
            if env_override is not None:
                merged_env.update(env_override)

            # Background jobs discard stdout/stderr; use ``exec`` when output
            # must be captured.
            proc_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE if stdin_size else subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": merged_env,
                "close_fds": True,
                "start_new_session": True,
            }
            if workdir:
                proc_kwargs["cwd"] = workdir

            try:
                proc = subprocess.Popen(command, **proc_kwargs)
                if stdin_size and proc.stdin is not None:
                    proc.stdin.write(stdin_bytes)
                    proc.stdin.close()
            except OSError as exc:
                _send_response(
                    conn,
                    _bg_job_response(
                        ok=False,
                        job_id=job_id,
                        started=False,
                        error="spawn_failed",
                        stderr=f"failed to spawn background command: {exc}",
                    ),
                )
                return

            job = _BgJob(
                job_id=job_id,
                command=command,
                proc=proc,
            )
            _commit_bg_job(job_id, job)
            reserved = False

            _send_response(conn, _bg_job_response(ok=True, job_id=job_id, job=job))
        finally:
            if reserved:
                _release_bg_job_reservation(job_id)
    except (ValueError, ConnectionError) as exc:
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id=str(header.get("job_id") or ""),
                started=False,
                error="bad_request",
                stderr=str(exc),
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unhandled error while handling exec_background request")
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id=str(header.get("job_id") or ""),
                started=False,
                error="internal",
                stderr=f"daemon internal error: {exc}",
            ),
        )


def _handle_bg_status(conn: socket.socket, header: dict[str, Any]) -> None:
    job_id = header.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id="",
                started=False,
                error="bad_request",
                stderr="bg_status request missing 'job_id'",
            ),
        )
        return
    job_id = job_id.strip()
    with _bg_jobs_lock:
        job = _bg_jobs.get(job_id)
    if job is None or job is _BG_JOB_RESERVED:
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id=job_id,
                started=False,
                error="not_found",
                stderr=f"background job {job_id!r} not found",
            ),
        )
        return
    _send_response(conn, _bg_job_response(ok=True, job_id=job_id, job=job))


def _handle_bg_kill(conn: socket.socket, header: dict[str, Any]) -> None:
    job_id = header.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "bg_kill request missing 'job_id'",
            },
        )
        return
    job_id = job_id.strip()
    signum = header.get("signum", 15)
    if not isinstance(signum, int):
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "signum must be an integer",
            },
        )
        return

    with _bg_jobs_lock:
        job = _bg_jobs.get(job_id)
    if job is None or job is _BG_JOB_RESERVED:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "job_id": job_id,
                "killed": False,
                "reason": "not_found",
            },
        )
        return

    _sync_bg_job(job)
    if job.proc.returncode is not None:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "already_exited",
                "exit_code": job.proc.returncode,
            },
        )
        return

    try:
        job.proc.send_signal(signum)
    except ProcessLookupError:
        _sync_bg_job(job)
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "already_exited",
                "exit_code": job.proc.returncode,
            },
        )
        return
    except PermissionError:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "permission_denied",
                "exit_code": job.proc.returncode,
            },
        )
        return
    except OSError:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "permission_denied",
                "exit_code": job.proc.returncode,
            },
        )
        return

    _sync_bg_job(job)
    _send_response(
        conn,
        {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "job_id": job_id,
            "killed": True,
            "reason": "ok",
            "exit_code": job.proc.returncode,
        },
    )


def _handle_connection(conn: socket.socket, state: DaemonState) -> None:
    state.begin_request()
    try:
        try:
            header_bytes = _recv_frame(conn, MAX_HEADER_BYTES)
        except OSError:
            # ``OSError`` already covers ``ConnectionError`` (G.ERR.09).
            return
        try:
            header = json.loads(header_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "bad_request",
                    "stderr": f"invalid request header: {exc}",
                },
            )
            return

        request_type = header.get("type")
        if request_type == REQUEST_TYPE_EXEC:
            _handle_exec(conn, header, state)
        elif request_type == REQUEST_TYPE_SHUTDOWN:
            _send_response(
                conn,
                {"v": PROTOCOL_VERSION, "ok": True, "type": "shutdown_ack"},
            )
            state.shutdown_event.set()
        elif request_type == REQUEST_TYPE_WRITE_FILE:
            _handle_write_file(conn, header)
        elif request_type == REQUEST_TYPE_READ_FILE:
            _handle_read_file(conn, header)
        elif request_type == REQUEST_TYPE_LIST_DIR:
            _handle_list_dir(conn, header)
        elif request_type == REQUEST_TYPE_EXEC_BACKGROUND:
            _handle_exec_background(conn, header)
        elif request_type == REQUEST_TYPE_BG_STATUS:
            _handle_bg_status(conn, header)
        elif request_type == REQUEST_TYPE_BG_KILL:
            _handle_bg_kill(conn, header)
        else:
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "unknown_request_type",
                    "stderr": f"unknown request type: {request_type!r}",
                },
            )
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
        state.end_request()


@contextlib.contextmanager
def _adopted_listener() -> Iterator[socket.socket]:
    """Recover the host-bound listener fd that box-server passed in.

    Box-server creates the Unix listening socket on its own filesystem and
    passes the resulting fd to bubblewrap via ``subprocess.Popen(pass_fds=...)``.
    Bubblewrap's user command path never closes arbitrary inherited fds, so
    by the time the daemon runs, the fd is already in our process and
    ready to ``accept()``. We wrap it in a Python socket object so the
    standard library accept loop works without re-binding (which Landlock
    would forbid, since the launcher applies the policy filesystem ruleset
    before exec'ing the daemon).

    Exposed as a context manager so resource acquisition (wrapping the
    pre-bound fd in a Python socket object) and release (closing that
    socket) live in the same lexical scope, satisfying the resource-pair
    requirement (G.PRM.03).
    """
    raw = os.environ.get(LISTENER_FD_ENV)
    if raw is None:
        raise RuntimeError(
            f"{LISTENER_FD_ENV} is not set; box-server must hand the daemon "
            "a pre-bound control listener fd",
        )
    try:
        fd = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{LISTENER_FD_ENV}={raw!r} is not an integer",
        ) from exc

    listener = socket.socket(
        family=socket.AF_UNIX,
        type=socket.SOCK_STREAM,
        fileno=fd,
    )
    try:
        listener.settimeout(ACCEPT_TIMEOUT_SECONDS)
        yield listener
    finally:
        try:
            listener.close()
        except OSError:
            pass


def _accept_loop(listener: socket.socket, state: DaemonState) -> None:
    while not state.shutdown_event.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError as exc:
            if exc.errno == errno.EBADF:
                return
            logger.warning("accept() failed: %s", exc)
            continue
        thread = threading.Thread(
            target=_handle_connection,
            args=(conn, state),
            name="jiuwenbox-daemon-worker",
            daemon=True,
        )
        thread.start()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = DaemonState()
    try:
        with _adopted_listener() as listener:
            logger.info(
                "sandbox daemon adopted listener fd; entering accept loop",
            )
            try:
                _accept_loop(listener, state)
            finally:
                # Give in-flight requests a brief window to complete cleanly
                # before the context manager closes the listener.
                state.wait_drain(SHUTDOWN_DRAIN_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("sandbox daemon exited cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
