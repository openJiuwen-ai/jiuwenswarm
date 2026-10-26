# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""外部 turbo 端到端（fake 布局）：发现→注册→plan_code→root→类型同一性。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_skill(root: Path, skill_name: str, external: str):
    codes = root / external / "turbo" / "turbo_codes" / skill_name
    codes.mkdir(parents=True)
    (codes / "__init__.py").write_text("", encoding="utf-8")
    (codes / f"{skill_name}_common.py").write_text(
        "from skill_turbo_runtime import AbortError, PlanNode\n"
        "class _Root(PlanNode):\n"
        "    def __init__(self):\n"
        "        super().__init__(plan_name='%s_root', instruction='i')\n"
        "    async def _execute(self, inputs):\n"
        "        return inputs\n"
        "def make():\n    return _Root()\n"
        "def raise_abort():\n    raise AbortError('hitl')\n" % skill_name,
        encoding="utf-8",
    )
    (codes / f"{skill_name}_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        f"from .{skill_name}_common import make\n"
        "root = make()\n",
        encoding="utf-8",
    )
    (root / external / "turbo" / "meta.json").write_text(
        '{"external_name": "%s", "description": "%s 任务流", "match_keywords": ["%s"]}'
        % (external, skill_name, skill_name),
        encoding="utf-8",
    )


@pytest.fixture()
def fake_root(tmp_path, monkeypatch):
    _write_skill(tmp_path, "fakedeck", "fakedeck-craft")
    _write_skill(tmp_path, "fakedoc", "fakedoc-craft")
    _write_skill(tmp_path, "fakesheet", "fakesheet-craft")
    import jiuwenswarm.common.utils as jw_utils

    monkeypatch.setattr(
        jw_utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path]
    )
    return tmp_path


def test_end_to_end_all_three_skills(fake_root):
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )
    from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
    from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

    env = SkillTurboEnvironment(
        {"skill_codes_dir": "", "skill_code_import_package": ""}
    )
    assert set(env.skills) >= {"fakedeck", "fakedoc", "fakesheet"}
    assert all(
        env.skills[n].package_name == f"skill_turbo_codes_{n}"
        for n in ("fakedeck", "fakedoc", "fakesheet")
    )

    planner = SkillTurboPlanner(env)
    executor = SkillTurboExecutor(env)
    for name in ("fakedeck", "fakedoc", "fakesheet"):
        plan_code = planner.build_plan_code(name)
        assert plan_code == (
            f"from skill_turbo_codes_{name}.{name}.{name}_gen_root import root"
        )
        root = executor._prepare_root_node(plan_code)
        assert isinstance(root, PlanNode)
        assert root.plan_name == f"{name}_root"


def test_abort_error_type_identity_through_facade(fake_root):
    """门面与动态包加载的 code 模块内 AbortError 为同一类对象（HITL 铁律）。"""
    import importlib
    import sys

    from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
        AbortError as RealAbortError,
    )

    facade = sys.modules["skill_turbo_runtime"]
    assert facade.AbortError is RealAbortError

    # 经动态包加载的 code 模块内 AbortError 同一
    mod = importlib.import_module("skill_turbo_codes_fakedeck.fakedeck.fakedeck_common")
    with pytest.raises(RealAbortError):
        mod.raise_abort()
