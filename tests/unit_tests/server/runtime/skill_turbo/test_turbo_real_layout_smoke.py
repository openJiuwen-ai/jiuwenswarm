# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""真实 relay-claw 布局导入冒烟。

需 JIUWENSWARM_TEST_TURBO_SKILLS_DIR 指向 office-claw-skills 目录；
未设置或目录不存在时整体 skip。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REAL_DIR = os.environ.get("JIUWENSWARM_TEST_TURBO_SKILLS_DIR", "")

pytestmark = pytest.mark.skipif(
    not REAL_DIR or not Path(REAL_DIR).is_dir(),
    reason="set JIUWENSWARM_TEST_TURBO_SKILLS_DIR=<office-claw-skills> to enable",
)


@pytest.fixture()
def _real_roots(monkeypatch):
    import jiuwenswarm.common.utils as jw_utils

    monkeypatch.setattr(
        jw_utils,
        "resolve_agent_registered_skill_dirs",
        lambda: [Path(REAL_DIR)],
    )


def test_real_layout_discovery_and_plan_code(_real_roots):
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )
    from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
    from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

    env = SkillTurboEnvironment(
        {"skill_codes_dir": "", "skill_code_import_package": ""}
    )
    # 当前线上仅支持 pptx-craft 加速
    expected = (
        ("ppt", "pptx-craft"),
    )
    for name, external in expected:
        skill = env.skills.get(name)
        assert skill is not None, f"{name} not discovered from real layout"
        assert skill.external_name == external
        assert (Path(skill.turbo_codes_dir) / name).is_dir()

        planner = SkillTurboPlanner(env)
        executor = SkillTurboExecutor(env)
        plan_code = planner.build_plan_code(name)
        root = executor._prepare_root_node(plan_code)
        assert isinstance(root, PlanNode)
        assert root.plan_name  # 真实入口 root 必有 plan_name
