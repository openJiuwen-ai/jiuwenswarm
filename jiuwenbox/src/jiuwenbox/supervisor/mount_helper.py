# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Single-threaded helper that bind-mounts paths into a sandbox.

The jiuwenbox server is multi-threaded, and ``setns`` into a mount or user
namespace fails in that case. This process stays single-threaded, forks one
child per request, and that child joins the sandbox namespaces. The parent
never leaves the host namespaces, so it can serve the next request.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import sys
from collections.abc import Callable

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
AT_RECURSIVE = 0x8000
OPEN_TREE_CLONE = 1
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
MOVE_MOUNT_F_EMPTY_PATH = 0x4
SYS_OPEN_TREE = 428
SYS_MOVE_MOUNT = 429
SYS_MOUNT_SETATTR = 442
MS_RDONLY = 1
MS_REMOUNT = 32
MS_BIND = 4096
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
MS_SLAVE = 1 << 19

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.setns.argtypes = [ctypes.c_int, ctypes.c_int]
_libc.setns.restype = ctypes.c_int
_libc.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_void_p,
]
_libc.mount.restype = ctypes.c_int
_syscall = _libc.syscall
_syscall.restype = ctypes.c_long


class _MountAttr(ctypes.Structure):
    """``struct mount_attr`` passed to ``mount_setattr(2)``."""

    _fields_ = (
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    )


def _errno(prefix: str) -> OSError:
    err = ctypes.get_errno()
    return OSError(err, f"{prefix}: {os.strerror(err)}")


def _setns(fd: int, flags: int, label: str) -> None:
    if _libc.setns(fd, flags) != 0:
        raise _errno(label)


def _syscall_long(*args: int) -> int:
    return int(_syscall(*[ctypes.c_long(arg) for arg in args]))


def _open_tree(src: str) -> int:
    source = ctypes.create_string_buffer(src.encode())
    fd = _syscall_long(
        SYS_OPEN_TREE,
        AT_FDCWD,
        ctypes.addressof(source),
        OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC,
    )
    if fd < 0:
        raise _errno(f"open_tree {src}")
    return fd


def _make_private(tree_fd: int) -> bool:
    """Drop the cloned mount out of its source peer group.

    ``open_tree(OPEN_TREE_CLONE)`` copies the source propagation. On a
    systemd host that source is ``shared``, so the detached mount stays in
    the host peer group. Returns ``False`` when this kernel has no
    ``mount_setattr`` (``ENOSYS``); the caller then re-slaves the sandbox
    around every attach instead.
    """
    attr = _MountAttr(propagation=MS_PRIVATE)
    empty = ctypes.create_string_buffer(b"")
    result = _syscall_long(
        SYS_MOUNT_SETATTR,
        tree_fd,
        ctypes.addressof(empty),
        AT_EMPTY_PATH | AT_RECURSIVE,
        ctypes.addressof(attr),
        ctypes.sizeof(attr),
    )
    if result == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.ENOSYS:
        return False
    raise OSError(err, f"mount_setattr private: {os.strerror(err)}")


def _make_rslave(path: str = "/") -> None:
    """Stop mounts under ``path`` from propagating out of this namespace.

    A private clone attached onto a still-shared parent is propagated
    anyway: the kernel walks the destination peer group, not the source's.
    Bubblewrap's ``/usr`` bind is such a parent — it cloned a shared host
    mount and was never made a slave. Slave conversion here touches only
    this namespace's mounts.
    """
    if _libc.mount(None, path.encode(), None, MS_REC | MS_SLAVE, None) != 0:
        raise _errno(f"make rslave {path}")


def _move_mount(tree_fd: int, dst: str) -> None:
    empty = ctypes.create_string_buffer(b"")
    target = ctypes.create_string_buffer(dst.encode())
    result = _syscall_long(
        SYS_MOVE_MOUNT,
        tree_fd,
        ctypes.addressof(empty),
        AT_FDCWD,
        ctypes.addressof(target),
        MOVE_MOUNT_F_EMPTY_PATH,
    )
    if result < 0:
        raise _errno(f"move_mount {dst}")


def _remount_readonly(dst: str) -> None:
    flags = MS_BIND | MS_REMOUNT | MS_RDONLY
    if _libc.mount(None, dst.encode(), None, flags, None) != 0:
        raise _errno(f"remount ro {dst}")


def _root_is_readonly() -> bool:
    return bool(os.statvfs("/").f_flag & os.ST_RDONLY)


