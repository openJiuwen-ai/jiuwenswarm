# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_protocol 段抽取纯函数测试（flash 710/730 共用）。

锁定：
1. ``_extract_execution_rules`` 抽出 9 条执行规范：起于「随后按…」，不含加载前置两块，
   末尾 ⚠️ 消息语义并入第 9 条（作为其末条 bullet）；
2. ``_extract_loading_blocks`` 抽出「经验查询前置 + 加载 SKILL.md」两块，不含执行锚点；
3. 守卫：锚点/标题缺失 → None（不盲切）。
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
    _build_skill_protocol_section_text,
)
from jiuwenswarm.agents.harness.flash.rails.flash_skill_rail import (
    _extract_execution_rules,
    _extract_loading_blocks,
)


class TestExtractExecutionRules:
    def test_cn_rules_exclude_loading_preamble(self) -> None:
        rules = _extract_execution_rules(_build_skill_protocol_section_text("cn"), "cn")
        assert rules is not None
        assert rules.startswith("随后按 SKILL 工作流执行")
        assert "### 经验查询与技能流程的顺序" not in rules
        assert "### 加载 SKILL.md 正文" not in rules

    def test_cn_has_all_nine_rules(self) -> None:
        rules = _extract_execution_rules(_build_skill_protocol_section_text("cn"), "cn")
        assert rules is not None
        for i in range(1, 10):
            assert f"{i}. " in rules

    def test_cn_warning_folded_into_rule_nine(self) -> None:
        rules = _extract_execution_rules(_build_skill_protocol_section_text("cn"), "cn")
        assert rules is not None
        # ⚠️ 作为第 9 条的 bullet（"   - ⚠️ …"），不再是段尾独立段。
        assert "   - ⚠️ 用户发 N 条消息 ≠ N 个并发任务" in rules
        assert rules.rstrip().endswith("是错误推理。")

    def test_en_rules_and_warning(self) -> None:
        rules = _extract_execution_rules(_build_skill_protocol_section_text("en"), "en")
        assert rules is not None
        assert rules.startswith("Then execute the workflow")
        assert "   - ⚠️ N user messages ≠ N parallel tasks" in rules
        assert "### Experience lookup before the skill workflow" not in rules

    def test_guard_on_missing_anchor(self) -> None:
        assert _extract_execution_rules("无锚点的正文", "cn") is None
        assert _extract_execution_rules("", "cn") is None


class TestExtractLoadingBlocks:
    def test_cn_returns_both_blocks_without_execution_part(self) -> None:
        block = _extract_loading_blocks(_build_skill_protocol_section_text("cn"), "cn")
        assert block is not None
        assert "### 经验查询与技能流程的顺序" in block
        assert "### 加载 SKILL.md 正文（禁止用 bash 执行工具名）" in block
        assert "随后按 SKILL 工作流执行" not in block

    def test_en_returns_both_blocks(self) -> None:
        block = _extract_loading_blocks(_build_skill_protocol_section_text("en"), "en")
        assert block is not None
        assert "### Experience lookup before the skill workflow" in block
        assert "### Load SKILL.md body" in block
        assert "Then execute the workflow" not in block

    def test_guard_on_missing_heading(self) -> None:
        assert _extract_loading_blocks("只有一段正文", "cn") is None
        assert _extract_loading_blocks("", "cn") is None