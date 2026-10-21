# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Idempotency guard tests: 同会话×同主题已交付后再次调用应短路返回「已完成」。

Covers GitCode issue #5131（深度研究报告已交付后再次对话重复执行，且概率失败）。
守卫契约：
- 首轮报告交付后，rail.after_tool_call 在 states.pop 前调 _record_delivery 记录
  delivery_index[workflow_id] = {query(归一化), file_name, conversation_id,
  html_style_status, delivered_at}。
- 下一轮同主题（query 归一化后相等）同 file_name 的 before_tool_call 调用，
  在 bind_deepresearch_execution_context 之前命中 _lookup_delivery 即短路：
  tool_result 置为 kind=completed、force_finish，不进入 SDK 重跑路径。
- 不同主题（query 归一化后不等）或不同 file_name 不命中，正常 bind 走原流程。
"""

from __future__ import annotations

import asyncio
import types

import pytest
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

    参考 tests/unit_tests/agentserver/test_deepresearch_execute_tool.py 的 _rail_ctx。
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


def test_second_turn_same_topic_returns_already_delivered_without_rerun(monkeypatch):
    # 首轮已交付后，session 里有 delivery_index[wf_id] 指向同一 query/file_name
    sess = _fake_session(delivery_index={
        "wf_1": {"query": "深度研究AI芯片行业", "file_name": "ai_chip_report",
                 "conversation_id": "ad256fe6-...", "html_style_status": "applied",
                 "delivered_at": 1791601751.0},
    })
    # before_tool_call 命中 _lookup_delivery 应短路
    ctx = _build_callback_ctx(sess, tool_call_id="wf_new", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report", "report_type": "brief"})
    asyncio.run(rail.DeepResearchExecutionRail(model_provider=lambda: None).before_tool_call(ctx))
    # 守卫短路：不应调用 bind_deepresearch_execution_context（即不应进入 SDK 重跑路径）
    assert ctx.inputs.tool_result is not None
    payload = ctx.inputs.tool_result
    assert payload["kind"] == "completed"
    assert "已交付" in payload["content"] or payload.get("state", {}).get("phase") == "completed"


def test_different_topic_not_blocked():
    sess = _fake_session(delivery_index={
        "wf_1": {"query": "深度研究AI芯片行业", "file_name": "ai_chip_report",
                 "conversation_id": "ad256fe6-...", "html_style_status": "applied",
                 "delivered_at": 1791601751.0},
    })
    ctx = _build_callback_ctx(sess, tool_call_id="wf_new2", args={
        "query": "深度研究新能源汽车行业", "file_name": "nev_report", "report_type": "brief"})
    asyncio.run(rail.DeepResearchExecutionRail(model_provider=lambda: None).before_tool_call(ctx))
    # 不同主题：守卫不短路，正常进入 bind（tool_result 未被改写为 completed）
    assert ctx.inputs.tool_result is None or not isinstance(ctx.inputs.tool_result, dict)
