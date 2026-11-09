# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Keep one child-free procfs so nested user+pid namespaces can mount proc.

Ubuntu 5.15 rejects ``mount("proc")`` from a new user namespace when every
procfs in the copied mount tree has locked children (``/proc/sys``,
``/proc/kcore``, and the other runtime masks). ``mnt_already_visible``
accepts the mount as soon as one procfs covers the whole filesystem and has
no such children. That procfs is only a witness for the check. bwrap's
``--bind`` is not recursive, so the mount is not carried into the sandbox
even when a parent directory is bound.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import threading

from jiuwenbox.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

# Parent is mode 0700 so other uids in the container cannot walk into the
# unmasked proc. The proc mount itself replaces the leaf directory's mode.
_PARENT = "/run/jiuwenbox-visible-proc"
_MOUNTPOINT = "/run/jiuwenbox-visible-proc/mnt"
MS_REC = 16384
MS_PRIVATE = 1 << 18

_lock = threading.Lock()
_ready = False
_warned = False


def _mount(source: str | None, target: str, fstype: str | None, flags: int) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    libc.mount.restype = ctypes.c_int
    src = None if source is None else source.encode()
    fs = None if fstype is None else fstype.encode()
    if libc.mount(src, target.encode(), fs, flags, None) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target)


def reset_visible_proc_state() -> None:
    """Clear the once-per-process mount result. Used by tests."""
    global _ready, _warned
    _ready = False
    _warned = False


def visible_proc_is_ready() -> bool:
    """Whether the child-free procfs mount has succeeded in this process."""
    return _ready


def ensure_visible_proc() -> None:
    """Mount a child-free procfs once. Failures are logged, not raised.

    A container without ``CAP_SYS_ADMIN`` cannot create the witness mount.
    Sandbox creation then continues and bwrap reports the original error.
    """
    global _ready, _warned
    if _ready:
        return
    with _lock:
        if _ready:
            return
        try:
            os.makedirs(_PARENT, mode=0o700, exist_ok=True)
            os.chmod(_PARENT, 0o700)
            os.makedirs(_MOUNTPOINT, mode=0o700, exist_ok=True)
            if not os.path.ismount(_MOUNTPOINT):
                # Keep the witness mount inside this mount namespace.
                try:
                    _mount(None, _PARENT, None, MS_REC | MS_PRIVATE)
                except OSError as exc:
                    logger.debug("Could not mark %s private: %s", _PARENT, exc)
                _mount("proc", _MOUNTPOINT, "proc", 0)
                try:
                    _mount(None, _MOUNTPOINT, None, MS_PRIVATE)
                except OSError as exc:
                    logger.debug("Could not mark %s private: %s", _MOUNTPOINT, exc)
        except OSError as exc:
            if exc.errno == errno.EBUSY and os.path.ismount(_MOUNTPOINT):
                _ready = True
                return
            if not _warned:
                logger.warning(
                    "Could not mount a child-free procfs at %s (%s). "
                    "Creating a sandbox with a new user and PID namespace may fail "
                    "on kernels that reject masked /proc.",
                    _MOUNTPOINT,
                    exc,
                )
                _warned = True
            return
        _ready = True
        logger.info("Mounted child-free procfs at %s for nested proc mounts", _MOUNTPOINT)
