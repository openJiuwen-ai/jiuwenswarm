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


# Windows: JIUWENBOX_HOME 与 agent-server (jiuwenclaw) 同根, 放在 <workspace>/jiuwenbox 下.
# agent-server 启动时设 JIUWENCLAW_DATA_DIR (~/.office-claw/.jiuwenclaw), box-server 继承该 env.
# 同根保证 box-server 天然是目录 owner → 改 DACL 不会 WinError 5
# 可能非当前用户或 ACL 被 revoke 残留 → upload/list Permission denied.
# Linux 不变 (~/.jiuwenbox).
#
# OFFICE_CLAW_DATA_ROOT: 上游产品数据根 (~/.office-claw),
# 与 relay-claw 同算法 (env OFFICE_CLAW_DATA_DIR > fallback ~/.office-claw).
# 沙箱 workspace/venv/产物都在该根子树, 受限 token 访问子路径需该根 traverse,
# 默认 ACL 不含 jbx-sandbox/合成 SID → lstat EPERM, apply_sandbox_acl 对该根施加非递归 traverse read 解决. 见 win_acl.py.
if sys.platform == "win32":
    _win_root_env = os.environ.get("JIUWENCLAW_DATA_DIR", "").strip()
    JIUWENCLAW_DATA_DIR_PATH = (
        Path(_win_root_env).expanduser().resolve()
        if _win_root_env
        else _effective_user_home() / ".jiuwenclaw"
    )
    JIUWENBOX_HOME = JIUWENCLAW_DATA_DIR_PATH / "jiuwenbox"
    _office_claw_env = os.environ.get("OFFICE_CLAW_DATA_DIR", "").strip()
    OFFICE_CLAW_DATA_ROOT = (
        Path(_office_claw_env).expanduser().resolve()
        if _office_claw_env
        else _effective_user_home() / ".office-claw"
    )
else:
    JIUWENCLAW_DATA_DIR_PATH = _effective_user_home() / ".jiuwenclaw"
    JIUWENBOX_HOME = _effective_user_home() / ".jiuwenbox"
    OFFICE_CLAW_DATA_ROOT = _effective_user_home() / ".office-claw"
SANDBOX_WORKSPACE = JIUWENBOX_HOME / "workspace"
WIN_SANDBOX_WORKSPACE_ROOT = SANDBOX_WORKSPACE
