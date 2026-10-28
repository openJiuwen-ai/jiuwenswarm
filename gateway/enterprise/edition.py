# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 侧 edition 探测（不依赖 jiuwenswarm.enterprise）.

与 ``jiuwenswarm.enterprise.edition`` 约定同一环境变量，便于日后 gateway 独立成仓后仍可
单独演进，而不经 AgentServer 包导入企业平面。
"""

from __future__ import annotations

import os

__all__ = ["is_enterprise"]


def is_enterprise() -> bool:
    """True if JIUWENSWARM_EDITION is 'enterprise'."""
    return os.getenv("JIUWENSWARM_EDITION", "").strip().lower() == "enterprise"
