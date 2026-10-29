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
import hashlib
import json
import logging
import os
import secrets
import socket
import stat as stat_module
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
REQUEST_TYPE_FILE_SNAPSHOT = "file_snapshot"
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


def _snapshot_read(path: str, directory_fd: int | None = None) -> tuple[dict, bytes]:
    kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
    name = os.path.basename(path) if directory_fd is not None else path
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = os.stat(name, follow_symlinks=False, **kwargs)
        if not stat_module.S_ISREG(before.st_mode) or before.st_nlink > 1:
            raise OSError(errno.EINVAL, "Snapshot target must be a regular, non-hardlinked file")
        fd = os.open(name, flags, mode=0o600, **kwargs)
    except FileNotFoundError:
        return {"exists": False, "content_sha256": hashlib.sha256(b"").hexdigest()}, b""
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            info = os.fstat(stream.fileno())
            if not stat_module.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError(errno.EINVAL, "Snapshot target must be a regular, non-hardlinked file")
            if info.st_size > MAX_FILE_BYTES:
                raise OSError(errno.EFBIG, "Snapshot exceeds file size limit")
            content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise OSError(errno.EFBIG, "Snapshot exceeds file size limit")
    finally:
        os.close(fd)
    return {"exists": True, "content_sha256": hashlib.sha256(content).hexdigest()}, content


@contextlib.contextmanager
def _snapshot_lock(path: str, timeout: float):
    """Stable sibling lock: replacing the target inode must not replace its lock."""
    parent = os.path.dirname(path)
    directory_fd = None
    if os.name != "nt":
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, mode=0o600)
    kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
    name = (
        ".jbx-cas-"
        + hashlib.sha256(os.path.normcase(os.path.basename(path)).encode()).hexdigest()[:24]
        + ".lock"
    )
    lock_path = name if directory_fd is not None else os.path.join(parent, name)
    fd = None
    locked = False
    try:
        full_lock_path = os.path.join(parent, name)
        if os.path.normcase(os.path.realpath(full_lock_path)) != os.path.normcase(full_lock_path):
            raise OSError(errno.EINVAL, "Snapshot lock must not be a symbolic link")
        fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            0o600,
            **kwargs,
        )
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "Unsafe snapshot lock file")
        current = os.stat(lock_path, follow_symlinks=False, **kwargs)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(errno.EINVAL, "Snapshot lock was replaced before locking")
        # Windows permits locking beyond EOF; initializing before locking races
        # another process already holding the first-byte lock.
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Snapshot lock timed out") from exc
                time.sleep(0.01)
        current = os.stat(lock_path, follow_symlinks=False, **kwargs)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(errno.EINVAL, "Snapshot lock was replaced")
        yield directory_fd
    finally:
        try:
            if fd is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


def _snapshot_read_access_acl(fd: int) -> bytes | None:
    if sys.platform != "linux":
        return None
    try:
        return os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise
        return None


def _snapshot_restore_access_acl(fd: int, acl: bytes | None) -> None:
    if sys.platform != "linux":
        return
    if acl is not None:
        os.setxattr(fd, "system.posix_acl_access", acl)
    else:
        # The temporary inode must not gain a default ACL absent from the target.
        try:
            os.removexattr(fd, "system.posix_acl_access")
        except OSError as exc:
            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise


