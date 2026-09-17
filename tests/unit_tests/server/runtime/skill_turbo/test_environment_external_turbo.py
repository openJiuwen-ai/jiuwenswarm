# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""environment 外部 turbo 发现：注册 / 前缀 / plan_code 组装 / checksum 排除 turbo。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_min_skill(root: Path, skill_name: str, external: str) -> Path:
    codes = root / external / "turbo" / "turbo_codes" / skill_name
    codes.mkdir(parents=True)
    (codes / "__init__.py").write_text("", encoding="utf-8")
    (codes / f"{skill_name}_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        f"root = PlanNode(plan_name='{skill_name}_root', instruction='i')\n",
        encoding="utf-8",
    )
    (root / external / "turbo" / "meta.json").write_text(
        '{"external_name": "%s", "description": "ext %s", "match_keywords": ["ext"]}'
        % (external, skill_name),
        encoding="utf-8",
    )
    return codes


@pytest.fixture()
def external_root(tmp_path, monkeypatch):
    import jiuwenswarm.common.utils as jw_utils

    monkeypatch.setattr(
        jw_utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path]
    )
    return tmp_path


def _make_env(**overrides):
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )

    config = {"skill_codes_dir": "", "skill_code_import_package": ""}
    config.update(overrides)
    return SkillTurboEnvironment(config)


def test_external_skill_registered_with_package_info(external_root):
    _write_min_skill(external_root, "fakedeck", "fakedeck-craft")
    env = _make_env()
    skill = env.skills.get("fakedeck")
    assert skill is not None
    assert skill.package_name == "skill_turbo_codes_fakedeck"
    assert Path(skill.turbo_codes_dir).is_dir()
    assert skill.external_name == "fakedeck-craft"
    assert skill.description == "ext fakedeck"
    assert "skill_turbo_codes_fakedeck." in env.skill_code_import_prefixes


def test_plan_code_uses_dynamic_package(external_root):
    from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

    _write_min_skill(external_root, "fakedeck", "fakedeck-craft")
    env = _make_env()
    planner = SkillTurboPlanner(env)
    plan_code = planner.build_plan_code("fakedeck")
    assert plan_code == "from skill_turbo_codes_fakedeck.fakedeck.fakedeck_gen_root import root"


def test_builtin_skill_plan_code_unchanged(external_root, monkeypatch):
    """无外部技能时内置 plan_code 组装不受影响（回归保护）。"""
    from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

    import jiuwenswarm.common.utils as jw_utils

    monkeypatch.setattr(
        jw_utils, "resolve_agent_registered_skill_dirs", lambda: []
    )
    env = _make_env()  # skill_codes_dir 为空 -> 扫描跳过 -> 无技能
    planner = SkillTurboPlanner(env)
    assert planner.build_plan_code("fakedeck") is None


def test_checksum_excludes_turbo_dir(tmp_path):
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        _compute_dir_checksum,
    )

    (tmp_path / "SKILL.md").write_text("x", encoding="utf-8")
    base = _compute_dir_checksum(str(tmp_path))
    (tmp_path / "turbo").mkdir()
    (tmp_path / "turbo" / "code.py").write_text("print(1)", encoding="utf-8")
    assert _compute_dir_checksum(str(tmp_path)) == base


def test_external_scan_cache_hit_on_mtime_stable(external_root):
    _write_min_skill(external_root, "fakedeck", "fakedeck-craft")
    env1 = _make_env()
    assert "fakedeck" in env1.skills
    env2 = _make_env()
    assert "fakedeck" in env2.skills
    # 两个 env 实例复用类级缓存（无异常即通过；此处主要防重复扫盘报错）


