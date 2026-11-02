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
    import jiuwenswarm.common.utils as jw_utils
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )
    from jiuwenswarm.server.runtime.skill_turbo.executor import (
        PlanCodeLoadError,
        PlanCodeValidationError,
        SkillTurboExecutor,
    )

    monkeypatch.setattr(
        jw_utils, "resolve_agent_registered_skill_dirs", list
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


def test_extract_root_node_three_branches():
    """_extract_root_node 三分支（S_03 边界 4）：显式 root 优先 / 恰 1 实例脆弱兜底 / 0 与 >1 报错。

    防护意图：技能包缺显式 root 变量时的兜底行为是契约（兜底取"最后一个
    PlanNode"依赖 dict 迭代顺序），多实例静默取错会导致执行错树。
    """
    import pytest as _pytest

    from jiuwenswarm.server.runtime.skill_turbo.executor import (
        PlanCodeValidationError,
        SkillTurboExecutor,
    )
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode

    class _N(PlanNode):
        def __init__(self, name: str) -> None:
            super().__init__(plan_name=name, instruction="i", sub_plans=[])

        async def _execute(self, inputs):
            return inputs

    explicit = _N("explicit_root")
    # ① 显式 root 变量：直接采用（即使命名空间里还有其他实例）
    ns = {"root": explicit, "other": _N("other")}
    assert SkillTurboExecutor._extract_root_node(ns) is explicit

    # ② 无显式 root 且恰 1 个实例：脆弱兜底采用
    only = _N("only_one")
    assert SkillTurboExecutor._extract_root_node({"x": only}) is only

    # ③ 无实例：ValueError（明确报错而非静默 None）
    with _pytest.raises(ValueError):
        SkillTurboExecutor._extract_root_node({"x": 1})

    # ④ 无显式 root 且 >1 实例：校验错误（禁止静默取最后一个）
    with _pytest.raises(PlanCodeValidationError):
        SkillTurboExecutor._extract_root_node({"a": _N("a"), "b": _N("b")})
