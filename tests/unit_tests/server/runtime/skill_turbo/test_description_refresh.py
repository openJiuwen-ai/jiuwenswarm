# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_acceleration_exec 工具描述请求期动态刷新 + rail 指南清单动态化。

覆盖 §3.2 机制五：
- refresh：无上下文空清单 / 请求期刷新 card.description / 幂等 /
  外部扫描失败沿用最近成功清单 / 整体异常返回最近已知清单；
- rail：有清单注入动态清单文案（强制指令保留）/ 空清单整段不注入 /
  disabled 不注入 / 刷新先于注入且两者清单同源。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# ─── 夹具（复用 test_tool_description_external 的最小技能布局） ──────────────


def _write_min_skill(root: Path, external: str, skill_name: str):
    codes = root / external / "turbo" / "turbo_codes" / skill_name
    codes.mkdir(parents=True)
    (codes / f"{skill_name}_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "class _R(PlanNode):\n"
        "    def __init__(self):\n"
        "        super().__init__(plan_name='r', instruction='i')\n"
        "    async def _execute(self, inputs):\n"
        "        return inputs\n"
        "root = _R()\n",
        encoding="utf-8",
    )
    (root / external / "turbo" / "meta.json").write_text(
        '{"external_name": "%s", "description": "外部 %s", "match_keywords": ["extkw"]}'
        % (external, skill_name),
        encoding="utf-8",
    )


def _patch_discover_to(tmp_path, monkeypatch):
    """把 discover 的默认 roots 固定为 tmp_path（保留真实发现逻辑）。"""
    from jiuwenswarm.server.runtime.skill_turbo import (
        turbo_package_loader as tpl,
    )

    orig = tpl.discover_external_turbo_skills
    monkeypatch.setattr(
        tpl,
        "discover_external_turbo_skills",
        lambda skill_roots=None: orig(skill_roots=[tmp_path]),
    )


@pytest.fixture()
def _isolated_state(monkeypatch):
    """隔离模块级可变状态（card.description / _last_good_entries / TTL 清单缓存）。

    _entries_cache 换成全新 dict：测试通过 patch discover 驱动不同结果，
    而缓存 key 取自真实 roots，若不隔离会出现前一个测试的清单缓存
    在 TTL 窗口内污染后一个测试。
    """
    from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools as stt

    saved_desc = stt.skill_turbo.card.description
    saved_last_good = dict(stt._last_good_entries)
    monkeypatch.setattr(stt, "_last_good_entries", {})
    monkeypatch.setattr(stt, "_entries_cache", {})
    yield stt
    stt.skill_turbo.card.description = saved_desc
    stt._last_good_entries.clear()
    stt._last_good_entries.update(saved_last_good)


# ─── refresh_skill_acceleration_description ──────────────────────────────────


def test_refresh_empty_without_context(tmp_path, monkeypatch, _isolated_state):
    """无外部目录可见时：清单为空，描述为"暂无"（等价 sidecar 启动时序）。"""
    stt = _isolated_state
    _patch_discover_to(tmp_path, monkeypatch)  # tmp_path 无技能 → 发现为空

    names = stt.refresh_skill_acceleration_description()
    assert names == []
    assert "暂无" in stt.skill_turbo.card.description
    # 静态尾部（模板排除段）保留
    assert "【临时排除】" in stt.skill_turbo.card.description


def test_refresh_updates_card_description(tmp_path, monkeypatch, _isolated_state):
    """请求期目录可见后：refresh 刷新 card.description 并返回清单。"""
    stt = _isolated_state
    _write_min_skill(tmp_path, "docx-craft", "docx")
    _write_min_skill(tmp_path, "xlsx-craft", "xlsx")
    _patch_discover_to(tmp_path, monkeypatch)
    assert "暂无" in stt.skill_turbo.card.description  # 初始（固化）为暂无

    names = stt.refresh_skill_acceleration_description()
    assert names == ["docx-craft", "xlsx-craft"]
    desc = stt.skill_turbo.card.description
    assert "暂无" not in desc
    assert "docx-craft" in desc
    assert "xlsx-craft" in desc
    assert "【临时排除】" in desc  # 静态尾部不受刷新影响


