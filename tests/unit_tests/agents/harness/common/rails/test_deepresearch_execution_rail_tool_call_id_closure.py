# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""tool_call_id 闭合测试：打断续跑时终端结果须携带原始 workflow_id。

覆盖 GitCode issue #5131 根因（框架层打断续跑 tool_call_id 闭合）。
闭合契约：
- 首轮打断：after_tool_call 把工具结果改写为 ToolInterruptException，
  tool_call.id = interaction_id（= f"{workflow_id}_interaction_{revision}"），
  并写 alias[interaction_id] = workflow_id。
- 续跑：harness handle_resume 用 interaction_id 续跑，before_tool_call 收到的
  ctx.inputs.tool_call.id = interaction_id。
- 修复前：before_tool_call 不重写 id，tokens 以 interaction_id 为键，
  StreamEventRail(priority=80) 发出的 chat.tool_call/chat.tool_result 都带
  interaction_id，而原始 workflow_id 的 chat.tool_call 永远等不到配对结果
  -> session_history open_calls/pending 泄漏 -> 下一轮 LLM 误判「研究未完成」。
- 修复后：before_tool_call 在 bind 之后把 ctx.inputs.tool_call.id 重写回
  workflow_id（经 alias 解析的原始 id），tokens 也以 workflow_id 为键，
  finally 正确清理，chat.tool_call/chat.tool_result 对齐原始 id。
"""

from __future__ import annotations

import asyncio
import types

from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails import deepresearch_execution_rail as rail


def _fake_session(delivery_index=None, states=None, aliases=None):
    store = {
        rail.DEEPRESEARCH_DELIVERY_INDEX_KEY: delivery_index or {},
        rail.DEEPRESEARCH_EXECUTION_STATE_KEY: states or {},
        rail.DEEPRESEARCH_EXECUTION_ALIAS_KEY: aliases or {},
    }
    sess = types.SimpleNamespace()
    sess.get_state = lambda k: store.get(k)
    sess.update_state = lambda d: store.update(d)
    return sess


def _build_callback_ctx(session, *, tool_call_id, args):
    """构造最小 AgentCallbackContext：tool_name=deepresearch_execute、tool_result=None、extra={}。

    参考 tests/unit_tests/agents/harness/common/tools/deepresearch/test_deepresearch_execute_idempotency.py
    的 _build_callback_ctx。
    """
    tool_call = types.SimpleNamespace(
        id=tool_call_id,
        name="deepresearch_execute",
        arguments=args,
    )
    forced: list[dict] = []
    ctx = types.SimpleNamespace(
        session=session,
        agent=None,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="deepresearch_execute",
            tool_args=args,
            tool_result=None,
        ),
        extra={},
        exception=None,
        request_force_finish=forced.append,
        force_finish_requests=forced,
    )
    return ctx


def test_resumed_terminal_result_carries_original_tool_call_id():
    """续跑：harness 传入 interaction_id，before_tool_call 重写回原始 workflow_id。

    模拟首轮打断后 aliases 已记录 interaction_id -> workflow_id，
    states 里有 wait_feedback 相位的 workflow state。续跑时 harness
    handle_resume 用 interaction_id 续跑（与生产 handler.py 一致）。
    """
    sess = _fake_session(
        aliases={"call_orig_interaction_2": "call_orig"},
        states={
            "call_orig": {
                "phase": "wait_feedback",
                "revision": 2,
                "conversation_id": "ad256fe6-test-test-test",
                "query": "深度研究AI芯片行业",
                "file_name": "ai_chip_report",
                "requested_report_type": "brief",
            }
        },
    )
    ctx = _build_callback_ctx(
        sess,
        tool_call_id="call_orig_interaction_2",
        args={
            "query": "深度研究AI芯片行业",
            "file_name": "ai_chip_report",
            "report_type": "brief",
        },
    )
    r = rail.DeepResearchExecutionRail(model_provider=lambda: None)
    asyncio.run(r.before_tool_call(ctx))
    # B 修复后：ctx.inputs.tool_call.id 被重写回原始 workflow_id
    assert ctx.inputs.tool_call.id == "call_orig"
    # 且 tokens 字典以原始 id 为键（finally 正确清理）
    assert "call_orig" in ctx.extra[rail._TOKENS_KEY]
    assert "call_orig_interaction_2" not in ctx.extra[rail._TOKENS_KEY]


def test_first_turn_no_alias_no_rewrite():
    """首轮调用（无 alias）：tool_call_id == workflow_id，不应触发重写。

    防御：修复不能破坏首轮正常调用路径。无 alias 时 _workflow_id 返回
    原值，tool_call_id == workflow_id，重写分支不进入，id 保持不变。
    """
    sess = _fake_session(
        states={
            "call_first": {
                "phase": "new",
                "revision": 0,
                "conversation_id": "",
                "query": "深度研究AI芯片行业",
                "file_name": "ai_chip_report",
                "requested_report_type": "brief",
            }
        },
    )
    ctx = _build_callback_ctx(
        sess,
        tool_call_id="call_first",
        args={
            "query": "深度研究AI芯片行业",
            "file_name": "ai_chip_report",
            "report_type": "brief",
        },
    )
    r = rail.DeepResearchExecutionRail(model_provider=lambda: None)
    asyncio.run(r.before_tool_call(ctx))
    # 首轮：无 alias，id 不重写
    assert ctx.inputs.tool_call.id == "call_first"
    assert "call_first" in ctx.extra[rail._TOKENS_KEY]
