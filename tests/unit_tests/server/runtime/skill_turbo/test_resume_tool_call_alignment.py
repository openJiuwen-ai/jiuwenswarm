# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resume replay: 当 ask_user 等工具因非确定性参数导致 tool_call_id 变化时，
仍能按 (tool_name, idx 段) 对齐注入 user_input，避免循环中断。

复现 officeclaw_a873fc300d433068be0b0741 的根因：pptx-craft 的 ask_user 节点
在 resume 重放时 LLM 重新生成主题候选 → questions args 变 → args_hash 变 →
tool_call_id 与中断时不一致 → _consume_pending_resume_input 返回 None →
ask_user_rail user_input=None → 再次 interrupt → 死循环。

回退匹配按 (tool_name, tool_call_id 末段 idx) 对齐：idx 是同
``(tool_name, args_hash)`` 组合的调用次序，避免同一 stage 内多个同名同参
调用时把 user_input 误注入到非中断点的同名调用。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import (
    SkillTurboExecutor,
    _parse_call_idx_from_call_id,
    _parse_tool_name_from_call_id,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = (
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes",
    )
    return SkillTurboExecutor(environment=env)


@pytest.mark.asyncio
async def test_use_tool_rejects_hitl_pause_without_model_identity(monkeypatch):
    """没有唯一模型身份时，SkillTurbo 不应保存不可恢复的 resume_ctx。"""
    ex = _make_executor()
    ex._execution_inputs = {"metadata": {}}
    ex._current_plan_code = "plan"
    ex._snapshot_task_states = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(
        ex,
        "_run_rail_hook",
        AsyncMock(side_effect=AbortError("hitl")),
    )
    save_resume_ctx = AsyncMock()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.executor.save_resume_ctx",
        save_resume_ctx,
    )

    with pytest.raises(RuntimeError, match="model identity is unavailable"):
        await ex.use_tool("ask_user")

    save_resume_ctx.assert_not_awaited()


class TestParseToolNameFromCallId:
    def test_parses_standard_id(self):
        assert (
            _parse_tool_name_from_call_id("skill_turbo-tc-ask_user-605c02b1-0")
            == "ask_user"
        )

    def test_parses_tool_name_with_dash(self):
        assert (
            _parse_tool_name_from_call_id("skill_turbo-tc-write_file-abcd1234-2")
            == "write_file"
        )

    def test_returns_none_for_non_skill_turbo_id(self):
        assert _parse_tool_name_from_call_id("other-prefix-ask_user-1234-0") is None

    def test_returns_none_for_empty_or_short(self):
        assert _parse_tool_name_from_call_id("") is None
        assert _parse_tool_name_from_call_id("skill_turbo-tc-") is None
        assert _parse_tool_name_from_call_id("skill_turbo-tc-x-1") is None

    def test_non_string_returns_none(self):
        assert _parse_tool_name_from_call_id(None) is None  # type: ignore[arg-type]


class TestConsumePendingResumeInputFallback:
    def test_exact_match_returns_user_input_and_keeps_id(self):
        ex = _make_executor()
        ex.set_pending_resume(
            expected_tool_call_id="skill_turbo-tc-ask_user-605c02b1-0",
            user_input=[{"question": "q", "selected_options": ["A"]}],
        )
        user_input, effective_id = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-605c02b1-0", "ask_user"
        )
        assert user_input == [{"question": "q", "selected_options": ["A"]}]
        assert effective_id == "skill_turbo-tc-ask_user-605c02b1-0"
        assert ex._pending_resume is None

    def test_no_pending_returns_none_and_keeps_id(self):
        ex = _make_executor()
        user_input, effective_id = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-deadbeef-0", "ask_user"
        )
        assert user_input is None
        assert effective_id == "skill_turbo-tc-ask_user-deadbeef-0"

    def test_tool_name_mismatch_keeps_pending(self):
        """tool_name 不同时不消费 pending（防止误注入到无关工具调用）。"""
        ex = _make_executor()
        ex.set_pending_resume(
            expected_tool_call_id="skill_turbo-tc-ask_user-605c02b1-0",
            user_input=[{"question": "q", "selected_options": ["A"]}],
        )
        user_input, effective_id = ex._consume_pending_resume_input(
            "skill_turbo-tc-write_file-aaaaaaaa-0", "write_file"
        )
        assert user_input is None
        assert effective_id == "skill_turbo-tc-write_file-aaaaaaaa-0"
        # pending 保留，等真正的 ask_user 调用到来
        assert ex._pending_resume is not None

    def test_tcid_mismatch_but_tool_name_match_aligns_to_expected(self):
        """核心修复：tcid 因非确定性 args 变化，但 tool_name 匹配时，
        用 expected_tool_call_id 对齐并注入 user_input，打破死循环。"""
        ex = _make_executor()
        expected_tcid = "skill_turbo-tc-ask_user-605c02b1-0"
        replay_tcid = "skill_turbo-tc-ask_user-ac68cd05-0"
        ex.set_pending_resume(
            expected_tool_call_id=expected_tcid,
            user_input=[{"question": "q", "selected_options": ["主题A"]}],
        )
        user_input, effective_id = ex._consume_pending_resume_input(
            replay_tcid, "ask_user"
        )
        assert user_input == [{"question": "q", "selected_options": ["主题A"]}]
        # 关键断言：effective_id 对齐到中断时的 expected_tcid，使 rail 能
        # 按 RESUME_USER_INPUT_KEY 正确取出 user_input。
        assert effective_id == expected_tcid
        assert ex._pending_resume is None

    def test_pending_consumed_once(self):
        """回退命中后 pending 清空，后续同名调用拿不到 user_input。"""
        ex = _make_executor()
        ex.set_pending_resume(
            expected_tool_call_id="skill_turbo-tc-ask_user-605c02b1-0",
            user_input=[{"question": "q", "selected_options": ["A"]}],
        )
        _, _ = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-ac68cd05-0", "ask_user"
        )
        # 第二次调用：pending 已清空
        user_input2, effective_id2 = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-11111111-0", "ask_user"
        )
        assert user_input2 is None
        assert effective_id2 == "skill_turbo-tc-ask_user-11111111-0"

    def test_expected_tool_name_parsed_from_id(self):
        """set_pending_resume 解析 expected_tool_name，供回退匹配使用。"""
        ex = _make_executor()
        ex.set_pending_resume(
            expected_tool_call_id="skill_turbo-tc-ask_user-605c02b1-0",
            user_input="payload",
        )
        assert ex._pending_resume is not None
        assert ex._pending_resume["expected_tool_name"] == "ask_user"

    def test_none_user_input_does_not_trigger_fallback(self):
        """user_input 为 None 的 pending 不走回退（防止把空回复注入）。"""
        ex = _make_executor()
        ex.set_pending_resume(
            expected_tool_call_id="skill_turbo-tc-ask_user-605c02b1-0",
            user_input=None,
        )
        user_input, effective_id = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-ac68cd05-0", "ask_user"
        )
        assert user_input is None
        assert effective_id == "skill_turbo-tc-ask_user-ac68cd05-0"


