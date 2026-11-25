# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRelayMergeRail 单元测试（M4 relay 去重归并 + 静态末位收口）。

锁定：
1. ``_strip_relay_duplicates`` 剥除 ``request_system_prompt`` 末尾的 ``[文件输出约束]`` 块，
   保留 ``[Personality Override]`` 等前序与 ``[记忆纠正规则]``（纯函数层不碰后者）；
2. 守卫：``[文件输出约束]`` 锚点缺失 / 空内容 → 返回 None（整体回退不盲切）；
3. 剥除结果无尾部空白；纯函数保留 ``[记忆纠正规则]`` 原块（工具名改写在其后的 rail 层）；
4. rail 每轮**无条件** remove + 重加：段号收敛到 ``_PRIO_REQUEST``（999 静态末位），
   解除与 refusal_behavior@110 的撞号——即使守卫命中或无重复块可剥也一样重排；
5. 幂等安全：已剥净的内容不再改动；守卫命中用原内容重加、不盲切。
6. 冲突修复（relay 注入内容）：
   - ``[记忆纠正规则]`` 第 6 条（删除=回收站）删除（120 已覆盖且更精确）；
   - 旧记忆工具名按 FlashMemoryRail 映射表改写为 ``memory(mode=…)``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.prompt.content.relay import (
    _PRIO_REQUEST,
    _rewrite_memory_tool_names,
    _strip_memory_rule6,
)
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

    def test_pure_strip_keeps_memory_block_verbatim(self) -> None:
        # 纯函数层只管 [文件输出约束]；[记忆纠正规则] 原块（含旧工具名、第 6 条）保留，
        # 由 rail 层再做工具名改写与第 6 条删除（见 TestConflictFixes）。
        stripped = _strip_relay_duplicates(_full())
        assert "read_memory" in stripped
        assert "edit_memory" in stripped
        assert "移入回收站即为合规的软删除" in stripped

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


class TestConflictFixes:
    """relay 注入内容与 flash 自有段的冲突修复（纯函数层）。"""

    def test_strip_memory_rule6(self) -> None:
        text, hit = _strip_memory_rule6(_MC)
        assert hit
        assert "移入回收站即为合规的软删除" not in text
        assert "1. 本轮一律按用户当前指令执行。" in text

    def test_strip_memory_rule6_renumbered(self) -> None:
        text, hit = _strip_memory_rule6(
            "[记忆纠正规则]\n7. 用户要求删除时，移入回收站即为合规的软删除。",
        )
        assert hit
        assert "软删除" not in text
        assert "[记忆纠正规则]" in text

    def test_rewrite_memory_tool_names(self) -> None:
        text, hit = _rewrite_memory_tool_names(
            "用 read_memory 定位，再用 edit_memory 改写。",
            (("read_memory", "memory(mode=read)"), ("edit_memory", "memory(mode=edit)")),
        )
        assert hit
        assert "read_memory" not in text and "edit_memory" not in text
        assert "memory(mode=read)" in text and "memory(mode=edit)" in text

    def test_rewrite_memory_tool_names_noop(self) -> None:
        text, hit = _rewrite_memory_tool_names(
            "已统一", (("read_memory", "memory(mode=read)"),),
        )
        assert not hit and text == "已统一"


class TestRailConflictFixes:
    @pytest.mark.asyncio
    async def test_drops_rule6_and_rewrites_names(self) -> None:
        agent = _FakeAgent([_sec("request_system_prompt", 110, _full())])
        rail = FlashRelayMergeRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))

        text = agent.system_prompt_builder.get_section("request_system_prompt").content["cn"]
        assert "[记忆纠正规则]" in text  # 块本身保留（1–5 条 flash 无对应物）
        assert "移入回收站即为合规的软删除" not in text
        assert "read_memory" not in text and "edit_memory" not in text
        assert "memory(mode=read)" in text and "memory(mode=edit)" in text
