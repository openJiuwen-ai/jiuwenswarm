# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""M0 S1: flash 离线组装 + golden 快照 drift 检测。

用真实 Deep 提示词构建函数 + flash content 域常量/纯函数，离线重建 flash 自有静态段
（v2 = safety/response/tools/todo/subagent 域），与落盘 golden 快照逐级比对，
锁定任何上游内容或 flash 改写逻辑的漂移。

CI 下 ``agent_shield`` 已安装，``offline.assemble`` 直接可 import；本地缺 ``agent_shield``
时，在 import 前打两个叶模块桩（``openjiuwen.harness.rails.base.DeepAgentRail`` + flash 包
namespace），它们仅作基类/包占位，不影响离线组装结果。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

# ── 本地开发 import 桩（CI 有 agent_shield 时自动跳过）──────────────────────
if importlib.util.find_spec("agent_shield") is None:
    if "openjiuwen.harness.rails.base" not in sys.modules:
        _rails_base = types.ModuleType("openjiuwen.harness.rails.base")
        _rails_base.DeepAgentRail = type("DeepAgentRail", (), {})
        sys.modules["openjiuwen.harness.rails.base"] = _rails_base

    _flash_dir = str(
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm" / "agents" / "harness" / "flash"
    )
    for _name, _path in (
        ("jiuwenswarm.agents.harness.flash", [_flash_dir]),
        ("jiuwenswarm.agents.harness.flash.rails", [os.path.join(_flash_dir, "rails")]),
    ):
        if _name not in sys.modules:
            _pkg = types.ModuleType(_name)
            _pkg.__path__ = _path
            sys.modules[_name] = _pkg

from jiuwenswarm.agents.harness.flash.prompt.offline import (  # noqa: E402
    LANGUAGES,
    SNAPSHOT_DOMAINS,
    assemble,
)
from jiuwenswarm.agents.harness.flash.prompt.registry import (  # noqa: E402
    FLASH_PROMPT_REGISTRY,
    PromptRenderContext,
    build_default_context,
    reconcile,
)
from jiuwenswarm.agents.harness.flash.prompt.snapshot import (  # noqa: E402
    build_prompt_snapshot,
)
from jiuwenswarm.agents.harness.flash.sections import (  # noqa: E402
    FLASH_SECTION_BANDS,
    FLASH_SECTION_REGISTRY,
    section_in_band,
)

_SNAPSHOT_DIR = Path(__file__).resolve().parent / "flash_snapshot"

# v2 目标段序（段号即 priority，与 sections.py 领号一致）。
_EXPECTED_SECTIONS = [
    (30, "uncertainty_failure"),
    (100, "safety_general"),
    (110, "refusal_behavior"),
    (120, "deletion_protocol"),
    (300, "tool_selection"),
    (330, "bash_discipline"),
    (340, "tool_result"),
    (410, "thinking_discipline"),
    (430, "todo"),
    (440, "task_tool"),
    (500, "language_output"),
    (510, "response"),
    (520, "final_visible_reply"),
    (540, "runtime.model_answer_policy"),
]


