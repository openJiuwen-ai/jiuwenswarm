# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboAskUserRail 门控分支（S_06 边界 #19/#20——此前零测试）。

防护意图：ask_user rail 是 HITL 中断-恢复状态机的入口（根因 1，24 个 PR
的领域）。三个分支此前无任何测试触达：
  1. 非引导模式 + preview 类问答 → 跳过不弹卡（PR 5093 修复的行为）
  2. 首次调用 → 触发中断（HITL 主路径）
  3. 不认识的 user_input 形态 → re-interrupt（S_06 边界 #20：重问帧
     可能被 sidecar dedup 吞卡的已知风险点）

spec S_06 声称"ask_user_rail 单测已覆盖"与实际不符——本文件补齐该缺口。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    InterruptResult,
    RejectResult,
)

from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
    set_interactive_ask,
)
from jiuwenswarm.server.runtime.skill_turbo.rails.ask_user_rail import (
    SkillTurboAskUserRail,
)


def _tool_call(questions: list[dict[str, Any]] | None = None) -> ToolCall:
    if questions is None:
        return ToolCall(id="tc_ask", type="function", name="ask_user", arguments="{}")
    return ToolCall(
        id="tc_ask",
        type="function",
        name="ask_user",
        arguments=json.dumps({"query": "请确认", "questions": questions}),
    )


_PREVIEW_QUESTIONS = [
    {
        "question": "请确认大纲",
        "header": "大纲审阅",
        "options": [{"label": "确认", "description": "继续"}],
        "preview": {"text": "# 一级标题\n## 二级标题"},
    }
]

_PLAIN_QUESTIONS = [
    {
        "question": "选择主题方向",
        "header": "主题",
        "options": [{"label": "A", "description": "a"}],
    }
]


@pytest.fixture
def non_interactive():
    """显式关闭引导模式（rail 的 skip 分支判定条件）。"""
    token = set_interactive_ask(False)
    yield
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        reset_interactive_ask,
    )

    reset_interactive_ask(token)


@pytest.fixture
def interactive():
    token = set_interactive_ask(True)
    yield
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        reset_interactive_ask,
    )

    reset_interactive_ask(token)


class TestNonInteractivePreviewSkip:
    @pytest.mark.asyncio
    async def test_preview_ask_skipped_in_non_interactive_mode(self, non_interactive):
        """非引导模式 + preview 问答 → 返回 skipped 占位，不中断、不弹卡（PR 5093 行为锁定）。"""
        rail = SkillTurboAskUserRail()
        decision = await rail.resolve_interrupt(MagicMock(), _tool_call(_PREVIEW_QUESTIONS), None)

        assert isinstance(decision, RejectResult)
        tool_result = decision.tool_result
        assert isinstance(tool_result, dict)
        assert tool_result["status"] == "skipped"
        assert tool_result["answers"] == []

    @pytest.mark.asyncio
    async def test_plain_ask_still_interrupts_in_non_interactive_mode(self, non_interactive):
        """非引导模式 + 无 preview 普通问答（如需求收集）仍照常中断——skip 只针对内容确认类。"""
        rail = SkillTurboAskUserRail()
        decision = await rail.resolve_interrupt(MagicMock(), _tool_call(_PLAIN_QUESTIONS), None)

        assert isinstance(decision, InterruptResult), "无 preview 的问答在非引导模式不得被静默跳过"

    @pytest.mark.asyncio
    async def test_preview_ask_interrupts_in_interactive_mode(self, interactive):
        """引导模式 + preview → 照常中断弹卡（大纲审阅主路径）。"""
        rail = SkillTurboAskUserRail()
        decision = await rail.resolve_interrupt(MagicMock(), _tool_call(_PREVIEW_QUESTIONS), None)

        assert isinstance(decision, InterruptResult)


class TestFirstInterrupt:
    @pytest.mark.asyncio
    async def test_first_call_interrupts(self, interactive):
        """首次调用（user_input=None）→ 触发 harness 原生 HITL 中断。"""
        rail = SkillTurboAskUserRail()
        decision = await rail.resolve_interrupt(MagicMock(), _tool_call(_PLAIN_QUESTIONS), None)
        assert isinstance(decision, InterruptResult)


class TestReinterruptOnUnrecognizedInput:
    @pytest.mark.asyncio
    async def test_unrecognized_user_input_reinterrupts(self, interactive):
        """不认识的 user_input 形态 → re-interrupt 等待重新作答（S_06 边界 #20）。"""
        rail = SkillTurboAskUserRail()
        decision = await rail.resolve_interrupt(MagicMock(), _tool_call(_PLAIN_QUESTIONS), 42)

        assert isinstance(decision, InterruptResult), "解析失败的作答必须重新中断，不得当已作答处理"


class TestParseUserAnswers:
    """_parse_user_answers 支持形态（resume 回填链路，S_06 P3）。"""

    def test_list_of_dicts_normalizes_selected_options(self):
        answers = [{"question": "Q", "selected_options": "A", "custom_input": "x"}]
        parsed = SkillTurboAskUserRail._parse_user_answers(answers)
        assert parsed == [{"question": "Q", "selected_options": ["A"], "custom_input": "x"}]

    def test_wrapped_answers_list(self):
        parsed = SkillTurboAskUserRail._parse_user_answers({"answers": [{"question": "Q", "selected_options": ["B"]}]})
        assert parsed == [{"question": "Q", "selected_options": ["B"]}]

    def test_answers_key_value_mapping(self):
        parsed = SkillTurboAskUserRail._parse_user_answers({"answers": {"主题": "科技风"}})
        assert parsed == [{"question": "主题", "selected_options": ["科技风"]}]

    def test_bare_key_value_mapping(self):
        parsed = SkillTurboAskUserRail._parse_user_answers({"页数": "10 页"})
        assert parsed == [{"question": "页数", "selected_options": ["10 页"]}]

    def test_unrecognized_returns_none(self):
        assert SkillTurboAskUserRail._parse_user_answers(42) is None
        assert SkillTurboAskUserRail._parse_user_answers(None) is None
        assert SkillTurboAskUserRail._parse_user_answers({"answers": 42}) is None
