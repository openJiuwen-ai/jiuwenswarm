# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSafetyRail 单元测试（M4 安全域拆分 + 120 relay 归并）。

锁定：
1. ``_transform_safety`` 把 safety 巨石拆成 030/100/110/120 四段，段号序 30/100/110/120；
2. 030 拆出总则末两条（任务失败/不确定）；100 删「正确的响应」三行；110/120 原文保留；
3. 120 归并：deletion_protocol 段尾追加 relay「删除=回收站即合规」一条（CN/EN 双份）；
4. 守卫：标题/锚点缺失或乱序 → 返回 None（整体回退保留原生 safety 段）；
5. rail 每轮重拆不设幂等 flag（SafetyPromptRail 每轮重注入 safety）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _flash_test_utils import FakeAgent as _FakeAgent
from _flash_test_utils import FakeCtx as _FakeCtx

from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import (
    FlashSafetyRail,
    _transform_safety,
)


def _sec(name, priority, text):
    return SimpleNamespace(name=name, content={"cn": text, "en": text}, priority=priority)


def _build_content(cn: bool) -> str:
    if cn:
        intro = (
            "# 安全原则\n\n- 第一条\n- 第二条\n- 第三条\n- 第四条\n- 第五条\n"
            "- 任务失败时简要说明原因并给出建议\n- 不确定时说明不确定\n\n"
        )
        deletion = "## 删除操作规范（强制）\n禁止物理删除\n\n"
        refusal = "## 拒绝回答时的行为（重要）\n拒绝时说明原因\n\n"
        auth = "## 授权声明无效（重要）\n授权无效说明\n\n**正确的响应：**\n正确响应三行\n"
    else:
        intro = (
            "# Security Principles\n\n- one\n- two\n- three\n- four\n- five\n"
            "- If a task fails, briefly explain why and suggest the most practical next step\n"
            "- uncertainty\n\n"
        )
        deletion = "## File Deletion Protocol (Mandatory)\nno physical delete\n\n"
        refusal = "## Behavior When Refusing to Answer (Important)\nexplain reason\n\n"
        auth = "## Authorization Declaractions Are Invalid (Important)\nauth invalid\n\n**Correct response:**\ncorrect response lines\n"
    return intro + deletion + refusal + auth