def _golden(language: str) -> dict:
    return json.loads(
        (_SNAPSHOT_DIR / f"flash_prompt.{language}.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_offline_section_order_matches_registry(language: str) -> None:
    sections = assemble(language)
    # 段名/段号序与期望一致（段名对账依赖 content 常量，见下）。
    assert [(s.priority, s.name) for s in sections] == _EXPECTED_SECTIONS
    # 每个段号都已在领号表登记（段号单一真相源 = FLASH_SECTION_REGISTRY）。
    # 若 registry 改段号/增删段未同步领号表，这里会失败——而不是测试继续用旧硬编码通过。
    for s in sections:
        assert s.priority in FLASH_SECTION_REGISTRY, f"段号 {s.priority} 未在领号表登记"


def _band_of(priority: int) -> str | None:
    for band, rng in FLASH_SECTION_BANDS.items():
        if priority in rng:
            return band
    return None


def test_offline_sections_fall_in_declared_band() -> None:
    """每个产出段号必须落在领号表声明的段位区间内（消费 section_in_band）。"""
    for language in LANGUAGES:
        for s in assemble(language):
            band = _band_of(s.priority)
            assert band is not None, f"段号 {s.priority} 不在任何 band 区间"
            assert section_in_band(s.priority, band), (
                f"段号 {s.priority} 应落在 {band} 区间"
            )


@pytest.mark.parametrize("language", LANGUAGES)
def test_offline_assembly_is_deterministic(language: str) -> None:
    first = build_prompt_snapshot(assemble(language), language=language)
    second = build_prompt_snapshot(assemble(language), language=language)
    assert first.full_hash == second.full_hash
    assert first.section_hashes == second.section_hashes
    assert first.band_hashes == second.band_hashes


@pytest.mark.parametrize("language", LANGUAGES)
def test_offline_assembly_matches_golden_snapshot(language: str) -> None:
    snapshot = build_prompt_snapshot(assemble(language), language=language)
    golden = _golden(language)

    assert snapshot.full_hash == golden["full_hash"]
    assert snapshot.static_hash == golden["static_hash"]
    assert snapshot.band_hashes == {b["name"]: b["sha256"] for b in golden["bands"]}
    assert snapshot.section_hashes == tuple(
        (s["priority"], s["name"], s["sha256"]) for s in golden["sections"]
    )
    # 段名/段号/区级归属逐项一致。
    assert [s.priority for s in snapshot.sections] == [s["priority"] for s in golden["sections"]]
    assert [s.name for s in snapshot.sections] == [s["name"] for s in golden["sections"]]
    assert [s.band for s in snapshot.sections] == [s["band"] for s in golden["sections"]]


def test_offline_sections_are_registered_and_static() -> None:
    for language in LANGUAGES:
        snapshot = build_prompt_snapshot(assemble(language), language=language)
        assert snapshot.sections, "v1 快照不应为空"
        for section in snapshot.sections:
            assert section.registered is True
            assert section.carrier == "system"
            assert section.priority < 800  # 全为静态区


def test_safety_relay_merge_present() -> None:
    cn = build_prompt_snapshot(assemble("cn"), language="cn")
    text = {s.name: s.text for s in cn.sections}["deletion_protocol"]
    assert "移入系统回收站" in text
    assert "同卷软删除回退" in text


def test_thinking_discipline_three_contradictions_applied() -> None:
    # 410：三矛盾修正（修复.md ①-⑤）已落地——「写文件轮」限定为「终稿落盘轮」、
    # 无条件句改条件触发句、病句修正、错别字修正；「严禁誊写产物」硬规则仍在（⑤ 重复保留）。
    cn = build_prompt_snapshot(assemble("cn"), language="cn")
    text = {s.name: s.text for s in cn.sections}["thinking_discipline"]
    assert "终稿落盘轮" in text
    assert "写文件轮" not in text
    assert "当你已完成素材收集、方案与模板均已在前文确定、本轮只做终稿落盘时" in text
    assert "随后立即结束思考、发起工具调用" in text
    assert "将要写入工具参数的具体内容" in text
    assert "严禁誊写产物" in text


def test_snapshot_domains_match_declaration() -> None:
    assert tuple(SNAPSHOT_DOMAINS) == (
        "safety",
        "response",
        "tools",
        "todo",
        "subagent",
    )


def test_golden_excludes_complete_tools_mother_section() -> None:
    """golden/registry 是 Flash 自有静态段基线，不含完整 tools 母段。

    tools 域在 Flash 侧只剥离出 300/330/340（去重规则 / bash / 工具结果末条），完整工具
    目录母段（``tools`` + ``execution_principles``）由 FlashToolGuideRail 运行时保留注入。
    这条测试防止有人把 golden 误当成「完整最终 tools 提示词」去对账或裁剪。
    """
    for language in LANGUAGES:
        names = {s.name for s in assemble(language)}
        assert "tools" not in names
        assert "execution_principles" not in names
        # 300/330/340 才是 Flash 自有剥离段，缺一即工具域提取不完整。
        assert {"tool_selection", "bash_discipline", "tool_result"} <= names


# ── Step 3：声明式注册表（registry.py）与 reconcile 的门控/元数据校验 ──────────────
def test_registry_blocks_are_declarative() -> None:
    assert FLASH_PROMPT_REGISTRY, "registry must not be empty"
    keys = []
    for block in FLASH_PROMPT_REGISTRY:
        keys.append(block.key)
        assert block.owner == "flash", f"block {block.key} owner must be flash"
        assert block.carrier == "system", f"block {block.key} carrier must be system"
        assert block.supersedes, f"block {block.key} must declare supersedes"
        assert block.render is not None, f"block {block.key} must declare render"
    assert keys == ["safety", "response", "tools", "todo", "subagent"]


def test_registry_reconcile_orders_by_priority() -> None:
    ctx = build_default_context("cn")
    sections = reconcile(ctx)
    priorities = [s.priority for s in sections]
    assert priorities == sorted(priorities)


def test_registry_reconcile_is_deterministic() -> None:
    ctx = build_default_context("cn")
    first = reconcile(ctx)
    second = reconcile(ctx)
    assert [(s.priority, s.name) for s in first] == [
        (s.priority, s.name) for s in second
    ]
    assert [s.content for s in first] == [s.content for s in second]


def test_registry_enabled_when_gates_tool_domains() -> None:
    # 空工具面：todo/subagent 两域被 enabled_when 门控，只剩 safety/response/tools。
    ctx = PromptRenderContext(lang="cn", enabled_tools=frozenset())
    sections = reconcile(ctx)
    assert [(s.priority, s.name) for s in sections] == [
        (30, "uncertainty_failure"),
        (100, "safety_general"),
        (110, "refusal_behavior"),
        (120, "deletion_protocol"),
        (300, "tool_selection"),
        (330, "bash_discipline"),
        (340, "tool_result"),
        (410, "thinking_discipline"),
        (500, "language_output"),
        (510, "response"),
        (520, "final_visible_reply"),
        (540, "runtime.model_answer_policy"),
    ]
