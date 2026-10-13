# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight deployment identity for Worker election and reattachment."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def data_scope() -> str:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    path = os.path.normcase(str(get_user_workspace_dir().resolve()))
    profile = os.environ.get("JIUWENSWARM_RUNTIME_PROFILE", "desktop").strip() or "desktop"
    return hashlib.sha256(f"{path}\0{profile}".encode()).hexdigest()


def code_identity() -> str:
    override = os.environ.get("JIUWENSWARM_BUILD_ID", "").strip()
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "jiuwenswarm" / "_worker_build_identity.json"
        try:
            payload = json.loads(bundled.read_text(encoding="utf-8"))
            fingerprint = payload["fingerprint"]
            if (payload.get("schema") != 1 or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64 or any(letter not in "0123456789abcdef" for letter in fingerprint)):
                raise ValueError("invalid controlled build identity")
        except (OSError, ValueError, KeyError, TypeError):
            fingerprint = ""
        if fingerprint:
            return hashlib.sha256(f"frozen-v1\0{fingerprint}\0{override}".encode()).hexdigest()
        # Older packages have no controlled build identity. Preserve the
        # executable-based validation rather than trusting an environment ID.
        executable = Path(sys.executable)
        stat = executable.stat()
        legacy = hashlib.sha256(f"{executable}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()
        return hashlib.sha256(f"{legacy}\0{override}".encode()).hexdigest() if override else legacy
    from jiuwenswarm.server.supervisor.identity_cache import source_fingerprint
    fingerprint = source_fingerprint(Path(__file__).resolve().parents[2])
    # An explicit build label distinguishes deployments, but cannot override
    # dirty source or lockfile validation in a source checkout.
    return hashlib.sha256(f"source-v1\0{fingerprint}\0{override}".encode()).hexdigest()
