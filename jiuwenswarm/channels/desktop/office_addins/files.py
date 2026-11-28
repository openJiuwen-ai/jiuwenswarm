# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Files the add-ins keep, which the status route reads while install replaces them."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step, so a reader or a crash never sees it half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fixed temp name let concurrent writers move or delete each other's file:
    # the loser raised FileNotFoundError and a stale manifest could win the race.
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        _replace_with_retry(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _replace_with_retry(source: Path, target: Path, max_attempts: int = 10) -> None:
    """Retry the replace while Office still holds the manifest open on Windows."""
    for attempt in range(max(1, max_attempts)):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == max(1, max_attempts) - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
