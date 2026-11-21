# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRelayMergeRail 单元测试（M4 relay 去重归并 + 静态末位收口）。

锁定：
1. ``_strip_relay_duplicates`` 剥除 ``request_system_prompt`` 末尾的 ``[文件输出约束]`` 块，
   保留 ``[Personality Override]`` 等前序与 ``[记忆纠正规则]``（记忆域已回退 deep 原样）；
2. 守卫：``[文件输出约束]`` 锚点缺失 / 空内容 → 返回 None（整体回退不盲切）；
3. 剥除结果无尾部空白；``[记忆纠正规则]`` 保留其旧工具名 read_memory/edit_memory/write_memory
   （与 stock MemoryRail 五件套一致）；
4. rail 每轮**无条件** remove + 重加：段号收敛到 ``_PRIO_REQUEST``（999 静态末位），
   解除与 refusal_behavior@110 的撞号——即使守卫命中或无重复块可剥也一样重排；
5. 幂等安全：已剥净的内容不再改动；守卫命中用原内容重加、不盲切。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.prompt.content.relay import _PRIO_REQUEST
from jiuwenswarm.agents.harness.flash.rails.flash_relay_merge_rail import (
    FlashRelayMergeRail,
    _strip_relay_duplicates,
)


class _FakeBuilder:
    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.sections: dict[str, SimpleNamespace] = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name) -> None:
        self.sections.pop(name, None)

    def get_section(self, name):
        return self.sections.get(name)


class _FakeAgent:
    def __init__(self, sections=None) -> None:
        self.system_prompt_builder = _FakeBuilder()
        for s in sections or []:
            self.system_prompt_builder.add_section(s)


class _FakeCtx:
    def __init__(self, agent) -> None:
        self.agent = agent


def _sec(name, priority, text):
    return SimpleNamespace(name=name, content={"cn": text, "en": text}, priority=priority)


_PERSONA = "[Personality Override]\n你是 X。"
_HINTS = "## 文件生成任务的开头规范\n开头规范内容\n## 再改改两轮卡\n两轮卡内容"
_PDF = "## PDF 转 Markdown 硬规则\n禁止自己写脚本"
_WS = "[文件输出约束]\n生成或写出文件时，一律写入当前工作目录内并使用相对路径。\n当前工作目录：/workspace/2026"
_MC = "[记忆纠正规则]\n1. 本轮一律按用户当前指令执行。\n2. 用 read_memory 定位旧条目，再用 edit_memory 改写。\n6. 用户要求删除文件时，移入回收站即为合规的软删除。"


def _full() -> str:
    return "\n\n".join([_PERSONA, _HINTS, _PDF, _WS, _MC])


class TestStripRelayDuplicates:
    def test_strips_trailing_two_blocks(self) -> None:
        stripped = _strip_relay_duplicates(_full())
        assert stripped is not None
        assert "[Personality Override]" in stripped
        assert "文件生成任务" in stripped
        assert "PDF 转 Markdown" in stripped
        assert "[文件输出约束]" not in stripped
        assert "[记忆纠正规则]" in stripped

    def test_keeps_legacy_memory_tool_names(self) -> None:
        # 记忆域已回退 deep 原样：stock 五件套工具名 read_memory/edit_memory/write_memory
        # 与 relay [记忆纠正规则] 原块一致，故保留（不再被 640 归并剥除）。
        stripped = _strip_relay_duplicates(_full())
        assert "read_memory" in stripped
        assert "edit_memory" in stripped

    def test_no_trailing_whitespace(self) -> None:
        stripped = _strip_relay_duplicates(_full())
        assert stripped == stripped.rstrip()

    def test_missing_memory_correction_falls_back(self) -> None:
        # 无 [记忆纠正规则] 收尾 → 无法确定 [文件输出约束] 块的结束边界，
        # 不盲删到段尾，整体回退（None）。
        stripped = _strip_relay_duplicates("\n\n".join([_PERSONA, _WS]))
        assert stripped is None

    def test_memory_correction_before_workspace_falls_back(self) -> None:
        # [记忆纠正规则] 出现在 [文件输出约束] 之前（布局漂移）→ 找不到合法的
        # 收尾锚点，整体回退（None），不误删 [记忆纠正规则] 之后的内容。
        stripped = _strip_relay_duplicates("\n\n".join([_PERSONA, _MC, _WS]))
        assert stripped is None

    def test_no_anchor_returns_none(self) -> None:
        assert _strip_relay_duplicates("\n\n".join([_PERSONA, _HINTS])) is None

    def test_empty_returns_none(self) -> None:
        assert _strip_relay_duplicates("") is None


class TestRail:
    @pytest.mark.asyncio
    async def test_strips_and_repins_on_before_model_call(self) -> None:
        agent = _FakeAgent([_sec("request_system_prompt", 110, _full())])
        rail = FlashRelayMergeRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))

        sec = agent.system_prompt_builder.get_section("request_system_prompt")
        assert sec is not None
        text = sec.content["cn"]
        assert "[文件输出约束]" not in text
        assert "[记忆纠正规则]" in text
        assert "[Personality Override]" in text
        assert sec.priority == _PRIO_REQUEST  # 110 → 999 静态末位，解除撞号

    @pytest.mark.asyncio
    async def test_noop_when_section_missing(self) -> None:
        agent = _FakeAgent()
        rail = FlashRelayMergeRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))  # 不抛
        assert agent.system_prompt_builder.sections == {}

    @pytest.mark.asyncio
    async def test_guard_fallback_repins_original(self) -> None:
        # 内容缺 [文件输出约束] → 守卫回退：内容原样重加，段号仍无条件收敛到 999。
        content = "\n\n".join([_PERSONA, _HINTS])
        agent = _FakeAgent([_sec("request_system_prompt", 110, content)])
        rail = FlashRelayMergeRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))
        sec = agent.system_prompt_builder.get_section("request_system_prompt")
        assert sec.content["cn"] == content
        assert sec.priority == _PRIO_REQUEST

    @pytest.mark.asyncio
    async def test_unconditional_repin_even_when_no_duplicates(self) -> None:
        # 无重复块可剥（内容不变）时同样重排到 999——撞号修复不依赖锚点是否命中。
        content = "\n\n".join([_PERSONA, _HINTS, _PDF])
        agent = _FakeAgent([_sec("request_system_prompt", 110, content)])
        rail = FlashRelayMergeRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))
        sec = agent.system_prompt_builder.get_section("request_system_prompt")
        assert sec.content["cn"] == content
        assert sec.priority == _PRIO_REQUEST
