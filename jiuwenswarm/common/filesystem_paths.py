# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Filesystem-only path adaptation for Windows extended-length operations."""

from __future__ import annotations

import ntpath
import os
import sys
from pathlib import Path


def extended_path(path: str | Path) -> str:
    """Return an absolute Windows extended path, preserving other platforms.

    Use only at filesystem boundaries, after validating untrusted paths. This
    does not resolve symbolic links or change package identities.
    """
    value = os.fspath(path)
    if sys.platform != "win32" or value.startswith(("\\\\?\\", "\\\\.\\")):
        return value
    value = ntpath.abspath(value)
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value
