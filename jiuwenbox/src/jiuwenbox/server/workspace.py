# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Server-managed workspace paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform != "win32":
    import pwd


def _effective_user_home() -> Path:
    """Return the effective user's home directory without trusting $HOME."""
    if sys.platform == "win32":
        # Windows 无 pwd/geteuid, 直接用 Path.home() (走 USERPROFILE/USERDOMAIN).
        return Path.home()
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path.home()


# JIUWENBOX_HOME: ~/.jiuwenbox on Windows and Linux.
# Per-sandbox cwd is JIUWENBOX_HOME/workspace/<id>.
#
# OFFICE_CLAW_DATA_ROOT: 上游产品数据根 (~/.office-claw),
# 与 relay-claw 同算法 (env OFFICE_CLAW_DATA_DIR > fallback ~/.office-claw).
# 该根不再给沙箱特殊授权; workspace 祖先 traverse 由 grant_parent_traverse 覆盖.
JIUWENBOX_HOME = _effective_user_home() / ".jiuwenbox"
if sys.platform == "win32":
    _office_claw_env = os.environ.get("OFFICE_CLAW_DATA_DIR", "").strip()
    OFFICE_CLAW_DATA_ROOT = (
        Path(_office_claw_env).expanduser().resolve()
        if _office_claw_env
        else _effective_user_home() / ".office-claw"
    )
else:
    OFFICE_CLAW_DATA_ROOT = _effective_user_home() / ".office-claw"
SANDBOX_WORKSPACE = JIUWENBOX_HOME / "workspace"
WIN_SANDBOX_WORKSPACE_ROOT = SANDBOX_WORKSPACE
