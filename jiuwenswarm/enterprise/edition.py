# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Zero-dependency edition detection（AS 企业平面）.

产品形态统一由 ``JIUWENSWARM_EDITION`` 环境变量标识（"enterprise" / "personal"）。
本模块只依赖标准库 ``os``，任何启动阶段（包括 ``dotenv_early`` 等早期路径）
都可安全导入；宿主请优先 ``from jiuwenswarm.enterprise import is_enterprise``，
早期路径可直接 ``from jiuwenswarm.enterprise.edition import is_enterprise``。
Gateway 侧使用 ``gateway.enterprise.edition``（不依赖本模块）。
"""

from __future__ import annotations

import os

__all__ = ["is_enterprise"]


def is_enterprise() -> bool:
    """True if JIUWENSWARM_EDITION is 'enterprise' (企业版)."""
    return os.getenv("JIUWENSWARM_EDITION", "").strip().lower() == "enterprise"
