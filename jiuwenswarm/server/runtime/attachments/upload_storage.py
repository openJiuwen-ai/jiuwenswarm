# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared naming rules for browser-uploaded attachments.

Documents and images land in the same per-session ``uploads`` directory and
must agree on how a browser-supplied name becomes a path on disk, so the rules
live here instead of being duplicated per attachment kind.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import stat
from pathlib import Path

# Characters no mainstream filesystem accepts inside a name: ASCII control
# chars, POSIX/Windows path separators, and the rest of the Windows-reserved
# punctuation. Everything else — including non-ASCII letters — is kept.
from gateway.common.attachment_safety import (  # noqa: F401
    safe_session_dirname,
    safe_upload_filename,
)

UPLOAD_READ_CHUNK_BYTES = 512 * 1024


def read_session_upload_chunk(session_id: str, raw_path: str, offset: int, limit: int) -> dict:
    """Read a bounded chunk from this tenant's direct session-upload children."""
    from jiuwenswarm.common.obs_files import MAX_FILE_BYTES
    from jiuwenswarm.common.utils import get_agent_sessions_dir
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    if not session_id or safe_session_dirname(session_id) != session_id:
        raise ValueError("invalid session_id")
    if offset < 0 or not 0 < limit <= UPLOAD_READ_CHUNK_BYTES:
        raise ValueError("invalid offset or limit")
    root = TenantContext.current_tenant_root()
    uploads = get_agent_sessions_dir() / session_id / "uploads"
    path = Path(raw_path)
    path_in_uploads = path.is_absolute() and path.parent == uploads
    name_ok = path.name not in {"", ".", ".."}
    under_tenant = root is not None and uploads.is_relative_to(root)
    if not path_in_uploads or not name_ok or not under_tenant:
        raise PermissionError("path outside tenant session uploads")
    # Anchor every directory and the leaf by descriptor; never follow a symlink.
    # Pair each os.open with os.close in the same try/finally arm (G.PRM.03);
    # a deferred list-close is opaque to that static check.
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, directory_flags, 0o700)
    try:
        for part in uploads.relative_to(root).parts:
            parent_fd = directory
            directory = os.open(part, directory_flags, 0o700, dir_fd=parent_fd)
            os.close(parent_fd)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError("upload must be a single-link regular file")
            if info.st_size > MAX_FILE_BYTES or offset > info.st_size:
                raise ValueError("invalid upload size or offset")
            os.lseek(descriptor, offset, os.SEEK_SET)
            data = os.read(descriptor, min(limit, info.st_size - offset))
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    return {
        "data_base64": base64.b64encode(data).decode("ascii"),
        "offset": offset, "chunk_size": len(data), "size": info.st_size,
        "eof": offset + len(data) >= info.st_size,
        "name": path.name, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def unique_upload_path(path: Path) -> Path:
    """Return ``path`` or the first free ``stem-N`` variant beside it."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-overflow{suffix}")


def atomic_write_unique(target: Path, data: bytes, *, directory_fd: int | None = None) -> Path:
    """Write *data* beside *target* without overwriting an existing file.

    The create-and-open is atomic: ``os.open`` is called with
    ``O_CREAT | O_EXCL | O_WRONLY``, so two concurrent writers can never
    claim the same path and an existing file is never clobbered.  This closes
    the check-then-overwrite race that ``unique_upload_path`` followed by
    ``open(path, "wb")`` leaves open.

    Naming matches the rest of the uploads subsystem: *target* itself first,
    then ``stem-1``, ``stem-2``, … up to ``stem-999``.  Returns the path
    actually written.  After 999 name collisions (a pathological state)
    it raises ``FileExistsError`` rather than silently overwriting.
    """
    from jiuwenswarm.enterprise import is_enterprise

    if is_enterprise():
        from jiuwenswarm.enterprise.capabilities.workspace.quota import (
            check_workspace_write, is_workspace_quota_enabled, resolve_effective_quota,
        )

        if is_workspace_quota_enabled():
            resolve_effective_quota(force_refresh=True)
            check_workspace_write(additional_bytes=len(data))
    if directory_fd is None:
        target.parent.mkdir(parents=True, exist_ok=True)
    stem = target.stem
    suffix = target.suffix
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for index in range(1000):
        candidate = target if index == 0 else target.with_name(f"{stem}-{index}{suffix}")
        try:
            fd = os.open(
                candidate.name if directory_fd is not None else candidate,
                flags, 0o600 if directory_fd is not None else 0o644, dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        try:
            f = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        try:
            with f:
                f.write(data)
        except BaseException:
            try:
                os.unlink(candidate.name if directory_fd is not None else candidate, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        return candidate
    raise FileExistsError(f"no free upload slot under {target}")


