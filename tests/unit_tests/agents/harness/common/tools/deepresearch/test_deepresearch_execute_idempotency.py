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
    # 完成文案含真实 file_name（Issue 1/6），不回落默认「研究报告」
    assert "ai_chip_report" in payload["content"]
    # 跳过真实执行依赖 before_tool_call 的 force_finish：@rail wrapper 在
    # before 钩子后检查 has_force_finish_request 并 return None，工具方法体
    # 不会执行（openjiuwen/core/single_agent/rail/base.py:613-615）。
    assert ctx.force_finish_requests
    # 不得写共享 ctx.extra["_skip_tool"]：该标记由 AbilityManager 按名 pop
    # （ability_manager.py:653），短路调用自身不会走到 pop（方法体被跳过），
    # 标记会残留在共享 extra 里被同轮并行 tool call 误消费。
    assert "_skip_tool" not in ctx.extra
    # Issue 3：tool_msg 必须设为精简完成文案，含真实 file_name 且不含内部 state/conversation_id
    assert ctx.inputs.tool_msg is not None
    assert "ai_chip_report" in ctx.inputs.tool_msg.content
    assert "conversation_id" not in ctx.inputs.tool_msg.content


def test_short_circuit_completion_content_has_real_file_name():
    """Issue 1/6：短路完成文案含真实 file_name，不回落默认「研究报告」。"""
    sess = _fake_session(delivery_index={
        "wf_1": {"query": "深度研究AI芯片行业", "file_name": "ai_chip_report",
                 "conversation_id": "ad256fe6-...", "html_style_status": "applied",
                 "delivered_at": 1791601751.0},
    })
    ctx = _build_callback_ctx(sess, tool_call_id="wf_new", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report", "report_type": "brief"})
    asyncio.run(rail.DeepResearchExecutionRail(model_provider=lambda: None).before_tool_call(ctx))
    content = str(ctx.inputs.tool_result.get("content", ""))
    assert "ai_chip_report" in content
    assert "研究报告已成功生成" not in content or "ai_chip_report" in content


def test_short_circuit_skips_in_flight_workflow_resume():
    """Issue 2：进行中 workflow（workflow_id in states）不走短路，避免吞掉用户答复与残留 state。"""
    sess = _fake_session(
        delivery_index={
            "wf_1": {"query": "深度研究AI芯片行业", "file_name": "ai_chip_report",
                     "conversation_id": "ad256fe6-...", "html_style_status": "applied",
                     "delivered_at": 1791601751.0},
        },
        states={"wf_new": {"phase": "wait_feedback", "query": "深度研究AI芯片行业"}},
    )
    ctx = _build_callback_ctx(sess, tool_call_id="wf_new", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report", "report_type": "brief"})
    asyncio.run(rail.DeepResearchExecutionRail(model_provider=lambda: None).before_tool_call(ctx))
    # 进行中 workflow 不短路：未请求 force_finish，tool_result 未被改写为 completed
    assert ctx.force_finish_requests == []
    assert ctx.inputs.tool_result is None or not isinstance(ctx.inputs.tool_result, dict) \
        or ctx.inputs.tool_result.get("kind") != "completed"


def test_empty_query_or_file_name_not_matched():
    """Issue 7：空 query/file_name 不命中交付索引，不误短路合法研究。"""
    sess = _fake_session(delivery_index={
        "wf_1": {"query": "", "file_name": "",
                 "conversation_id": "cid-empty", "html_style_status": None,
                 "delivered_at": 1791601751.0},
    })
    ctx = _build_callback_ctx(sess, tool_call_id="wf_new", args={
        "query": "", "file_name": "", "report_type": "brief"})
    asyncio.run(rail.DeepResearchExecutionRail(model_provider=lambda: None).before_tool_call(ctx))
    # 空键不命中：不短路
    assert ctx.force_finish_requests == []


def test_upgrade_from_brief_not_short_circuited():
    """CR-005：已交付 brief 后「升级为专业版」不得被幂等短路吞掉。

    upgrade_from_brief 走 execution.py 的升级链路，会用 original_query 覆盖本次
    query，与已交付 brief 条目的 query 天然相同；若短路不排除升级路径，专业版
    报告不会生成，用户只收到旧精简报告正文并被 force_finish。
    """
    sess = _fake_session(delivery_index={
        "wf_1": {"query": "深度研究AI芯片行业", "file_name": "ai_chip_report",
                 "conversation_id": "ad256fe6-...", "html_style_status": "applied",
                 "delivered_at": 1791601751.0},
    })
    ctx = _build_callback_ctx(sess, tool_call_id="wf_upgrade", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report",
        "report_type": "professional", "upgrade_from_brief": True})
    asyncio.run(rail.DeepResearchExecutionRail(model_provider=lambda: None).before_tool_call(ctx))
    assert ctx.inputs.tool_result is None
    assert ctx.force_finish_requests == []


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


def test_short_circuit_skips_after_tool_call_side_effects():
    """短路标记跳过 after_tool_call 副作用：避免双重 force_finish 与交付索引污染。

    场景：before_tool_call 命中幂等短路后，框架仍调 after_tool_call。
    after_tool_call 检测短路标记后 early-return（finally 仍执行，但 token 未
    bind 故 reset 为 no-op），不应：再次 request_force_finish（破坏 ==[一条]
    契约）、以空 query/file_name 调 _record_delivery（污染 delivery_index）。
    """
    real_entry = {
        "query": "深度研究AI芯片行业",
        "file_name": "ai_chip_report",
        "conversation_id": "ad256fe6-...",
        "html_style_status": "applied",
        "delivered_at": 1791601751.0,
    }
    sess = _fake_session(delivery_index={"wf_1": dict(real_entry)})
    ctx = _build_callback_ctx(sess, tool_call_id="wf_new", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report", "report_type": "brief"})
    rail_instance = rail.DeepResearchExecutionRail(model_provider=lambda: None)
    asyncio.run(rail_instance.before_tool_call(ctx))
    asyncio.run(rail_instance.after_tool_call(ctx))
    assert len(ctx.force_finish_requests) == 1
    idx = sess.get_state(rail.DEEPRESEARCH_DELIVERY_INDEX_KEY)
    assert len(idx) == 1
    assert "wf_1" in idx
    assert idx["wf_1"]["query"] == real_entry["query"]
    assert idx["wf_1"]["file_name"] == real_entry["file_name"]
    assert "_deepresearch_short_circuit" not in ctx.extra
    assert "_skip_tool" not in ctx.extra
    assert not [k for k in ctx.extra if k.startswith(rail._SHORT_CIRCUIT_KEY_PREFIX)]


def test_terminal_completed_updates_tool_msg_to_completion_content():
    """终态完成时 tool_msg 更新为完成内容，替换 interaction 分支的「已暂停」消息。

    修复前：terminal 分支不更新 tool_msg，interaction 分支留下的
    「DeepResearch 已暂停…正在等待用户答复」消息残留到下一轮 LLM 上下文，
    LLM 误判「研究未完成」（issue #5131 残留问题）。
    修复后：terminal 分支在 request_force_finish 前把 tool_msg 更新为
    完成内容，下一轮 LLM 看到的是「已完成」而非「已暂停」。
    """
    from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
        EXECUTION_SCHEMA,
    )

    sess = _fake_session()
    ctx = _build_callback_ctx(sess, tool_call_id="wf_done", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report", "report_type": "brief"})
    completion_content = "✅ 深度研究已完成！\n\nai_chip_report已成功生成并交付。"
    ctx.inputs.tool_result = {
        "schema_version": EXECUTION_SCHEMA,
        "kind": "completed",
        "content": completion_content,
        "state": {
            "schema_version": 1,
            "phase": "completed",
            "query": "深度研究AI芯片行业",
            "file_name": "ai_chip_report",
            "conversation_id": "test-cid",
            "requested_report_type": "brief",
            "revision": 4,
        },
    }
    r = rail.DeepResearchExecutionRail(model_provider=lambda: None)
    asyncio.run(r.after_tool_call(ctx))
    assert ctx.inputs.tool_msg is not None
    assert "已完成" in ctx.inputs.tool_msg.content
    assert "暂停" not in ctx.inputs.tool_msg.content
    assert len(ctx.force_finish_requests) == 1


def test_terminal_completed_preserves_structured_tool_result_for_usage_accounting():
    """终态完成时保留 tool_result 结构化信封，SDK token 用量可被下游提取（Issue 4）。

    面向模型的精简文案落到 tool_msg（react_agent 经 context.add_messages(tool_message)
    将 ToolMessage 写入会话历史，LLM 看到 tool_msg 而非 tool_result）。tool_result 保留
    kind=completed + workflow_llm_token_usage 结构，使 StreamEventRail raw_output 与
    interface_deep._extract_deepresearch_sdk_usage 能提取 SDK token 用量计入 perf 汇总。
    """
    from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
        EXECUTION_SCHEMA,
    )

    sess = _fake_session()
    ctx = _build_callback_ctx(sess, tool_call_id="wf_done2", args={
        "query": "深度研究AI芯片行业", "file_name": "ai_chip_report", "report_type": "brief"})
    completion_content = "✅ 深度研究已完成！\n\nai_chip_report已成功生成并交付。"
    ctx.inputs.tool_result = {
        "schema_version": EXECUTION_SCHEMA,
        "kind": "completed",
        "content": completion_content,
        "workflow_llm_token_usage": {"input_tokens": 999999, "output_tokens": 99999},
        "state": {
            "schema_version": 1,
            "phase": "completed",
            "query": "深度研究AI芯片行业",
            "file_name": "ai_chip_report",
            "conversation_id": "test-cid-2",
            "requested_report_type": "brief",
            "revision": 4,
        },
    }
    r = rail.DeepResearchExecutionRail(model_provider=lambda: None)
    asyncio.run(r.after_tool_call(ctx))
    # tool_msg 含精简完成文案（LLM 经 context.add_messages 看到）
    assert ctx.inputs.tool_msg is not None
    assert "已完成" in ctx.inputs.tool_msg.content
    # tool_result 保留结构化信封：kind=completed + workflow_llm_token_usage 不丢失
    assert isinstance(ctx.inputs.tool_result, dict)
    assert ctx.inputs.tool_result.get("kind") == "completed"
    assert "workflow_llm_token_usage" in ctx.inputs.tool_result
    assert len(ctx.force_finish_requests) == 1
