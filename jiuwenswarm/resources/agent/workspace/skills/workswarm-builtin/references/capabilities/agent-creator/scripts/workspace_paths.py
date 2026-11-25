# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-creator skill 脚本的共享路径解析（独立运行，不导入 jiuwenswarm 包）。

脚本安装在 ``<工作空间>/skills/workswarm-builtin/references/capabilities/agent-creator/scripts/``
下；工作空间优先按脚本安装位置推导（个人版与企业版租户各自正确），仅当脚本被复制到
skills 树之外运行时，回退 ``JIUWENSWARM_AGENT_ROOT``（运行期注入，租户感知）与数据根
个人版路径。
"""

from __future__ import annotations

import os
from pathlib import Path


def get_agent_workspace_dir() -> Path:
    """agent 工作区：优先按本脚本安装位置推导。

    回退：``JIUWENSWARM_AGENT_ROOT`` > ``JIUWENSWARM_DATA_DIR`` >
    ``JIUWENSWARM_HOME``/.jiuwenswarm > ``~/.jiuwenswarm``，均取
    ``profile/agent/workspace``（现行契约）。
    """
    for parent in Path(__file__).resolve().parents:
        if parent.name == "skills":
            workspace = parent.parent
            if "resources" not in workspace.parts:
                return workspace
            break
    raw = os.environ.get("JIUWENSWARM_AGENT_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser() / "workspace"
    raw = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser()
    else:
        home = os.environ.get("JIUWENSWARM_HOME", "").strip()
        base = (Path(home) if home else Path.home()) / ".jiuwenswarm"
    return base / "profile" / "agent" / "workspace"