def _snapshot_copy_windows_security(source: str, destination: str) -> None:
    """Preserve owner/group/DACL and its inheritance policy before publication."""
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    get = api.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, *([ctypes.POINTER(ctypes.c_void_p)] * 5)]
    get.restype = wintypes.DWORD
    put = api.SetNamedSecurityInfoW
    put.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, *([ctypes.c_void_p] * 4)]
    put.restype = wintypes.DWORD
    owner, group, dacl, descriptor = (ctypes.c_void_p() for _ in range(4))
    information = 7  # OWNER | GROUP | DACL
    error = get(str(source), 1, information, ctypes.byref(owner), ctypes.byref(group),
                ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        get_control = api.GetSecurityDescriptorControl
        get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        get_control.restype = wintypes.BOOL
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        information |= 0x80000000 if control.value & 0x1000 else 0x20000000
        if control.value & 0x0400:  # SE_DACL_AUTO_INHERITED
            error = put(str(destination), 1, information, owner, group, dacl, None)
            if error:
                raise ctypes.WinError(error)
        else:
            # The named getter can annotate legacy ACEs as inherited. Read the
            # raw descriptor so copying it does not change their future behavior.
            get_raw = api.GetFileSecurityW
            get_raw.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            get_raw.restype = wintypes.BOOL
            needed = wintypes.DWORD()
            get_raw(str(source), 7, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise ctypes.WinError(ctypes.get_last_error())
            raw = ctypes.create_string_buffer(needed.value)
            if not get_raw(str(source), 7, raw, needed.value, ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            put_legacy = api.SetFileSecurityW
            put_legacy.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
            put_legacy.restype = wintypes.BOOL
            if not put_legacy(str(destination), information, raw):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(descriptor)


def _snapshot_replace(
    path: str, content: bytes, directory_fd: int | None, permissions: int
) -> None:
    parent, leaf = os.path.dirname(path), os.path.basename(path)
    kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
    target = leaf if directory_fd is not None else path
    parent_identity = os.stat(parent)
    existing = None
    existing_acl = None
    try:
        # Atomic replacement must not bypass an existing file's write permission.
        fd = os.open(
            target,
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            **kwargs,
        )
        try:
            existing = os.fstat(fd)
            if not stat_module.S_ISREG(existing.st_mode) or existing.st_nlink > 1:
                raise OSError(errno.EINVAL, "Unsafe snapshot target")
            existing_acl = _snapshot_read_access_acl(fd)
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    name = f".{leaf}-{secrets.token_hex(12)}.tmp"
    temporary = name if directory_fd is not None else os.path.join(parent, name)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        **kwargs,
    )
    owned = os.fstat(fd)
    published = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            if (
                hasattr(os, "fchown")
                and existing is not None
                and (owned.st_uid, owned.st_gid) != (existing.st_uid, existing.st_gid)
            ):
                os.fchown(stream.fileno(), existing.st_uid, existing.st_gid)
            if existing is not None:
                _snapshot_restore_access_acl(stream.fileno(), existing_acl)
            mode = stat_module.S_IMODE(existing.st_mode) if existing is not None else permissions
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
        if os.name == "nt" and existing is not None:
            _snapshot_copy_windows_security(path, temporary)
        current = os.stat(parent)
        if (current.st_dev, current.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
            raise OSError(errno.EINVAL, "Snapshot parent changed")
        if os.path.normcase(os.path.realpath(path)) != os.path.normcase(path):
            raise OSError(errno.EINVAL, "Snapshot path changed during publication")
        if directory_fd is not None:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise OSError(errno.EINVAL, "Snapshot parent changed before write")
            os.replace(temporary, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            os.replace(temporary, target)
        published = True
    finally:
        if not published:
            try:
                info = os.stat(temporary, follow_symlinks=False, **kwargs)
                if (info.st_dev, info.st_ino) == (owned.st_dev, owned.st_ino):
                    os.unlink(temporary, **kwargs)
            except FileNotFoundError:
                pass


def file_snapshot_operation(header: dict, content: bytes = b"") -> tuple[dict, bytes]:
    """One sandbox-owned read or CAS. No host-path translation or exec fallback."""
    try:
        operation = header.get("operation")
        if operation not in {"read", "compare_replace"}:
            raise ValueError("Unsupported snapshot operation")
        supplied = header.get("path")
        if not isinstance(supplied, str) or not supplied or not os.path.isabs(supplied):
            raise ValueError("Snapshot path must be absolute")
        path = os.path.abspath(supplied)
        if os.name == "nt" and path.startswith(("\\\\.\\", "\\??\\")):
            raise ValueError("Snapshot device paths are not supported")
        if os.path.normcase(os.path.realpath(path)) != os.path.normcase(path):
            raise ValueError("Snapshot path must not traverse symbolic links or reparse points")
        options = header.get("options") or {}
        if not isinstance(options, dict):
            raise TypeError("Snapshot options must be an object")
        expected = options.get("expected_exists")
        digest = options.get("expected_content_sha256")
        if expected is not None and not isinstance(expected, bool):
            raise ValueError("expected_exists must be boolean")
        valid_digest = digest is None or (
            isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        )
        if not valid_digest:
            raise ValueError("expected_content_sha256 must be a SHA256 hex digest")
        if operation == "read":
            metadata, raw = _snapshot_read(path)
            if not metadata["exists"] and not options.get("missing_ok", False):
                return {"ok": False, "error": "not_found"}, b""
            return {"ok": True, **metadata, "content_size": len(raw)}, raw
        if expected is None or digest is None:
            raise ValueError("Conditional write requires existence and content hash")
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("Snapshot exceeds file size limit")
        timeout = float(options.get("lock_timeout", 30))
        if not 0 <= timeout <= 30:
            raise ValueError("lock_timeout must be between 0 and 30 seconds")
        permissions = int(options.get("permissions", "644"), 8)
        if not 0 <= permissions <= 0o777:
            raise ValueError("Invalid snapshot permissions")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _snapshot_lock(path, timeout) as directory_fd:
            metadata, _ = _snapshot_read(path, directory_fd)
            if not metadata["exists"] and options.get("create_if_not_exist") is False:
                return {"ok": False, "error": "not_found"}, b""
            if expected != metadata["exists"] or digest != metadata["content_sha256"]:
                return {"ok": False, "error": "conflict"}, b""
            _snapshot_replace(path, content, directory_fd, permissions)
        return {"ok": True, "atomic_replace": True, "content_size": len(content)}, b""
    except TimeoutError as exc:
        return {"ok": False, "error": "lock_timeout", "stderr": str(exc)}, b""
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": "bad_request", "stderr": str(exc)}, b""
    except OSError as exc:
        return _os_error_response(exc), b""


def _handle_file_snapshot(conn: socket.socket, header: dict[str, Any]) -> None:
    size = header.get("content_size", 0)
    if type(size) is not int or size < 0 or size > MAX_FILE_BYTES:
        _send_response(conn, {"ok": False, "error": "bad_request"})
        return
    content = _recv_exact(conn, size) if size else b""
    response, raw = file_snapshot_operation(header, content)
    _send_response(conn, response)
    if response.get("ok") and header.get("operation") == "read":
        _send_frame(conn, raw)


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
        elif request_type == REQUEST_TYPE_FILE_SNAPSHOT:
            _handle_file_snapshot(conn, header)
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
