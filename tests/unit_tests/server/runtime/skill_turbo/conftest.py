# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_turbo 单测共享环境：外部 turbo 目录（relay-claw 技能目录）动态包注册。

设置 JIUWENSWARM_TEST_TURBO_SKILLS_DIR 指向 office-claw-skills 时，注册
ppt/docx/xlsx 三个动态包，使 C 类测试（引擎+真实 code 组合行为）可经
``from skill_turbo_codes_ppt.ppt.xxx import ...`` 加载迁移后的 code；
未设置时不注册，相关测试文件以 module-level skip 跳过。
"""

from __future__ import annotations

import os
from pathlib import Path


def _register_external_packages() -> None:
    root = os.environ.get("JIUWENSWARM_TEST_TURBO_SKILLS_DIR", "")
    if not root or not Path(root).is_dir():
        return
    try:
        from jiuwenswarm.server.runtime.skill_turbo.runtime import (
            ensure_runtime_facade,
        )
        from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import (
            ensure_turbo_package,
        )
    except ImportError:
        return

    ensure_runtime_facade()
    for skill_name, external in (
        ("ppt", "pptx-craft"),
        ("docx", "docx-craft"),
        ("xlsx", "xlsx-craft"),
    ):
        codes = Path(root) / external / "turbo" / "turbo_codes"
        if codes.is_dir():
            ensure_turbo_package(skill_name, codes)


_register_external_packages()
