# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Align host-created workspace paths with the jiuwenbox sandbox uid.

jiuwenbox's in-sandbox daemon runs as a non-root user (default ``app`` /
uid 1000). When AgentServer creates ``projects/.../work`` as root (openshift
claw image defaults to USER root; MODE=dev also runs as root), those
directories land as ``root:root`` mode 0755 and sandbox ``write_file`` /
upload fails with EACCES (surfaced as HTTP 409 Permission denied).

Call :func:`ensure_sandbox_writable` after mkdir of sandbox-bound writable
paths. No-op on non-posix, when not running as root, or when chown fails.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SANDBOX_UID = 1000
_DEFAULT_SANDBOX_GID = 1000
_ENV_UID = "JIUWENSWARM_SANDBOX_UID"
_ENV_GID = "JIUWENSWARM_SANDBOX_GID"


def sandbox_fs_uid() -> int:
    """Return the uid the sandbox daemon uses for workspace IO."""
    raw = (os.environ.get(_ENV_UID) or "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_SANDBOX_UID


def sandbox_fs_gid() -> int:
    """Return the gid the sandbox daemon uses for workspace IO."""
    raw = (os.environ.get(_ENV_GID) or "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_SANDBOX_GID


def ensure_sandbox_writable(
    path: Path | str,
    *,
    uid: int | None = None,
    gid: int | None = None,
    recursive: bool = False,
) -> None:
    """Chown ``path`` to the sandbox uid/gid when the process is root.

    Args:
        path: File or directory that the sandboxed agent must be able to write.
        uid: Override sandbox uid (default from env / 1000).
        gid: Override sandbox gid (default from env / 1000).
        recursive: When ``path`` is a directory, also chown descendants.
    """
    if os.name != "posix":
        return
    try:
        if os.geteuid() != 0:
            return
    except AttributeError:
        return

    target = Path(path)
    if not target.exists():
        return

    owner_uid = sandbox_fs_uid() if uid is None else uid
    owner_gid = sandbox_fs_gid() if gid is None else gid

    try:
        os.chown(target, owner_uid, owner_gid)
        if recursive and target.is_dir() and not target.is_symlink():
            for root, dirs, files in os.walk(target):
                for name in dirs:
                    child = os.path.join(root, name)
                    if not os.path.islink(child):
                        os.chown(child, owner_uid, owner_gid)
                for name in files:
                    child = os.path.join(root, name)
                    if not os.path.islink(child):
                        os.chown(child, owner_uid, owner_gid)
    except OSError as exc:
        logger.warning(
            "[sandbox_fs] chown failed path=%s uid=%s gid=%s error=%s",
            target,
            owner_uid,
            owner_gid,
            exc,
        )
