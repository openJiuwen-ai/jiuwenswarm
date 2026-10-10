# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight deployment identity for Worker election and reattachment."""

from __future__ import annotations

import hashlib
import os
import sys
from functools import lru_cache
from pathlib import Path


def data_scope() -> str:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    path = os.path.normcase(str(get_user_workspace_dir().resolve()))
    profile = os.environ.get("JIUWENSWARM_RUNTIME_PROFILE", "desktop").strip() or "desktop"
    return hashlib.sha256(f"{path}\0{profile}".encode()).hexdigest()


@lru_cache(maxsize=1)
def code_identity() -> str:
    override = os.environ.get("JIUWENSWARM_BUILD_ID", "").strip()
    if override:
        return override
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        stat = executable.stat()
        return hashlib.sha256(f"{executable}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256(str(root).encode())
    # Detect both checkout changes and local edits without starting git on the
    # Front startup path. Runtime code and the IPC ABI participate in identity.
    for source in sorted(root.rglob("*.py")):
        digest.update(str(source.relative_to(root)).encode())
        digest.update(source.read_bytes())
    for manifest in (root.parent / "pyproject.toml", root.parent / "uv.lock"):
        if manifest.exists():
            digest.update(manifest.read_bytes())
    return digest.hexdigest()
