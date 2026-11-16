# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashResponseRail 单元测试（M4 响应/交互/输出域迁号 + 改写）。

锁定：
1. 410 思考纪律迁号 + 三矛盾修正（修复.md ①-⑤），改写为 06 目标正文（单语言注入）；
2. 500/510/520/540 迁号 + 改写为 06 目标正文（单语言注入 ``{language: text}``）；
3. 源段缺失即跳过（不凭空新增段，带守卫）；
4. 每轮重迁号（不设幂等 flag——源 rail 每轮重注入）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _flash_test_utils import FakeAgent as _FakeAgent
from _flash_test_utils import FakeCtx as _FakeCtx

from jiuwenswarm.agents.harness.flash.rails.flash_response_rail import (
    _FINAL_CN,
    _FINAL_EN,
    _LANGUAGE_CN,
    _LANGUAGE_EN,
    _MODEL_CN,
    _MODEL_EN,
    _RESPONSE_CN,
    _RESPONSE_EN,
    _THINKING_CN,
    FlashResponseRail,
)


def _sec(name, priority, cn, en=None):
    if en is None:
        en = cn
    return SimpleNamespace(name=name, content={"cn": cn, "en": en}, priority=priority)


_SOURCE_THINKING_CN = "写文件轮：任何写文件动作前先在本轮思考里自述一遍目的与路径。"
_SOURCE_THINKING_EN = "write-file turn: before any write action, restate purpose and path in this turn's thinking."


def _full_agent(language: str = "cn") -> _FakeAgent:
    return _FakeAgent(language, [
        _sec("thinking_discipline", 115, _SOURCE_THINKING_CN, _SOURCE_THINKING_EN),
        _sec("language_output", 93, "永远用简体中文", "Always respond in Chinese"),
        _sec("response", 60, "response 原文", "response source"),
        _sec("final_visible_reply", 120, "final 原文", "final source"),
        _sec("runtime.model_answer_policy", 95, "model 原文", "model source"),
    ])


class TestMigrate:
    def test_410_rewrites_to_target_body(self) -> None:
        agent = _full_agent("cn")
        rail = FlashResponseRail()
        rail.init(agent)
        rail._migrate(agent.system_prompt_builder)

        sec = agent.system_prompt_builder.get_section("thinking_discipline")
        assert sec.priority == 410
        # 三矛盾修正：改写为 06 目标正文（单语言注入，不再逐字复用源 content dict）。
        assert sec.content == {"cn": _THINKING_CN}

    def test_rewritten_sections_use_target_body_cn(self) -> None:
        agent = _full_agent("cn")
        rail = FlashResponseRail()
        rail.init(agent)
        rail._migrate(agent.system_prompt_builder)

        b = agent.system_prompt_builder
        assert b.get_section("language_output").priority == 500
        assert b.get_section("language_output").content == {"cn": _LANGUAGE_CN}
        assert b.get_section("response").priority == 510
        assert b.get_section("response").content == {"cn": _RESPONSE_CN}
        assert b.get_section("final_visible_reply").priority == 520
        assert b.get_section("final_visible_reply").content == {"cn": _FINAL_CN}
        assert b.get_section("runtime.model_answer_policy").priority == 540
        assert b.get_section("runtime.model_answer_policy").content == {"cn": _MODEL_CN}

    def test_rewritten_sections_use_target_body_en(self) -> None:
        agent = _full_agent("en")
        rail = FlashResponseRail()
        rail.init(agent)
        rail._migrate(agent.system_prompt_builder)

        b = agent.system_prompt_builder
        assert b.get_section("language_output").content == {"en": _LANGUAGE_EN}
        assert b.get_section("response").content == {"en": _RESPONSE_EN}
        assert b.get_section("final_visible_reply").content == {"en": _FINAL_EN}
        assert b.get_section("runtime.model_answer_policy").content == {"en": _MODEL_EN}

    def test_drops_absolute_language_claim(self) -> None:
        agent = _full_agent("cn")
        rail = FlashResponseRail()
        rail.init(agent)
        rail._migrate(agent.system_prompt_builder)
        text = agent.system_prompt_builder.get_section("language_output").content["cn"]
        assert "永远" not in text

    def test_drops_line_count_dead_cap(self) -> None:
        agent = _full_agent("cn")
        rail = FlashResponseRail()
        rail.init(agent)
        rail._migrate(agent.system_prompt_builder)
        text = agent.system_prompt_builder.get_section("final_visible_reply").content["cn"]
        assert "50-70" not in text
        assert "按交付物复杂度与受众需求完整呈现" in text


class TestSourceMissingGuard:
    def test_missing_source_skips_without_orphan(self) -> None:
        # 只给 thinking_discipline 源段，其余 4 段缺失 → 不凭空新增。
        agent = _FakeAgent("cn", [_sec("thinking_discipline", 115, _SOURCE_THINKING_CN)])
        rail = FlashResponseRail()
        rail.init(agent)
        rail._migrate(agent.system_prompt_builder)

        b = agent.system_prompt_builder
        assert b.get_section("thinking_discipline").priority == 410
        assert b.get_section("language_output") is None
        assert b.get_section("response") is None
        assert b.get_section("final_visible_reply") is None
        assert b.get_section("runtime.model_answer_policy") is None


class TestRail:
    @pytest.mark.asyncio
    async def test_migrates_on_before_model_call(self) -> None:
        agent = _full_agent("cn")
        rail = FlashResponseRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))
        assert agent.system_prompt_builder.get_section("thinking_discipline").priority == 410
        assert agent.system_prompt_builder.get_section("response").priority == 510

    @pytest.mark.asyncio
    async def test_noop_when_builder_missing(self) -> None:
        rail = FlashResponseRail()
        rail.init(SimpleNamespace())  # 无 system_prompt_builder
        await rail.before_model_call(SimpleNamespace(agent=SimpleNamespace()))  # 不抛
