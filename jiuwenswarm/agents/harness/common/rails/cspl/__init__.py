# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CSPL 云端安全扫描。

包级惰性导出（PEP 562）：sentinel_rail 依赖 security_lists.risk_classify，
而 risk_classify 又需导入本包叶子模块（constants/scanners）；急加载会在
risk_classify 首导入时形成循环。惰性导出保持 ``from ...cspl import X``
用法不变，首次访问时才加载子模块。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 类型检查/IDE 补全仍可见
    from jiuwenswarm.agents.harness.common.rails.cspl.client import CsplConfig
    from jiuwenswarm.agents.harness.common.rails.cspl.sentinel_rail import (
        CsplSentinelRail,
    )

__all__ = ["CsplConfig", "CsplSentinelRail"]


def __getattr__(name: str):
    if name == "CsplConfig":
        from jiuwenswarm.agents.harness.common.rails.cspl.client import CsplConfig

        return CsplConfig
    if name == "CsplSentinelRail":
        from jiuwenswarm.agents.harness.common.rails.cspl.sentinel_rail import (
            CsplSentinelRail,
        )

        return CsplSentinelRail
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
