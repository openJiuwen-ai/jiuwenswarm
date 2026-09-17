# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Server-managed workspace paths."""

from __future__ import annotations

import os
import pwd
from pathlib import Path


def _effective_user_home() -> Path:
    """Return the effective user's home directory without trusting $HOME."""
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path.home()


JIUWENBOX_HOME = _effective_user_home() / ".jiuwenbox"
SANDBOX_WORKSPACE = JIUWENBOX_HOME / "workspace"