def test_refresh_idempotent_same_desc_kept(tmp_path, monkeypatch, _isolated_state):
    """幂等：清单不变时描述保持一致（仅变化时写）。"""
    stt = _isolated_state
    _write_min_skill(tmp_path, "pptx-craft", "ppt")
    _patch_discover_to(tmp_path, monkeypatch)

    stt.refresh_skill_acceleration_description()
    desc_after_first = stt.skill_turbo.card.description
    names = stt.refresh_skill_acceleration_description()
    assert names == ["pptx-craft"]
    assert stt.skill_turbo.card.description == desc_after_first


def test_refresh_keeps_last_good_on_external_scan_failure(
    tmp_path, monkeypatch, _isolated_state
):
    """外部扫描瞬时失败：沿用最近一次成功清单，描述不闪断为"暂无"。"""
    stt = _isolated_state
    _write_min_skill(tmp_path, "pptx-craft", "ppt")
    _patch_discover_to(tmp_path, monkeypatch)
    stt.refresh_skill_acceleration_description()
    assert "pptx-craft" in stt.skill_turbo.card.description

    # 清掉 TTL 清单缓存：TTL 窗口内的重复刷新直接命中缓存不会触发扫描，
    # 清缓存以模拟 TTL 过期后的刷新，使下面的失败注入真正走到扫描路径。
    stt._entries_cache.clear()

    # 模拟外部源扫描异常（如目录瞬时不可访问）
    def _boom(skill_roots=None):
        raise OSError("transient scan failure")

    from jiuwenswarm.server.runtime.skill_turbo import (
        turbo_package_loader as tpl,
    )

    monkeypatch.setattr(tpl, "discover_external_turbo_skills", _boom)

    names = stt.refresh_skill_acceleration_description()
    assert names == ["pptx-craft"]
    assert "pptx-craft" in stt.skill_turbo.card.description
    assert "暂无" not in stt.skill_turbo.card.description


def test_refresh_overall_failure_returns_last_known(
    tmp_path, monkeypatch, _isolated_state
):
    """整体异常：保留 card 旧值，返回最近已知清单。"""
    stt = _isolated_state
    _write_min_skill(tmp_path, "pptx-craft", "ppt")
    _patch_discover_to(tmp_path, monkeypatch)
    stt.refresh_skill_acceleration_description()
    desc_before = stt.skill_turbo.card.description

    def _boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(stt, "_collect_skill_entries", _boom)
    names = stt.refresh_skill_acceleration_description()
    assert names == ["pptx-craft"]
    assert stt.skill_turbo.card.description == desc_before


def test_collect_skill_entries_ttl_cache_avoids_rescan(
    tmp_path, monkeypatch, _isolated_state
):
    """TTL 清单缓存：同 roots 的窗口内重复收集不重复扫盘，过期（清缓存）后重扫。"""
    stt = _isolated_state
    _write_min_skill(tmp_path, "pptx-craft", "ppt")

    from jiuwenswarm.server.runtime.skill_turbo import (
        turbo_package_loader as tpl,
    )

    calls = {"n": 0}
    orig = tpl.discover_external_turbo_skills

    def _counting(skill_roots=None):
        calls["n"] += 1
        return orig(skill_roots=[tmp_path])

    monkeypatch.setattr(tpl, "discover_external_turbo_skills", _counting)

    entries1, ok1 = stt._collect_skill_entries()
    assert ok1 is True
    assert "pptx-craft" in entries1
    assert calls["n"] == 1

    # TTL 窗口内：命中缓存，不重复扫盘（before_model_call 每轮模型调用的高频路径）
    entries2, ok2 = stt._collect_skill_entries()
    assert ok2 is True
    assert entries2 == entries1
    assert calls["n"] == 1

    # 缓存清空（等价 TTL 过期）：重新扫盘
    stt._entries_cache.clear()
    entries3, ok3 = stt._collect_skill_entries()
    assert ok3 is True
    assert entries3 == entries1
    assert calls["n"] == 2


