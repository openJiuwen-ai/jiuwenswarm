# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""turbo_package_loader：动态包绑定 / 外部发现 / plan_code 自愈。"""

from __future__ import annotations

import sys
from pathlib import Path

from jiuwenswarm.server.runtime.skill_turbo import (
    turbo_package_loader as tpl,
)


def _write_min_skill(
    root: Path, skill_name: str = "fakedeck", external: str = "fakedeck-craft"
) -> Path:
    """构造最小外部 turbo 布局：{root}/{external}/turbo/turbo_codes/{skill_name}/。"""
    codes = root / external / "turbo" / "turbo_codes" / skill_name
    codes.mkdir(parents=True)
    (codes / "__init__.py").write_text("", encoding="utf-8")
    (codes / f"{skill_name}_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        f"from . import {skill_name}_common\n"
        f"root = {skill_name}_common.make()\n",
        encoding="utf-8",
    )
    (codes / f"{skill_name}_common.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "def make():\n    return PlanNode(plan_name='p0', instruction='i')\n",
        encoding="utf-8",
    )
    meta = root / external / "turbo" / "meta.json"
    meta.write_text(
        '{"external_name": "%s", "description": "d", "match_keywords": ["k"]}'
        % external,
        encoding="utf-8",
    )
    return codes


class TestEnsureTurboPackage:
    def test_registers_unique_package_and_returns_name(self, tmp_path):
        codes = _write_min_skill(tmp_path)
        pkg = tpl.ensure_turbo_package("fakedeck", codes)
        assert pkg == "skill_turbo_codes_fakedeck"
        mod = sys.modules[pkg]
        assert str(Path(codes).resolve()) in list(mod.__path__)
        assert "skill_turbo_runtime" in sys.modules  # 门面已注册

    def test_idempotent_when_dir_unchanged(self, tmp_path):
        codes = _write_min_skill(tmp_path)
        p1 = tpl.ensure_turbo_package("fakedeck", codes)
        m1 = sys.modules[p1]
        p2 = tpl.ensure_turbo_package("fakedeck", codes)
        assert p1 == p2
        assert sys.modules[p1] is m1

    def test_reregisters_when_dir_changes(self, tmp_path):
        codes1 = _write_min_skill(tmp_path / "a")
        pkg = tpl.ensure_turbo_package("fakedeck", codes1)
        import importlib

        importlib.import_module(f"{pkg}.fakedeck_common")  # 触发子模块加载
        assert f"{pkg}.fakedeck_common" in sys.modules

        codes2 = _write_min_skill(tmp_path / "b")
        pkg2 = tpl.ensure_turbo_package("fakedeck", codes2)
        assert pkg2 == pkg
        assert str(Path(codes2).resolve()) in list(sys.modules[pkg2].__path__)
        assert f"{pkg}.fakedeck_common" not in sys.modules  # 旧子模块已清理

    def test_sanitize_illegal_chars(self):
        assert tpl.sanitize_skill_name("my-skill") == "my_skill"
        assert tpl.sanitize_skill_name("a.b") == "a_b"


class TestDiscover:
    def test_discovers_all_skills_in_roots(self, tmp_path):
        _write_min_skill(tmp_path, "fakedeck", "fakedeck-craft")
        _write_min_skill(tmp_path, "fakedoc", "fakedoc-craft")
        found = tpl.discover_external_turbo_skills(skill_roots=[tmp_path])
        by_name = {f.skill_name: f for f in found}
        assert set(by_name) == {"fakedeck", "fakedoc"}
        assert by_name["fakedeck"].external_name == "fakedeck-craft"
        assert by_name["fakedeck"].meta["match_keywords"] == ["k"]
        assert Path(by_name["fakedeck"].turbo_codes_dir).is_dir()

    def test_root_itself_as_skill_dir(self, tmp_path):
        # root 直接是技能目录（root/turbo/turbo_codes/... 存在）
        codes = tmp_path / "turbo" / "turbo_codes" / "fakedeck"
        codes.mkdir(parents=True)
        (codes / "fakedeck_gen_root.py").write_text(
            "from skill_turbo_runtime import PlanNode\n"
            "root = PlanNode(plan_name='r', instruction='i')\n",
            encoding="utf-8",
        )
        found = tpl.discover_external_turbo_skills(skill_roots=[tmp_path])
        assert any(f.skill_name == "fakedeck" for f in found)

    def test_no_turbo_dir_yields_empty(self, tmp_path):
        (tmp_path / "plain-skill").mkdir()
        assert tpl.discover_external_turbo_skills(skill_roots=[tmp_path]) == []

    def test_no_root_file_skipped(self, tmp_path):
        codes = tmp_path / "x-craft" / "turbo" / "turbo_codes" / "fakedeck"
        codes.mkdir(parents=True)
        # 注意不能用形如 xxx_root.py 的名字（会命中 *_root.py 入口约定）
        (codes / "helper.py").write_text("x = 1\n", encoding="utf-8")
        found = tpl.discover_external_turbo_skills(skill_roots=[tmp_path])
        assert found == []


class TestPlanCodeSelfHeal:
    def test_ensure_packages_for_plan_code(self, tmp_path, monkeypatch):
        codes = _write_min_skill(tmp_path)
        pkg = tpl.ensure_turbo_package("fakedeck", codes)
        sys.modules.pop(pkg, None)  # 模拟进程内未注册

        import jiuwenswarm.common.utils as jw_utils

        monkeypatch.setattr(
            jw_utils,
            "resolve_agent_registered_skill_dirs",
            lambda: [tmp_path],
        )
        plan_code = f"from {pkg}.fakedeck.fakedeck_gen_root import root"
        tpl.ensure_packages_for_plan_code(plan_code)
        assert pkg in sys.modules
        assert pkg in tpl.registered_packages()

    def test_non_dynamic_prefix_ignored(self):
        # 内置 plan_code 前缀不触发发现
        tpl.ensure_packages_for_plan_code(
            "from jiuwenswarm.server.runtime.skill_turbo.skill_codes.fakedeck.fakedeck_gen_root import root"
        )
