# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Zero-dependency edition detection.

产品形态统一由 ``JIUWENSWARM_EDITION`` 环境变量标识（"enterprise" / "personal"）。
本模块只依赖标准库 ``os``，任何启动阶段（包括 ``dotenv_early`` 等早期路径）
都可安全导入；``jiuwenswarm.common.local_env_config`` 与 ``jiuwenswarm.common.utils``
从这里 re-export ``is_enterprise``，其余代码不要再各自读取环境变量判断版本。
"""

from __future__ import annotations

import os

__all__ = ["is_enterprise"]


def is_enterprise() -> bool:
    """True if JIUWENSWARM_EDITION is 'enterprise' (企业版)."""
    return os.getenv("JIUWENSWARM_EDITION", "").strip().lower() == "enterprise"