def test_external_scan_survives_builtin_cache_hit(
    external_root, tmp_path, monkeypatch
):
    """回归：内置 _scan_skills_dir mtime 缓存命中不得短路外部 turbo 扫描。

    生产形态：skill_codes_dir 指向引擎包内已清空的 skill_codes/（仅剩
    __init__.py，.py mtime 恒定）→ 进程内第二次 Environment 构造起内置
    缓存必命中。若外部扫描嵌在 _scan_skills_dir 内部（缓存命中提前
    return 的路径不经过它），第二次构造起外部技能全部丢失
    （no registered skills → 加速调用全部降级标准流）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        SkillTurboEnvironment,
    )

    # 类级缓存隔离，避免与其他用例串扰
    monkeypatch.setattr(SkillTurboEnvironment, "_scan_cache", {})
    monkeypatch.setattr(SkillTurboEnvironment, "_external_scan_cache", {})

    # 内置 skill_codes 目录：存在且含 .py（保证 mtime>0、缓存可写入命中）
    builtin_dir = tmp_path / "builtin_codes"
    builtin_dir.mkdir()
    (builtin_dir / "__init__.py").write_text("", encoding="utf-8")

    _write_min_skill(external_root, "fakedeck", "fakedeck-craft")

    def make() -> SkillTurboEnvironment:
        return SkillTurboEnvironment({
            "skill_codes_dir": str(builtin_dir),
            "skill_code_import_package": "",
        })

    # 首次构造：内置缓存未命中，走完整路径（扫描 + 写缓存），外部扫描执行
    env1 = make()
    assert "fakedeck" in env1.skills

    # 首次扫描已写入类级缓存（mtime>0）；.py 未变 → 第二次构造必命中缓存
    cache_key = str(builtin_dir.resolve())
    scan_cache = SkillTurboEnvironment._scan_cache
    assert cache_key in scan_cache, "首次构造应写入内置扫描缓存"
    assert scan_cache[cache_key][0] > 0.0, "缓存 mtime 应为正值（可命中）"

    # 第二次构造：内置缓存命中路径（提前 return）下，外部技能仍须注册
    env2 = make()
    assert "fakedeck" in env2.skills
    assert env2.skills["fakedeck"].package_name == "skill_turbo_codes_fakedeck"


def test_validator_for_external_skill_prefixes(external_root):
    """外部专用 validator 前缀构成：内置 + 自身动态包前缀，剔除其他动态包。

    前缀经 turbo_package_loader.dynamic_package_prefix 取得（单一来源）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import (
        dynamic_package_prefix,
        is_dynamic_package_prefix,
    )

    env = _make_env()
    validator = env._validator_for_external_skill("deck")

    self_import = "from skill_turbo_codes_deck.deck.common import helper\n"
    cross_import = "from skill_turbo_codes_other.other.common import helper\n"
    assert validator.validate(self_import) == []
    assert validator.validate(cross_import) != []

    # 前缀 helper 单一来源契约
    assert dynamic_package_prefix("deck") == "skill_turbo_codes_deck."
    assert is_dynamic_package_prefix("skill_turbo_codes_deck.")
    assert not is_dynamic_package_prefix(
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes."
    )


def test_external_validator_allows_absolute_self_import(external_root):
    """绝对导入自身动态包：扫描校验放行且 plan_code 可执行（首实例确定性回归）。

    __init__ 的 _skill_code_validator 在 _load 注册动态包之前急切捕获前缀，
    修复前本用例在进程首个 env 实例上校验失败 -> 技能不注册
    （"首请求拒绝、后续请求放行"的非确定行为）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
    from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

    codes = _write_min_skill(external_root, "fakelib", "fakelib-craft")
    (codes / "common.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "class Helper(PlanNode):\n"
        "    def __init__(self):\n"
        "        super().__init__(plan_name='fakelib_helper', instruction='i')\n"
        "    async def _execute(self, inputs):\n"
        "        return inputs\n"
        "helper = Helper()\n",
        encoding="utf-8",
    )
    # 覆写入口：绝对导入自身动态包子模块（修复前被急切前缀校验拒绝）
    (codes / "fakelib_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "from skill_turbo_codes_fakelib.fakelib.common import helper\n"
        "class Root(PlanNode):\n"
        "    def __init__(self):\n"
        "        super().__init__(plan_name='fakelib_root', instruction='i')\n"
        "    async def _execute(self, inputs):\n"
        "        return inputs\n"
        "root = Root()\n"
        "assert helper is not None\n",
        encoding="utf-8",
    )

    env = _make_env()
    assert "fakelib" in env.skills, "绝对导入自身包的外部技能应通过校验并注册"

    planner = SkillTurboPlanner(env)
    executor = SkillTurboExecutor(env)
    plan_code = planner.build_plan_code("fakelib")
    root = executor._prepare_root_node(plan_code)
    assert root.plan_name == "fakelib_root"


def test_external_validator_rejects_cross_skill_absolute_import(external_root):
    """跨技能绝对导入（A 的 code import B 的动态包）：校验一致拒绝。

    B 卸载后 A 会在运行时 ImportError；跨技能共享工具应上收
    skill_turbo_runtime.tool_utils，不走技能间私连。修复前进程级
    注册表已热时（B 先注册）该写法会被放行，形成防线缺口。
    """
    # 先构造 env 注册 otherlib（进程级动态包注册表写入）
    _write_min_skill(external_root, "otherlib", "otherlib-craft")
    env_warm = _make_env()
    assert "otherlib" in env_warm.skills

    # 再放置 badlib：code 绝对导入 otherlib 的动态包
    codes = _write_min_skill(external_root, "badlib", "badlib-craft")
    (codes / "badlib_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "from skill_turbo_codes_otherlib.otherlib.common import helper\n"
        "root = PlanNode(plan_name='badlib_root', instruction='i')\n",
        encoding="utf-8",
    )

    env = _make_env()
    assert "badlib" not in env.skills, "跨技能绝对导入应被外部校验拒绝"
    assert "otherlib" in env.skills
