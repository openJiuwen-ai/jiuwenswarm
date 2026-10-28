# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared naming rules for browser-uploaded attachments.

Documents and images land in the same per-session ``uploads`` directory and
must agree on how a browser-supplied name becomes a path on disk, so the rules
live here instead of being duplicated per attachment kind.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Characters no mainstream filesystem accepts inside a name: ASCII control
# chars, POSIX/Windows path separators, and the rest of the Windows-reserved
# punctuation. Everything else — including non-ASCII letters — is kept.
from gateway.common.attachment_safety import (  # noqa: F401
    safe_session_dirname,
    safe_upload_filename,
)


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


def atomic_write_unique(target: Path, data: bytes) -> Path:
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
    target.parent.mkdir(parents=True, exist_ok=True)
    stem = target.stem
    suffix = target.suffix
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for index in range(1000):
        candidate = target if index == 0 else target.with_name(f"{stem}-{index}{suffix}")
        try:
            fd = os.open(candidate, flags, 0o644)
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
                os.unlink(candidate)
            except OSError:
                pass
            raise
        return candidate
    raise FileExistsError(f"no free upload slot under {target}")