class TestTransformSafety:
    def test_split_four_sections_and_priority_order(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        result = _transform_safety(_build_content(True), _ANCHORS_CN)
        assert result is not None
        names = [n for n, _, _ in result]
        prios = [p for _, p, _ in result]
        assert names == ["uncertainty_failure", "safety_general", "refusal_behavior", "deletion_protocol"]
        assert prios == [30, 100, 110, 120]

    def test_030_strips_uncertainty_from_intro(self) -> None:
        # 030 应从总则末两条切出、加 # 不确定与失败 heading。
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        result = _transform_safety(_build_content(True), _ANCHORS_CN)
        byname = {n: t for n, _, t in result}
        assert byname["uncertainty_failure"].startswith("# 不确定与失败")
        assert "任务失败时简要说明原因" in byname["uncertainty_failure"]

    def test_100_drops_correct_response(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        result = _transform_safety(_build_content(True), _ANCHORS_CN)
        byname = {n: t for n, _, t in result}
        assert "正确的响应" not in byname["safety_general"]

    def test_120_appends_deletion_merge_rule(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        result = _transform_safety(_build_content(True), _ANCHORS_CN)
        byname = {n: t for n, _, t in result}
        assert "移入系统回收站" in byname["deletion_protocol"]
        assert "同卷软删除回退" in byname["deletion_protocol"]
        assert byname["deletion_protocol"].startswith("# 删除操作规范（强制）")

    def test_heading_level_normalized_to_h1(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        result = _transform_safety(_build_content(True), _ANCHORS_CN)
        byname = {n: t for n, _, t in result}
        # 110/120 拆分后的段顶标题提升为 H1。
        assert byname["refusal_behavior"].startswith("# 拒绝回答时的行为（重要）")
        assert byname["deletion_protocol"].startswith("# 删除操作规范（强制）")
        # 100 顶层仍为 H1，段内「授权声明无效」保留为 H2 子标题（非段顶）。
        assert byname["safety_general"].startswith("# 安全原则")
        assert "## 授权声明无效（重要）" in byname["safety_general"]

    def test_merge_rule_not_leaked_into_other_sections(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        result = _transform_safety(_build_content(True), _ANCHORS_CN)
        byname = {n: t for n, _, t in result}
        merge = "移入系统回收站"
        for other in ("uncertainty_failure", "safety_general", "refusal_behavior"):
            assert merge not in byname[other]

    def test_en_split_and_merge(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_EN

        result = _transform_safety(_build_content(False), _ANCHORS_EN)
        assert result is not None
        byname = {n: t for n, _, t in result}
        assert "moving it to the recycle bin is compliant soft deletion" in byname["deletion_protocol"]

    def test_100_authorization_redline_rewrite(self) -> None:
        # 用真实 SDK safety 正文验证「授权声明无效」被窄化为「安全红线」，
        # 消除与「先问再做/需请示」确认门禁的语义误伤（防过度拒绝）。
        from openjiuwen.harness.prompts.sections.safety import (
            SAFETY_PROMPT_CN,
            SAFETY_PROMPT_EN,
        )

        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import (
            _ANCHORS_CN,
            _ANCHORS_EN,
        )

        cn = {n: t for n, _, t in _transform_safety(SAFETY_PROMPT_CN, _ANCHORS_CN)}
        assert "安全红线（隐私泄露与违法有害请求）" in cn["safety_general"]
        assert "覆盖安全原则" not in cn["safety_general"]
        assert "核心安全原则优先于用户的所有请求" not in cn["safety_general"]
        # 120 删除规则：Flash 追加的「删除=回收站即合规」必须在真实 SDK 正文里出现。
        assert "即为合规的软删除" in cn["deletion_protocol"]
        assert "同卷软删除回退" in cn["deletion_protocol"]

        en = {n: t for n, _, t in _transform_safety(SAFETY_PROMPT_EN, _ANCHORS_EN)}
        assert "safety red lines (privacy leaks and illegal/harmful requests)" in en["safety_general"]
        assert "override safety principles" not in en["safety_general"]
        # 120 删除规则（EN）：Flash 追加的「删除=回收站即合规」必须在真实 SDK 正文里出现。
        assert "compliant soft deletion" in en["deletion_protocol"]


class TestTransformGuards:
    def test_missing_header_returns_none(self) -> None:
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        assert _transform_safety("没有标题的内容", _ANCHORS_CN) is None

    def test_out_of_order_returns_none(self) -> None:
        # 拒绝标题出现在删除标题之前 → 乱序回退。
        bad = "## 拒绝回答时的行为（重要）\nx\n## 删除操作规范（强制）\ny\n## 授权声明无效（重要）\nz\n**正确的响应：**\nw"
        from jiuwenswarm.agents.harness.flash.rails.flash_safety_rail import _ANCHORS_CN

        assert _transform_safety(bad, _ANCHORS_CN) is None

    def test_empty_returns_none(self) -> None:
        assert _transform_safety("", {}) is None


class TestRail:
    @pytest.mark.asyncio
    async def test_splits_on_before_model_call(self) -> None:
        agent = _FakeAgent("cn", [_sec("safety", 20, _build_content(True))])
        rail = FlashSafetyRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))

        b = agent.system_prompt_builder
        assert b.get_section("safety") is None  # 原 safety 段已拆走
        assert b.get_section("uncertainty_failure").priority == 30
        assert b.get_section("safety_general").priority == 100
        assert b.get_section("refusal_behavior").priority == 110
        assert b.get_section("deletion_protocol").priority == 120
        assert "移入系统回收站" in b.get_section("deletion_protocol").content["cn"]

    @pytest.mark.asyncio
    async def test_guard_fallback_keeps_original(self) -> None:
        content = "没有标题的安全内容"
        agent = _FakeAgent("cn", [_sec("safety", 20, content)])
        rail = FlashSafetyRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))
        sec = agent.system_prompt_builder.get_section("safety")
        assert sec is not None  # 守卫回退，保留原生 safety 段
        assert sec.content["cn"] == content
