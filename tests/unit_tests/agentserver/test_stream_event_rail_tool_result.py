# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool-result streaming behaviour of JiuSwarmStreamEventRail."""

# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _format_tool_result_for_stream,
    _structured_tool_result_payload,
    extract_tool_interrupt,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
    EXECUTION_SCHEMA,
)


class _FakeSession:
    def __init__(self):
        self.outputs = []

    async def write_stream(self, output):
        self.outputs.append(output)

    @staticmethod
    def get_session_id() -> str:
        return "sess-1"


def _pending_approval_exception() -> AbortError:
    """Mirror what the approval rail raises out of ``before_tool_call``."""
    return AbortError(
        "Tool execution interrupted: exit_plan_mode",
        cause=ToolInterruptException(request=InterruptRequest(message="计划审批")),
    )


def _ctx(session: _FakeSession, *, exception: object = None):
    tool_call = SimpleNamespace(id="call_1", name="exit_plan_mode", arguments={})
    return SimpleNamespace(
        session=session,
        agent=None,
        context=None,
        extra={},
        exception=exception,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="exit_plan_mode",
            tool_args={},
            tool_result=SimpleNamespace(success=False, data=None, error=""),
        ),
    )


@pytest.mark.asyncio
async def test_no_tool_result_emitted_while_waiting_for_approval() -> None:
    """A call suspended for approval has no result to report yet."""
    session = _FakeSession()
    ctx = _ctx(session, exception=_pending_approval_exception())

    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    assert [output.type for output in session.outputs] == []


@pytest.mark.asyncio
async def test_tool_result_emitted_for_executed_tool_call() -> None:
    session = _FakeSession()
    ctx = _ctx(session)

    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    assert [output.type for output in session.outputs] == ["tool_result"]


def test_shared_interrupt_unwrap_handles_deep_and_cyclic_chains() -> None:
    interrupt = ToolInterruptException(request=InterruptRequest(message="approve"))
    wrapped = interrupt
    for index in range(10):
        wrapped = SimpleNamespace(cause=wrapped, marker=index)

    assert extract_tool_interrupt(wrapped) is interrupt

    first = SimpleNamespace(cause=None)
    second = SimpleNamespace(cause=first)
    first.cause = second
    assert extract_tool_interrupt(first) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("first_finished", ["todo_modify", "bash"])
async def test_parallel_tools_preserve_results_and_todo_update(monkeypatch, first_finished):
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        get_subagent_parent_session,
    )

    session = _FakeSession()
    rail = JiuSwarmStreamEventRail()
    monkeypatch.setattr(rail, "_rebind_runtime_cwd", lambda **kwargs: None)
    todo_updated = AsyncMock()
    monkeypatch.setattr(rail, "_emit_todo_updated", todo_updated)
    shared_extra = {}
    todo_bound = asyncio.Event()
    shell_bound = asyncio.Event()
    first_done = asyncio.Event()

    async def call(name):
        ctx = _ctx(session)
        ctx.extra = shared_extra
        ctx.inputs.tool_call.id = name
        ctx.inputs.tool_call.name = name
        ctx.inputs.tool_name = name
        ctx.inputs.tool_result = {"message": "ok"}
        original_parent = get_subagent_parent_session()
        if name == "bash":
            await todo_bound.wait()
        await rail.before_tool_call(ctx)
        if name == "todo_modify":
            todo_bound.set()
            await shell_bound.wait()
        else:
            shell_bound.set()
        if name != first_finished:
            await first_done.wait()
        try:
            await rail.after_tool_call(ctx)
            assert get_subagent_parent_session() is original_parent
            assert ctx.extra is shared_extra
        finally:
            if name == first_finished:
                first_done.set()

    results = await asyncio.wait_for(
        asyncio.gather(call("todo_modify"), call("bash"), return_exceptions=True),
        timeout=5,
    )
    assert results == [None, None]
    emitted = [o.payload["tool_result"] for o in session.outputs if o.type == "tool_result"]
    assert {r["tool_call_id"] for r in emitted} == {"todo_modify", "bash"}
    assert all(r["raw_output"] == {"message": "ok"} for r in emitted)
    todo_updated.assert_awaited_once()


def _deepresearch_completed_envelope(*, content: str, workflow_llm_token_usage: dict | None = None) -> dict:
    return {
        "schema_version": EXECUTION_SCHEMA,
        "kind": "completed",
        "content": content,
        "state": {
            "schema_version": 1,
            "phase": "completed",
            "query": "深度研究AI芯片行业",
            "file_name": "ai_chip_report",
            "conversation_id": "cid-1",
            "requested_report_type": "brief",
            "revision": 3,
        },
        "workflow_llm_token_usage": workflow_llm_token_usage or {"input_tokens": 10, "output_tokens": 5},
    }


def test_format_tool_result_for_stream_deepresearch_completed_returns_content() -> None:
    """deepresearch completed 信封：emitted result 取 content（→ history/下一轮 LLM 语境），而非 str(大字典)。"""
    content = "✅ 深度研究已完成！\n\nai_chip_report已成功生成并交付。"
    envelope = _deepresearch_completed_envelope(content=content)
    assert _format_tool_result_for_stream(envelope) == content


def test_format_tool_result_for_stream_non_envelope_dict_keeps_str_form() -> None:
    """非 deepresearch completed 信封的 dict 仍走 str(result)，保持既有口径（回归防护）。"""
    other = {"schema_version": "other.v1", "kind": "completed", "content": "x"}
    assert _format_tool_result_for_stream(other) == str(other)
    interaction = {"schema_version": EXECUTION_SCHEMA, "kind": "interaction", "content": "x"}
    assert _format_tool_result_for_stream(interaction) == str(interaction)
    assert _structured_tool_result_payload(other) == other


@pytest.mark.asyncio
async def test_emit_tool_result_deepresearch_completed_emits_content_result_and_structured_raw_output() -> None:
    """Plan A 端到端契约：同一 ctx.inputs.tool_result（dict）同时满足两条消费链。

    - emitted ``result`` = 完成文案（→ history 记录 → restart 恢复的 ToolMessage 看到文案，不读大字典）
    - emitted ``raw_output`` = 结构化信封（含 kind=completed + workflow_llm_token_usage，
      → interface_deep._extract_deepresearch_sdk_usage 可提取 SDK token 用量）
    """
    content = "✅ 深度研究已完成！\n\nai_chip_report已成功生成并交付。"
    sdk_usage = {"input_tokens": 999999, "output_tokens": 99999}
    envelope = _deepresearch_completed_envelope(content=content, workflow_llm_token_usage=sdk_usage)

    session = _FakeSession()
    tool_call = SimpleNamespace(id="call_dr", name="deepresearch_execute", arguments={})
    ctx = SimpleNamespace(
        session=session,
        agent=None,
        context=None,
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="deepresearch_execute",
            tool_args={},
            tool_result=envelope,
        ),
    )

    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    emitted = [o for o in session.outputs if o.type == "tool_result"]
    assert len(emitted) == 1
    payload = emitted[0].payload["tool_result"]
    assert payload["result"] == content
    assert "workflow_llm_token_usage" not in payload["result"]
    assert isinstance(payload["raw_output"], dict)
    assert payload["raw_output"]["kind"] == "completed"
    assert payload["raw_output"]["workflow_llm_token_usage"] == sdk_usage