# ─── rail 指南清单动态化 ─────────────────────────────────────────────────────


class _FakeBuilder:
    def __init__(self):
        self.sections: dict = {}

    def add_section(self, section):
        self.sections[section.name] = section

    def remove_section(self, name):
        self.sections.pop(name, None)

    def get_section(self, name):
        return self.sections.get(name)


def _make_ctx(builder):
    return SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder))


@pytest.mark.asyncio
async def test_rail_injects_guide_with_dynamic_list(
    tmp_path, monkeypatch, _isolated_state
):
    """有清单：注入指南，文案含动态清单且保留强制指令，card 描述已刷新（同源）。"""
    stt = _isolated_state
    _write_min_skill(tmp_path, "docx-craft", "docx")
    _patch_discover_to(tmp_path, monkeypatch)

    from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import (
        SkillTurboPromptRail,
    )

    builder = _FakeBuilder()
    rail = SkillTurboPromptRail()
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    await rail.before_model_call(_make_ctx(builder))

    section = builder.sections.get("skill_turbo_guide")
    assert section is not None
    text = section.content["cn"]
    # 动态清单（与 refresh 同源：docx-craft）
    assert "当前：docx-craft" in text
    # 强制指令保留
    assert "第一个工具调用必须是" in text
    # 硬编码示例名已去除
    assert "如 pptx-craft / docx-craft / xlsx-craft 等" not in text
    # 工具描述已同步刷新（rail 先刷新后注入，两处清单同源）
    assert "docx-craft" in stt.skill_turbo.card.description
    assert "暂无" not in stt.skill_turbo.card.description


@pytest.mark.asyncio
async def test_rail_skips_guide_when_no_skills(tmp_path, monkeypatch, _isolated_state):
    """空清单：整段不注入（对齐 acceleration_disabled 先例）。"""
    _patch_discover_to(tmp_path, monkeypatch)  # 无技能 → 空清单

    from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import (
        SkillTurboPromptRail,
    )

    builder = _FakeBuilder()
    rail = SkillTurboPromptRail()
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    # 预置一个 section，验证空清单时被移除（不残留旧注入）
    builder.add_section(SimpleNamespace(name="skill_turbo_guide"))

    await rail.before_model_call(_make_ctx(builder))

    assert "skill_turbo_guide" not in builder.sections


@pytest.mark.asyncio
async def test_rail_skips_when_disabled(tmp_path, monkeypatch):
    """acceleration_disabled=True：不注入（既有行为保持）。"""
    from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import (
        SkillTurboPromptRail,
    )

    builder = _FakeBuilder()
    rail = SkillTurboPromptRail(acceleration_disabled=True)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    builder.add_section(SimpleNamespace(name="skill_turbo_guide"))

    await rail.before_model_call(_make_ctx(builder))

    assert "skill_turbo_guide" not in builder.sections


@pytest.mark.asyncio
async def test_rail_refresh_runs_before_inject(tmp_path, monkeypatch):
    """顺序：描述刷新先于指南注入，且两者清单同源一致。"""
    from jiuwenswarm.server.runtime.skill_turbo import (
        skill_turbo_tools as stt,
    )
    from jiuwenswarm.server.runtime.skill_turbo.rails import (
        skill_prompt_rail as spr,
    )

    calls: list[str] = []

    def _fake_refresh():
        calls.append("refresh")
        return ["docx-craft", "pptx-craft"]

    monkeypatch.setattr(stt, "refresh_skill_acceleration_description", _fake_refresh)

    # rail 内部延迟 import 从模块取属性，monkeypatch 生效
    builder = _FakeBuilder()
    rail = spr.SkillTurboPromptRail()
    rail.init(SimpleNamespace(system_prompt_builder=builder))

    await spr.SkillTurboPromptRail.before_model_call(rail, _make_ctx(builder))

    assert calls == ["refresh"]  # 刷新已执行
    section = builder.sections.get("skill_turbo_guide")
    assert section is not None
    # 指南清单与 refresh 返回值一致（同源）
    assert "当前：docx-craft、pptx-craft" in section.content["cn"]