def _remount_root(read_only: bool) -> None:
    """Flip the sandbox root between read-only and read-write.

    A parked sandbox keeps the server default policy, whose ``read_only: /``
    makes bubblewrap remount the root read-only. Checkout still has to create
    mount points such as ``/home`` on that root.
    """
    flags = MS_BIND | MS_REMOUNT
    if read_only:
        flags |= MS_RDONLY
    if _libc.mount(None, b"/", None, flags, None) == 0:
        return
    plain = MS_REMOUNT | (MS_RDONLY if read_only else 0)
    if _libc.mount(None, b"/", None, plain, None) != 0:
        raise _errno("remount / read-only" if read_only else "remount / read-write")


def with_writable_root(action: Callable[[], None]) -> None:
    """Run ``action`` with a writable root, then restore a read-only root."""
    root_was_ro = _root_is_readonly()
    if root_was_ro:
        _remount_root(False)
    try:
        action()
    finally:
        if root_was_ro:
            _remount_root(True)


def _ensure_destination(dst: str, is_dir: bool) -> None:
    if is_dir:
        os.makedirs(dst, exist_ok=True)
        return
    parent = os.path.dirname(dst) or "/"
    os.makedirs(parent, exist_ok=True)
    if not os.path.lexists(dst):
        fd = os.open(dst, os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)


def _apply(pid: int, user_ns: bool, mounts: list[dict]) -> None:
    """Join ``pid``'s namespaces and attach each source tree."""
    user_fd = -1
    mnt_fd = -1
    trees: list[int] = []
    try:
        if user_ns:
            user_fd = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
        mnt_fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
        prepared: list[tuple[int, str, bool, bool]] = []
        clones_are_private = True
        for mount in mounts:
            src = str(mount["src"])
            dst = str(mount["dst"])
            is_dir = os.path.isdir(src)
            trees.append(_open_tree(src))
            if clones_are_private:
                clones_are_private = _make_private(trees[-1])
            prepared.append((trees[-1], dst, bool(mount.get("ro")), is_dir))
        if user_fd >= 0:
            _setns(user_fd, CLONE_NEWUSER, "setns user")
        _setns(mnt_fd, CLONE_NEWNS, "setns mnt")

        def attach() -> None:
            # Existing sandbox binds (bwrap's /usr) are still shared peers of
            # the host. Slave them before attaching, or the new mount is
            # copied back onto the host at the same path and never unmounted.
            _make_rslave("/")
            for tree_fd, dst, read_only, is_dir in prepared:
                _ensure_destination(dst, is_dir)
                if not clones_are_private:
                    # The clone itself stayed shared. A nested bind later in
                    # this batch would then see that new mount as a shared
                    # parent and propagate. Slave again before each attach,
                    # and once more after the last one.
                    _make_rslave("/")
                _move_mount(tree_fd, dst)
                if read_only:
                    _remount_readonly(dst)
            if not clones_are_private:
                _make_rslave("/")

        with_writable_root(attach)
    finally:
        for tree_fd in trees:
            os.close(tree_fd)
        if mnt_fd >= 0:
            os.close(mnt_fd)
        if user_fd >= 0:
            os.close(user_fd)


def _child_apply(pid: int, user_ns: bool, mounts: list[dict], err_w: int) -> None:
    try:
        _apply(pid, user_ns, mounts)
    except Exception as exc:  # noqa: BLE001 - the parent only has the pipe.
        try:
            os.write(err_w, str(exc).encode()[:4000])
        except OSError:
            pass
        getattr(os, "_exit")(1)
    getattr(os, "_exit")(0)


def apply_request(message: dict) -> None:
    """Fork a child that performs one mount batch, then wait for it."""
    pid = int(message["pid"])
    user_ns = bool(message.get("user_ns"))
    mounts = list(message.get("mounts") or [])
    err_r, err_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(err_r)
        _child_apply(pid, user_ns, mounts, err_w)
    os.close(err_w)
    try:
        _, status = os.waitpid(child, 0)
    finally:
        err = os.read(err_r, 4096)
        os.close(err_r)
    if os.waitstatus_to_exitcode(status) != 0:
        detail = err.decode(errors="replace").strip() or f"mount helper status {status}"
        raise RuntimeError(detail)


def _write_reply(payload: dict) -> None:
    """Write one JSON line on stdout. The server reads this pipe, not the log."""
    os.write(sys.stdout.fileno(), (json.dumps(payload) + "\n").encode())


def main() -> int:
    """Read one JSON request per line and write one JSON response."""
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        request_id = None
        try:
            message = json.loads(text)
            request_id = message.get("id")
            apply_request(message)
        except Exception as exc:  # noqa: BLE001 - reported to the server.
            _write_reply({"id": request_id, "ok": False, "error": str(exc)})
            continue
        _write_reply({"id": request_id, "ok": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
