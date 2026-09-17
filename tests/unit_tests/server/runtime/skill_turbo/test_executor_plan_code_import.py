# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""executor：外部动态包 plan_code 可加载 + resume 自愈注册。"""

from __future__ import annotations

import sys
from pathlib import Path


def _write_min_skill(root: Path) -> Path:
    codes = root / "fakedeck-craft" / "turbo" / "turbo_codes" / "fakedeck"
    codes.mkdir(parents=True)
    (codes / "__init__.py").write_text("", encoding="utf-8")
    (codes / "fakedeck_gen_root.py").write_text(
        "from skill_turbo_runtime import AbortError, PlanNode\n"
        "from .fakedeck_common import make\n"
        "root = make()\n",
        encoding="utf-8",
    )
    (codes / "fakedeck_common.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "class _Root(PlanNode):\n"
        "    def __init__(self):\n"
        "        super().__init__(plan_name='ppt_root', instruction='i')\n"
        "    async def _execute(self, inputs):\n"
        "        return inputs\n"
        "def make():\n    return _Root()\n",
        encoding="utf-8",
    )
    (root / "fakedeck-craft" / "turbo" / "meta.json").write_text(
        '{"external_name": "fakedeck-craft", "description": "d", "match_keywords": ["k"]}',
        encoding="utf-8",
    )
    return codes.parent


def test_execute_external_plan_code_end_to_end(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )
    from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
        PlanNode as RealPlanNode,
    )
    from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import (
        ensure_turbo_package,
    )

    _write_min_skill(tmp_path)
    import jiuwenswarm.common.utils as jw_utils

    monkeypatch.setattr(
        jw_utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path]
    )

    env = SkillTurboEnvironment(
        {"skill_codes_dir": "", "skill_code_import_package": ""}
    )
    executor = SkillTurboExecutor(env)
    plan_code = "from skill_turbo_codes_fakedeck.fakedeck.fakedeck_gen_root import root"

    # 模拟 resume：包未注册（进程内丢失），executor 加载时自愈
    sys.modules.pop("skill_turbo_codes_fakedeck", None)
    root = executor._prepare_root_node(plan_code)
    assert isinstance(root, RealPlanNode)
    assert root.plan_name == "ppt_root"

    # 动态包已注册且 plan_code 可重复加载（deepcopy 独立副本）
    assert (
        ensure_turbo_package(
            "fakedeck", tmp_path / "fakedeck-craft" / "turbo" / "turbo_codes"
        )
        == "skill_turbo_codes_fakedeck"
    )
    root2 = executor._prepare_root_node(plan_code)
    assert root2 is not root


def test_unresolvable_package_raises_plan_code_error(tmp_path, monkeypatch):
    """包无法自愈注册时，加载失败抛 PlanCode 校验/加载错误（降级链路兜底）。"""
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )
    from jiuwenswarm.server.runtime.skill_turbo.executor import (
        PlanCodeLoadError,
        PlanCodeValidationError,
        SkillTurboExecutor,
    )

    import jiuwenswarm.common.utils as jw_utils

    monkeypatch.setattr(
        jw_utils, "resolve_agent_registered_skill_dirs", lambda: []
    )
    env = SkillTurboEnvironment(
        {"skill_codes_dir": "", "skill_code_import_package": ""}
    )
    executor = SkillTurboExecutor(env)
    import pytest

    with pytest.raises((PlanCodeValidationError, PlanCodeLoadError)):
        executor._prepare_root_node(
            "from skill_turbo_codes_ghost.ghost.ghost_gen_root import root"
        )
