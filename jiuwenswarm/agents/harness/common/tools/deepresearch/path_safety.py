# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cross-platform directory metadata checks for DeepResearch artifacts."""

from __future__ import annotations

import os
import stat


def is_direct_directory(metadata: object) -> bool:
    """Return true only for a directory that is not a link or reparse point."""
    mode = getattr(metadata, "st_mode", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISDIR(mode)
        and not stat.S_ISLNK(mode)
        and not attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def is_direct_regular_file(metadata: object) -> bool:
    """Return true only for a regular file that is not a link or reparse point."""
    mode = getattr(metadata, "st_mode", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISREG(mode)
        and not stat.S_ISLNK(mode)
        and not attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def private_mode_is_compatible(metadata: object, expected_mode: int) -> bool:
    """Enforce exact POSIX modes without rejecting Windows' synthetic modes."""
    if os.name == "nt" or hasattr(metadata, "st_file_attributes"):
        return True
    return stat.S_IMODE(getattr(metadata, "st_mode", 0)) == expected_mode
