# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSkillRail 单元测试。

重点覆盖本轮的接管语义修正：原生 ``skills`` 段在无技能命中时提示「用 read_file
读 SKILL.md」，与 skill_protocol「禁止 read_file、只走 skill_tool」直接冲突；故无论
manifest 是否可用，FlashSkillRail 都要撤下原生段并注入 700–740 常驻规则段（730 执行
规范、740 清单），只有 740 清单依赖 manifest（缺失时注入空清单提示，而非回退原生段）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.rails.flash_skill_rail import (
    FlashSkillRail,
    _catalog_excerpt,
    _load_enabled_skills,
)


class _FakeBuilder:
    """按名覆盖存储段，记录 remove_section 调用。"""

    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.sections: dict[str, object] = {}
        self.removed: list[str] = []

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.removed.append(name)
        self.sections.pop(name, None)

    def get_section(self, name: str):
        return self.sections.get(name)


class _FakeAgent:
    def __init__(self, builder: _FakeBuilder) -> None:
        self.system_prompt_builder = builder


def _priorities(builder: _FakeBuilder) -> dict[str, int]:
    return {name: s.priority for name, s in builder.sections.items()}


@pytest.mark.asyncio
async def test_manifest_missing_still_injects_rules_and_empty_catalog(tmp_path) -> None:
    """manifest 缺失时仍撤下原生 skills、注入 700–740（740 为空清单提示），不回退原生段。"""
    builder = _FakeBuilder("cn")
    agent = _FakeAgent(builder)
    rail = FlashSkillRail(manifest_path=tmp_path / "nope" / "manifest.yaml")
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    # 原生 skills 段被撤下。
    assert "skills" in builder.removed
    assert "skills" not in builder.sections

    # 五个常驻规则段按 700/710/720/730/740 注入（730 执行规范、740 清单）。
    assert _priorities(builder) == {
        "skill_overview": 700,
        "skill_selection_policy": 710,
        "skill_loading": 720,
        "skill_execution_rules": 730,
        "skill_catalog": 740,
    }
    catalog = builder.sections["skill_catalog"].content["cn"]
    assert "manifest.yaml 缺失" in catalog


@pytest.mark.asyncio
async def test_manifest_present_catalog_lists_enabled_skills(tmp_path) -> None:
    """manifest 可用时 740 清单列出 default_enabled 技能名，且仍撤下原生段。"""
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "skills:\n"
        "  xlsx-craft:\n"
        "    default_enabled: true\n"
        "    description: Excel 制作\n"
        "  ppt-turbo:\n"
        "    default_enabled: false\n"
        "    description: 不启用\n",
        encoding="utf-8",
    )
    builder = _FakeBuilder("cn")
    agent = _FakeAgent(builder)
    rail = FlashSkillRail(manifest_path=manifest)
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    assert "skills" in builder.removed
    catalog = builder.sections["skill_catalog"].content["cn"]
    assert "xlsx-craft" in catalog
    assert "ppt-turbo" not in catalog


@pytest.mark.asyncio
async def test_skill_sections_use_h1_heading(tmp_path) -> None:
    """700–740 五段都是独立顶层段，段顶标题须用 H1（# ），与其余 flash 段一致。"""
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "skills:\n"
        "  xlsx-craft:\n"
        "    default_enabled: true\n"
        "    description: Excel 制作\n",
        encoding="utf-8",
    )
    builder = _FakeBuilder("cn")
    agent = _FakeAgent(builder)
    rail = FlashSkillRail(manifest_path=manifest)
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    assert builder.sections["skill_overview"].content["cn"].startswith("# 技能总则\n")
    assert builder.sections["skill_loading"].content["cn"].startswith("# 技能加载\n")
    assert builder.sections["skill_selection_policy"].content["cn"].startswith("# 技能选择\n")
    assert builder.sections["skill_execution_rules"].content["cn"].startswith("# 技能执行规范（强制）\n")
    assert builder.sections["skill_catalog"].content["cn"].startswith("# 技能清单\n")


def test_load_enabled_skills_returns_none_on_bad_structure(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("skills:\n  - not a dict\n", encoding="utf-8")
    assert _load_enabled_skills(bad) is None


def test_load_enabled_skills_returns_empty_on_no_enabled(tmp_path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("skills:\n  x:\n    default_enabled: false\n", encoding="utf-8")
    assert _load_enabled_skills(empty) == []


def test_render_catalog_none_emits_note() -> None:
    rail = FlashSkillRail()
    text = rail._render_catalog(None, "cn")
    assert "manifest.yaml 缺失" in text


def test_render_catalog_entries() -> None:
    rail = FlashSkillRail()
    text = rail._render_catalog([("xlsx-craft", "Excel 制作")], "cn")
    assert "xlsx-craft" in text
    assert "Excel 制作" in text


def test_catalog_excerpt_keeps_trigger_and_drops_procedure() -> None:
    """长描述摘录：保留触发词/限制，丢弃加载方式/执行管线与 EN 双语重复。"""
    desc = (
        "【强制·第一步】凡涉及 Excel 表格的任务必须第一步加载本技能。"
        "加载方式（择一）：① skill_tool；② office_claw_load_skill。"
        "加载成功前禁止 read_file 读取源文件、禁止 code/bash 手写 XML。"
        "【触发词】Excel、表格、财务报表、对账、月报、利润表、万元换算等。"
        "产物为表即必须加载，即使未明说 Excel。"
        "【固定财务场景】覆盖财务日报/周报等 10 类场景。"
        "加载后严格按 SKILL.md 环境预检→builder→校验管线→交付执行。"
        " EN: Use for any Excel task."
    )
    excerpt = _catalog_excerpt(desc, 800)
    assert "财务报表" in excerpt
    assert "对账" in excerpt
    assert "月报" in excerpt
    assert "加载方式" not in excerpt
    assert "环境预检" not in excerpt
    assert "EN:" not in excerpt
    assert len(excerpt) <= 800


def test_catalog_excerpt_keeps_restriction_even_with_procedure_words() -> None:
    """限制块（【禁止绕过】）即使提到 builder/校验管线也必须保留，不能当流程句误删。"""
    desc = (
        "【禁止绕过·不得以效率/控制力为由】即使任务复杂，仍必须走本技能的 builder+校验管线交付。"
        "复杂度越高越要用本技能（route→manifest→builder→validation→deliver）。"
        "【触发词】Excel、表格、财务报表。"
        " EN: Use for any Excel task."
    )
    excerpt = _catalog_excerpt(desc, 800)
    assert "禁止绕过" in excerpt
    assert "builder+校验管线" in excerpt  # 限制块内的流程词引用被保留
    assert "Excel" in excerpt  # 触发词保留
    assert "复杂度越高" not in excerpt  # 纯流程句仍丢弃
    assert "EN:" not in excerpt


def test_catalog_excerpt_keeps_short_description_intact() -> None:
    assert _catalog_excerpt("Excel 制作", 800) == "Excel 制作"


def test_catalog_excerpt_empty_returns_empty() -> None:
    assert _catalog_excerpt("", 800) == ""
