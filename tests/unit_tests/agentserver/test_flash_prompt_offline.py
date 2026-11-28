# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 离线组装、双语快照导出和注册表行为测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flash_offline_stub import install_offline_import_stubs

# 必须在 import 任何 flash 子模块之前执行：flash 包 ``__init__`` 会 eager import
# rails，缺 agent_shield 时直接拉起 openjiuwen.harness.security 而断链。
install_offline_import_stubs()

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
    write_prompt_snapshot,
)
from jiuwenswarm.agents.harness.flash.sections import (  # noqa: E402
    FLASH_SECTION_BANDS,
    section_in_band,
)


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
def test_offline_snapshot_export_matches_sections(language: str, tmp_path: Path) -> None:
    sections = assemble(language)
    snapshot = build_prompt_snapshot(sections, language=language)
    output, _ = write_prompt_snapshot(snapshot, tmp_path)
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert [(s["priority"], s["name"], s["text"]) for s in exported["sections"]] == [
        (s.priority, s.name, s.content[language]) for s in sections
    ]


@pytest.mark.parametrize("language", LANGUAGES)
def test_offline_section_carriers(language: str) -> None:
    snapshot = build_prompt_snapshot(assemble(language), language=language)
    for section in snapshot.sections:
        assert section.registered
        assert section.carrier == ("attachment" if section.name == "browser_routing" else "system")


def test_safety_relay_merge_present() -> None:
    cn = build_prompt_snapshot(assemble("cn"), language="cn")
    text = {s.name: s.text for s in cn.sections}["deletion_protocol"]
    assert "移入系统回收站" in text
    assert "同卷软删除回退" in text


def test_deletion_scope_disambiguation_present() -> None:
    """「整理」边界：删除规范里的「整理/归档」仅指用户文件，内部目录整理属内部操作。"""
    cn = build_prompt_snapshot(assemble("cn"), language="cn")
    text_cn = {s.name: s.text for s in cn.sections}["deletion_protocol"]
    assert "仅指移动用户的文件" in text_cn
    assert "Agent 内部数据目录的整理属于内部操作" in text_cn

    en = build_prompt_snapshot(assemble("en"), language="en")
    text_en = {s.name: s.text for s in en.sections}["deletion_protocol"]
    assert "refers only to moving the user's files" in text_en
    assert "internal data directories" in text_en


def test_response_language_field_note_removed() -> None:
    """语言字段说明已删：语言口径只看运行时语言；JSON 示例字段本身保留。"""
    for language in LANGUAGES:
        snapshot = build_prompt_snapshot(assemble(language), language=language)
        text = {s.name: s.text for s in snapshot.sections}["response"]
        assert '"preferred_response_language"' in text
        assert "用户期望的回复语言" not in text
        assert "The user's preferred response language" not in text


def test_final_reply_restate_disambiguation_present() -> None:
    """520「完整复述」与思考纪律「严禁复述上下文」作用域消歧，且不引用段号。"""
    cn = build_prompt_snapshot(assemble("cn"), language="cn")
    text_cn = {s.name: s.text for s in cn.sections}["final_visible_reply"]
    assert "与「思考纪律」一节不冲突" in text_cn
    assert "reasoning_content" in text_cn
    assert "410" not in text_cn

    en = build_prompt_snapshot(assemble("en"), language="en")
    text_en = {s.name: s.text for s in en.sections}["final_visible_reply"]
    assert '"Thinking discipline" section' in text_en
    assert "reasoning_content" in text_en
    assert " 410" not in text_en


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


def test_tool_selection_exploration_vs_execution() -> None:
    """300 段：只读探索放开并行、有副作用写精准；旧的「一个问题一个工具」一刀切已删。"""
    cn = build_prompt_snapshot(assemble("cn"), language="cn")
    cn_text = {s.name: s.text for s in cn.sections}["tool_selection"]
    assert "只读探索操作" in cn_text
    assert "有副作用的操作" in cn_text
    assert "默认一个问题一个工具" not in cn_text

    en = build_prompt_snapshot(assemble("en"), language="en")
    en_text = {s.name: s.text for s in en.sections}["tool_selection"]
    assert "Read-only exploration calls" in en_text
    assert "Side-effecting calls" in en_text
    assert "Default to one tool per question" not in en_text


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
