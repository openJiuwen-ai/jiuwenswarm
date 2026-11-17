# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Files the add-ins keep, which the status route reads while install replaces them."""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step, so a reader or a crash never sees it half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