class TestParseCallIdxFromCallId:
    def test_parses_idx_zero(self):
        assert _parse_call_idx_from_call_id("skill_turbo-tc-ask_user-605c02b1-0") == 0

    def test_parses_idx_nonzero(self):
        assert _parse_call_idx_from_call_id("skill_turbo-tc-ask_user-bbbbbbbb-2") == 2

    def test_returns_none_for_non_numeric_idx(self):
        assert _parse_call_idx_from_call_id("skill_turbo-tc-ask_user-hash-x") is None

    def test_returns_none_for_missing_dash(self):
        assert _parse_call_idx_from_call_id("no_dash_here") is None
        assert _parse_call_idx_from_call_id("trailing-") is None

    def test_returns_none_for_non_string(self):
        assert _parse_call_idx_from_call_id(None) is None  # type: ignore[arg-type]


class TestConsumePendingResumeIdxComparison:
    """回退匹配额外比较 tool_call_id 末段 idx，避免多同名同参调用误注入。

    idx 是同 ``(tool_name, args_hash)`` 组合的调用次序（0-based）。多同名调用
    场景下，回退命中要求 current 与 expected 的 idx 一致。
    """

    def test_idx_match_aligns_to_expected(self):
        """同名 + idx 一致（中断在第 2 次同参调用）：回退命中。"""
        ex = _make_executor()
        expected_tcid = "skill_turbo-tc-ask_user-bbbbbbbb-1"
        replay_tcid = "skill_turbo-tc-ask_user-cccccccc-1"  # args 变了，idx 仍是 1
        ex.set_pending_resume(
            expected_tool_call_id=expected_tcid,
            user_input=[{"question": "q", "selected_options": ["B"]}],
        )
        user_input, effective_id = ex._consume_pending_resume_input(
            replay_tcid, "ask_user"
        )
        assert user_input == [{"question": "q", "selected_options": ["B"]}]
        assert effective_id == expected_tcid
        assert ex._pending_resume is None

    def test_idx_mismatch_keeps_pending(self):
        """同名 + idx 不一致：不消费 pending，等 idx 匹配的调用到来。"""
        ex = _make_executor()
        expected_tcid = "skill_turbo-tc-ask_user-bbbbbbbb-1"
        replay_tcid = "skill_turbo-tc-ask_user-aaaaaaaa-0"  # 第 0 次同名调用
        ex.set_pending_resume(
            expected_tool_call_id=expected_tcid,
            user_input=[{"question": "q", "selected_options": ["B"]}],
        )
        user_input, effective_id = ex._consume_pending_resume_input(
            replay_tcid, "ask_user"
        )
        assert user_input is None
        assert effective_id == replay_tcid
        # pending 保留给第 1 次调用
        assert ex._pending_resume is not None

    def test_idx_match_then_mismatch_full_flow(self):
        """完整流程：第 0 次不命中保留 pending，第 1 次命中消费。"""
        ex = _make_executor()
        expected_tcid = "skill_turbo-tc-ask_user-bbbbbbbb-1"
        ex.set_pending_resume(
            expected_tool_call_id=expected_tcid,
            user_input="answer_for_second",
        )
        # 第 0 次同名调用（idx=0 != 1）
        r0 = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-aaaaaaaa-0", "ask_user"
        )
        assert r0 == (None, "skill_turbo-tc-ask_user-aaaaaaaa-0")
        # 第 1 次同名调用（idx=1 == 1）
        r1 = ex._consume_pending_resume_input(
            "skill_turbo-tc-ask_user-cccccccc-1", "ask_user"
        )
        assert r1 == ("answer_for_second", expected_tcid)
