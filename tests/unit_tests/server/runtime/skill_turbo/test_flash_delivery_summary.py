# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""验证 Flash 实际装配及 SDK 回调链中的 Turbo 总结时序与恢复去重。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    ToolMessage,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.foundation.tool.schema import ToolInfo
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    RESUME_USER_INPUT_KEY,
    ToolInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    AgentRail,
    ModelCallInputs,
    ToolCallInputs,
)
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.flash.rails.flash_delivery_summary_rail import FlashDeliverySummaryRail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools as turbo_tools
from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
    SkillTurboDeliverySummaryRail,
)

_MARKER = "【PPT 交付总结】"
_SUMMARY = f"{_MARKER}\n已生成并交付测试 PPT。"
_TOOL = "skill_acceleration_exec"


def _holder():
    return {
        "p10_delivery": {
            "info": {"send_file_status": "sent", "delivery_status": "ok"},
            "files": [{"path": "test.pptx"}],
            "delivery_summary": _SUMMARY,
        },
    }


@pytest.fixture(autouse=True)
def _summary_context(monkeypatch):
    # PPT 技能源在外部仓；这里只替换其标记常量，不替换缓存或发送实现。
    monkeypatch.setattr(turbo_tools, "_ppt_delivery_summary_start", lambda: _MARKER)
    turbo_tools.clear_pending_ppt_delivery_summary()
    turbo_tools.set_skill_turbo_hitl_tic(None)
    yield
    turbo_tools.clear_pending_ppt_delivery_summary()
    turbo_tools.set_skill_turbo_hitl_tic(None)


@pytest.fixture
def assemble_flash(monkeypatch):
    def assemble(enabled=True):
        config = {"react": {"skill_turbo": {"enabled": enabled}}}
        monkeypatch.setattr(interface_deep, "get_config", lambda: config)
        monkeypatch.setattr(interface_flash, "get_config", lambda: config)
        adapter = object.__new__(JiuwenSwarmFlashAdapter)
        adapter._model = adapter._instance = None
        adapter._config_cache = {}
        adapter._config_base_cache = config
        adapter._instance_overrides = {}
        adapter._skill_turbo_delivery_summary_rail = None

        # 保留真实的声明、Flash 过滤、组件构造和 enabled 门禁；隔离无关组件。
        real_builders = {
            "_build_agent_rails", "_build_stream_event_rail",
            "_build_skill_turbo_delivery_summary_rail",
        }
        for name in dir(JiuwenSwarmFlashAdapter):
            if name.startswith("_build_") and name not in real_builders:
                monkeypatch.setattr(adapter, name, Mock(return_value=None))
        monkeypatch.setattr(interface_deep, "_build_context_processor_rail", lambda **_: None)
        monkeypatch.setattr(adapter, "_load_extra_rails_from_env", lambda: [])
        monkeypatch.setattr(adapter, "_bind_checkpointer_to_rails", lambda: None)
        monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
        monkeypatch.setattr(adapter, "_task_execution_rail_enabled", lambda: True)
        rails = adapter._build_agent_rails(config["react"], config, mode="flash")
        adapter._build_task_execution_rail.assert_not_called()
        adapter._build_skill_turbo_prompt_rail.assert_called_once()
        adapter._build_skill_protocol_prompt_rail.assert_not_called()
        return adapter, rails

    return assemble


class _Session:
    def __init__(self):
        self.events = []

    async def write_stream(self, output):
        self.events.append(output)

    async def post_run(self):
        pass


@asynccontextmanager
async def _tool_context(rails, session):
    manager = AgentCallbackManager(f"flash-delivery-{uuid4().hex}")
    engine = SimpleNamespace(save_contexts=AsyncMock())
    agent = SimpleNamespace(agent_callback_manager=manager, context_engine=engine)
    # 故意反向注册，顺序必须由真实 SDK priority 决定，不能靠测试手工排序。
    for rail in reversed(rails):
        if isinstance(rail, (JiuSwarmStreamEventRail, SkillTurboDeliverySummaryRail)):
            await manager.register_rail(rail, agent)
    call = ToolCall(id="outer-ppt", type="function", name=_TOOL, arguments='{"finish_request": true}')
    messages = []

    async def add_messages(message):
        messages.append(message)

    ctx = AgentCallbackContext(
        agent=agent, session=session,
        inputs=ModelCallInputs(response=AssistantMessage(content="", tool_calls=[call])),
        context=SimpleNamespace(add_messages=add_messages, get_messages=lambda: messages),
    )
    flash_rail = next((r for r in rails if isinstance(r, FlashDeliverySummaryRail)), None)
    if flash_rail is not None:
        await flash_rail.before_invoke(ctx)
        await flash_rail.after_model_call(ctx)
    ctx.inputs = ToolCallInputs(tool_call=call, tool_name=_TOOL, tool_args={})
    try:
        yield ctx
    finally:
        if flash_rail is not None:
            await flash_rail.after_invoke(ctx)
        await manager.clear()


@pytest.mark.parametrize("enabled", [True, False])
def test_flash_assembly_respects_turbo_switch(assemble_flash, enabled):
    adapter, rails = assemble_flash(enabled)
    summaries = [rail for rail in rails if isinstance(rail, SkillTurboDeliverySummaryRail)]
    assert len(summaries) == int(enabled)
    assert adapter._skill_turbo_delivery_summary_rail == (summaries[0] if enabled else None)
    assert sum(isinstance(rail, JiuSwarmStreamEventRail) for rail in rails) == 1
    if enabled:
        # Flash 的时序调整不能污染共享类或父类创建的实例。
        normal = interface_deep.JiuWenSwarmDeepAdapter._build_skill_turbo_delivery_summary_rail()
        assert normal.priority == SkillTurboDeliverySummaryRail.priority
        assert type(normal) is SkillTurboDeliverySummaryRail
        assert type(summaries[0]) is FlashDeliverySummaryRail
        assert summaries[0].priority < JiuSwarmStreamEventRail.priority


@pytest.mark.asyncio
async def test_flash_success_emits_summary_once_after_real_tool_result(assemble_flash):
    adapter, rails = assemble_flash()
    session = _Session()
    async with _tool_context(rails, session) as ctx:
        # 代表内部流水线已经投递文件；外层回调只应补总结，不应重复发文件。
        await session.write_stream(OutputSchema(type="chat.file", index=0, payload={"path": "test.pptx"}))
        ctx.inputs.tool_result = turbo_tools._wrap_skill_turbo_result(
            {"success": True, "result": "任务已完成"}, _holder(),
        )
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert [event.type for event in session.events] == ["chat.file", "tool_result", "llm_output"]
        assert session.events[-1].payload["content"] == _SUMMARY
        assert _SUMMARY not in session.events[-2].payload["tool_result"]["result"]
        assert not ctx.has_force_finish_request
        await ctx.context.add_messages(ToolMessage(content="已交付", tool_call_id="outer-ppt"))
        await ctx.fire(AgentCallbackEvent.AFTER_REACT_ITERATION)
        finish = ctx.consume_force_finish()
        assert finish.result == {"output": _SUMMARY, "result_type": "answer"}
        assert [message.role for message in ctx.context.get_messages()] == ["tool", "assistant"]
        assert ctx.context.get_messages()[-1].content == _SUMMARY
        # 缓存消费后，重复调用总结组件不能再投递同一份正文。
        await adapter._skill_turbo_delivery_summary_rail.after_tool_call(ctx)
        await ctx.fire(AgentCallbackEvent.AFTER_REACT_ITERATION)
        assert len(session.events) == 3
        assert not ctx.has_force_finish_request
        assert turbo_tools.take_pending_ppt_delivery_summary() == ""


@pytest.mark.asyncio
async def test_flash_failed_delivery_does_not_emit_success_summary(assemble_flash):
    _, rails = assemble_flash()
    session = _Session()
    async with _tool_context(rails, session) as ctx:
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        ctx.inputs.tool_result = turbo_tools._wrap_skill_turbo_result(
            {"success": False, "error": "PPT 文件发送失败"}, _holder(),
        )
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert [event.type for event in session.events] == ["tool_result"]
        assert session.events[0].payload["tool_result"]["success"] is False
        assert turbo_tools.take_pending_ppt_delivery_summary() == ""
        assert not ctx.has_force_finish_request


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_kind", ["approval_exception", "hitl_result_marker"])
async def test_flash_pause_then_native_resume_emits_one_summary(assemble_flash, interrupt_kind):
    adapter, rails = assemble_flash()
    session = _Session()
    async with _tool_context(rails, session) as ctx:
        interrupt = ToolInterruptException(
            request=InterruptRequest(message="请确认 PPT 操作"),
            tool_call=SimpleNamespace(id="inner-approval", name="ask_user", arguments={}),
        )
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        if interrupt_kind == "approval_exception":
            ctx.exception = interrupt
            ctx.inputs.tool_result = {"success": False, "error": "等待审批"}
        else:
            ctx.inputs.tool_result = turbo_tools.build_skill_turbo_hitl_result(interrupt)
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert session.events == []
        assert not ctx.has_force_finish_request
        assert turbo_tools.take_pending_ppt_delivery_summary() == ""
        if interrupt_kind == "hitl_result_marker":
            assert isinstance(ctx.inputs.tool_result, ToolInterruptException)

        # 原生工具恢复成功仍走同一条 after_tool_call 链。
        ctx.exception = None
        ctx.inputs.tool_result = turbo_tools._wrap_skill_turbo_result(
            {"success": True, "result": "任务已完成"}, _holder(),
        )
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert [event.type for event in session.events] == ["tool_result", "llm_output"]
        assert session.events[-1].payload["content"] == _SUMMARY
        assert not ctx.has_force_finish_request
        await ctx.fire(AgentCallbackEvent.AFTER_REACT_ITERATION)
        assert ctx.consume_force_finish().result["output"] == _SUMMARY
        await adapter._skill_turbo_delivery_summary_rail.after_tool_call(ctx)
        assert len(session.events) == 2
        assert not ctx.has_force_finish_request


# 以下两个 Path A 用例（adapter._make_skill_turbo_resume_stream 直连 resume 流 /
# adapter._try_skill_turbo_resume 判重早退）随 skill_turbo 阶段 2 删除 Path A 一并
# 移除：resume 已统一走 harness handle_resume 重放 skill_acceleration_exec 工具体
# （[[S_06]] P3 统一 resume 路径），flash 适配器不再持有 resume 专属入口。守护
# 保证迁移至 Path B 等价用例：
# - 暂停→恢复→交付摘要单次输出且不补发：test_flash_pause_then_native_resume_
#   emits_one_summary（本文件，经真实 after_tool_call 回调链）；
# - 重复作答不重复执行交付：tests/unit_tests/agentserver/test_skill_turbo_tools.py
#   的 test_duplicate_resume_answer_returns_clean_placeholder_without_stop_hint
#   （工具体 resume_in_flight 判重早退：不 mark / 不误清 / 不启动 resume_stream）。


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "invalid", "send_error", "no_session", "unrelated_tool"])
async def test_flash_does_not_finish_without_emitted_summary(assemble_flash, case):
    adapter, rails = assemble_flash()
    session = _Session() if case != "no_session" else None
    if case == "send_error":
        session.write_stream = AsyncMock(side_effect=OSError("写流失败"))
    async with _tool_context(rails, session) as ctx:
        turbo_tools.set_pending_ppt_delivery_summary(
            "" if case == "empty" else "非交付总结" if case == "invalid" else _SUMMARY,
        )
        if case == "unrelated_tool":
            ctx.inputs.tool_name = "web_search"
        await adapter._skill_turbo_delivery_summary_rail.after_tool_call(ctx)
        assert not ctx.has_force_finish_request


@pytest.mark.asyncio
async def test_shared_summary_rail_does_not_finish_normal_agent():
    rail = SkillTurboDeliverySummaryRail()
    session = _Session()
    async with _tool_context([rail], session) as ctx:
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert [event.payload["content"] for event in session.events] == [_SUMMARY]
        assert not ctx.has_force_finish_request


class _DeliveryTool(Tool):
    def __init__(self, delivery_status):
        super().__init__(ToolCard(name=_TOOL, description="测试 PPT 交付"))
        self.delivery_status = delivery_status
        self.calls = 0

    async def invoke(self, inputs, **kwargs):
        assert "finish_request" not in inputs
        self.calls += 1
        holder = _holder()
        holder["p10_delivery"]["info"]["delivery_status"] = self.delivery_status
        return turbo_tools._wrap_skill_turbo_result(
            {"success": True, "result": "任务已完成"}, holder,
        )

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class _DeliveryModel:
    def __init__(self, tool):
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid",
        )
        self.model_config = ModelRequestConfig(model_name="fake")
        self.tool = tool
        self.calls = []

    def _reply(self, messages):
        self.calls.append(list(messages))
        if self.tool.calls:
            # 修复前本轮会继续调模型，从而多输出这段文本。
            return AssistantMessage(content="模型后续回复")
        return AssistantMessage(content="", tool_calls=[
            ToolCall(id="outer-ppt", type="function", name=_TOOL, arguments='{"finish_request": true}'),
        ])

    async def invoke(self, messages, **kwargs):
        self._check_finish_schema(kwargs)
        return self._reply(messages)

    async def stream(self, messages, **kwargs):
        self._check_finish_schema(kwargs)
        message = self._reply(messages)
        yield AssistantMessageChunk(content=message.content, tool_calls=message.tool_calls)

    @staticmethod
    def _check_finish_schema(kwargs):
        tool = next(tool for tool in kwargs.get("tools", []) if tool.name == _TOOL)
        assert "finish_request" in tool.parameters.get("properties", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "invoke"])
@pytest.mark.parametrize("delivery_status", ["ok", "partial"])
@pytest.mark.parametrize("max_iterations", [1, 3])
async def test_flash_summary_finishes_real_sdk_loop_and_next_turn_still_works(
    assemble_flash, monkeypatch, tmp_path, mode, delivery_status, max_iterations,
):
    # 不发真实模型请求；禁掉与本测试无关的后台图片能力探测。
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    adapter, rails = assemble_flash()
    rails = [r for r in rails if isinstance(r, (JiuSwarmStreamEventRail, SkillTurboDeliverySummaryRail))]
    tool = _DeliveryTool(delivery_status)
    model = _DeliveryModel(tool)
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[tool], rails=rails, system_prompt="Test",
        enable_task_loop=False, max_iterations=max_iterations, workspace=str(tmp_path),
    )
    session = Session(session_id=f"flash-finish-{uuid4().hex}", card=core.card)
    try:
        # Direct invoke/stream tests must not also start the interaction reader.
        await core._ensure_initialized()
        await session.pre_run(inputs={"query": "生成 PPT"})
        if mode == "stream":
            chunks = [chunk async for chunk in core.stream({"query": "生成 PPT"}, session=session)]
            events = [c for c in chunks if getattr(c, "type", None) in {"tool_result", "llm_output", "answer"}]
            assert [e.type for e in events] == ["tool_result", "llm_output", "answer"]
            assert events[1].payload["content"] == _SUMMARY
            # 用真实 adapter 转换结束帧，确认已有 delta 不会再成为一段正文。
            final = adapter._parse_stream_chunk(
                events[-1], _has_streamed_content=True, _streamed_text=_SUMMARY,
            )
            assert final == {"event_type": "chat.final", "content": ""}
            assert adapter._drain_final_content_to_deltas(
                final, segment_streamed_text=_SUMMARY, chunk_payload=events[-1].payload,
            ) == []
        else:
            result = await core.invoke({"query": "生成 PPT"}, session=session)
            assert result == {"output": _SUMMARY, "result_type": "answer"}
        assert len(model.calls) == 1
        assert tool.calls == 1

        # 同一实例与会话的下一轮必须继续调模型，不能残留结束信号。
        await session.post_run()
        session = Session(session_id=session.get_session_id(), card=core.card)
        await session.pre_run(inputs={"query": "继续聊天"})
        result = await core.invoke({"query": "继续聊天"}, session=session)
        assert result["output"] == "模型后续回复"
        assert len(model.calls) == 2
        assert tool.calls == 1
        assert any(getattr(message, "role", None) == "tool" for message in model.calls[-1])
        summaries = [message for message in model.calls[-1] if message.content == _SUMMARY]
        assert len(summaries) == 1 and summaries[0].role == "assistant"
        tool_index = next(i for i, message in enumerate(model.calls[-1]) if message.role == "tool")
        assert model.calls[-1].index(summaries[0]) > tool_index
        await session.post_run()
    finally:
        await core.stop()


@pytest.mark.asyncio
async def test_finish_parameter_only_changes_flash_model_input():
    rail = FlashDeliverySummaryRail()
    original = ToolInfo(name=_TOOL, parameters={"type": "object", "properties": {"query": {"type": "string"}}})
    other = ToolInfo(name="other", parameters={"type": "object"})
    ctx = AgentCallbackContext(agent=SimpleNamespace(), inputs=ModelCallInputs(tools=[original, other]))
    await rail.before_model_call(ctx)
    assert "finish_request" not in original.parameters.get("properties", {})
    assert ctx.inputs.tools[0].parameters.get("properties", {}).get("finish_request", {}).get("default") is False
    assert ctx.inputs.tools[1] is other


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [None, False, "true", 1])
async def test_missing_or_non_boolean_finish_flag_continues(assemble_flash, finish):
    _, rails = assemble_flash()
    rail = next(r for r in rails if isinstance(r, FlashDeliverySummaryRail))
    async with _tool_context(rails, _Session()) as ctx:
        tool_inputs = ctx.inputs
        args = {} if finish is None else {"finish_request": finish}
        tool_inputs.tool_call.arguments = json.dumps(args)
        ctx.inputs = ModelCallInputs(response=AssistantMessage(content="", tool_calls=[tool_inputs.tool_call]))
        await rail.after_model_call(ctx)
        ctx.inputs = tool_inputs
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        await rail.after_tool_call(ctx)
        await rail.after_react_iteration(ctx)
        assert not ctx.has_force_finish_request
        assert [m.content for m in ctx.context.get_messages()] == [_SUMMARY]
        assert "finish_request" not in json.loads(tool_inputs.tool_call.arguments)


@pytest.mark.asyncio
async def test_pending_steering_prevents_finish(assemble_flash):
    _, rails = assemble_flash()
    rail = next(r for r in rails if isinstance(r, FlashDeliverySummaryRail))
    async with _tool_context(rails, _Session()) as ctx:
        queue = asyncio.Queue()
        ctx.bind_steering_queue(queue)
        ctx.push_steering("再生成一份 PPT")
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        await rail.after_tool_call(ctx)
        await rail.after_react_iteration(ctx)
        assert not ctx.has_force_finish_request
        assert ctx.drain_steering() == ["再生成一份 PPT"]


@pytest.mark.asyncio
async def test_concurrent_requests_keep_separate_delivery_history(assemble_flash):
    _, rails = assemble_flash()
    rail = next(r for r in rails if isinstance(r, FlashDeliverySummaryRail))
    barrier = asyncio.Barrier(2)

    async def deliver(label):
        summary = f"{_SUMMARY}\n{label}"
        async with _tool_context([rail], _Session()) as ctx:
            turbo_tools.set_pending_ppt_delivery_summary(summary)
            await rail.after_tool_call(ctx)
            await barrier.wait()
            await rail.after_react_iteration(ctx)
            assert ctx.consume_force_finish().result.get("output") == summary
            assert [m.content for m in ctx.context.get_messages()] == [summary]

    await asyncio.gather(deliver("会话 A"), deliver("会话 B"))


@pytest.mark.asyncio
async def test_history_write_failure_cannot_finish_request(assemble_flash):
    _, rails = assemble_flash()
    rail = next(r for r in rails if isinstance(r, FlashDeliverySummaryRail))
    async with _tool_context(rails, _Session()) as ctx:
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        await rail.after_tool_call(ctx)
        add_messages = ctx.context.add_messages
        ctx.context.add_messages = AsyncMock(side_effect=OSError("历史保存失败"))
        with pytest.raises(OSError, match="历史保存失败"):
            await rail.after_react_iteration(ctx)
        assert not ctx.has_force_finish_request
        ctx.context.add_messages = add_messages
        await rail.after_react_iteration(ctx)
        assert ctx.consume_force_finish().result.get("output") == _SUMMARY
        assert [m.content for m in ctx.context.get_messages()] == [_SUMMARY]


class _ApprovalTool(Tool):
    def __init__(self):
        super().__init__(ToolCard(name="approval_tool", description="测试审批中断"))

    async def invoke(self, inputs, **kwargs):
        return {"success": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class _ApprovalRail(AgentRail):
    async def before_tool_call(self, ctx):
        if ctx.inputs.tool_name != "approval_tool" or ctx.extra.get(RESUME_USER_INPUT_KEY) is not None:
            return
        # 与 SDK InterruptRailBase 相同：AbortError 穿过 callback 框架，cause 进入 HITL。
        raise AbortError(
            reason="等待审批",
            cause=ToolInterruptException(
                request=InterruptRequest(message="请审批另一个操作"),
                tool_call=ctx.inputs.tool_call,
            ),
        )


class _ScriptedDeliveryModel(_DeliveryModel):
    def __init__(self, tool, replies):
        super().__init__(tool)
        self.replies = iter(replies)

    def _reply(self, messages):
        self.calls.append(list(messages))
        return next(self.replies)


def _ppt_call(call_id, finish):
    return ToolCall(
        id=call_id, type="function", name=_TOOL,
        arguments=json.dumps({"finish_request": finish}),
    )


@asynccontextmanager
async def _real_delivery_agent(assemble_flash, monkeypatch, tmp_path, replies, extra_tools=(), extra_rails=()):
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    _, rails = assemble_flash()
    rails = [r for r in rails if isinstance(r, (JiuSwarmStreamEventRail, SkillTurboDeliverySummaryRail))]
    rails.extend(extra_rails)
    tool = _DeliveryTool("ok")
    model = _ScriptedDeliveryModel(tool, replies)
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[tool, *extra_tools], rails=rails, system_prompt="Test",
        enable_task_loop=False, max_iterations=4, workspace=str(tmp_path),
    )
    session = Session(session_id=f"flash-review-{uuid4().hex}", card=core.card)
    try:
        await core._ensure_initialized()
        await session.pre_run(inputs={"query": "完成全部任务"})
        yield core, session, tool, model
        await session.post_run()
    finally:
        await core.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("approval_first", [False, True])
async def test_same_batch_approval_is_saved_with_delivered_summary(
    assemble_flash, monkeypatch, tmp_path, mode, approval_first,
):
    calls = [_ppt_call("ppt-1", True), ToolCall(id="approval-1", type="function", name="approval_tool", arguments="{}")]
    if approval_first:
        calls.reverse()
    replies = [AssistantMessage(content="", tool_calls=calls), AssistantMessage(content="审批后的操作已完成")]
    async with _real_delivery_agent(
        assemble_flash, monkeypatch, tmp_path, replies, [_ApprovalTool()], [_ApprovalRail()],
    ) as (core, session, tool, model):
        if mode == "invoke":
            result = await core.invoke({"query": "生成 PPT 并执行需要审批的操作"}, session=session)
            assert result.get("result_type") == "interrupt"
        else:
            chunks = [c async for c in core.stream({"query": "生成 PPT 并执行需要审批的操作"}, session=session)]
            assert not any(c.type == "answer" for c in chunks)
            assert sum(c.type == "llm_output" and c.payload.get("content") == _SUMMARY for c in chunks) == 1
        interrupt = session.get_state(INTERRUPTION_KEY)
        assert isinstance(interrupt, ToolInterruptionState)
        assert "approval-1" in interrupt.interrupted_tools
        assert len(model.calls) == 1 and tool.calls == 1
        # 重新从 session 持久化快照装载，不能只检查当前内存对象。
        context = await core._react_agent.context_engine.create_context(session=session)
        messages = context.get_messages()
        summary_index = next(i for i, message in enumerate(messages) if message.content == _SUMMARY)
        assert messages[summary_index].role == "assistant"
        assert sum(message.content == _SUMMARY for message in messages) == 1
        tool_indices = [i for i, message in enumerate(messages) if message.role == "tool"]
        assert len(tool_indices) == 1 and summary_index > max(tool_indices)
        assert messages[tool_indices[0]].tool_call_id == "ppt-1"
        # 恢复只执行被中断的工具，不能重发已完成的 PPT 或总结。
        answers = InteractiveInput()
        answers.update("approval-1", {"approved": True})
        result = await core.invoke(answers, session=session)
        assert result.get("result_type") == "answer"
        assert session.get_state(INTERRUPTION_KEY) is None
        assert tool.calls == 1
        assert sum(m.content == _SUMMARY for m in model.calls[-1]) == 1
        tool_messages = [m for m in model.calls[-1] if m.role == "tool"]
        assert {m.tool_call_id for m in tool_messages} == {"ppt-1", "approval-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
async def test_two_serial_deliveries_finish_only_after_second(
    assemble_flash, monkeypatch, tmp_path, mode,
):
    replies = [
        AssistantMessage(content="", tool_calls=[_ppt_call("ppt-1", False)]),
        AssistantMessage(content="", tool_calls=[_ppt_call("ppt-2", True)]),
    ]
    async with _real_delivery_agent(
        assemble_flash, monkeypatch, tmp_path, replies,
    ) as (core, session, tool, model):
        if mode == "invoke":
            result = await core.invoke({"query": "串行生成两份 PPT"}, session=session)
            assert result.get("output") == f"{_SUMMARY}\n\n{_SUMMARY}"
        else:
            chunks = [c async for c in core.stream({"query": "串行生成两份 PPT"}, session=session)]
            events = [c for c in chunks if c.type in {"tool_result", "llm_output", "answer"}]
            assert [c.type for c in events] == ["tool_result", "llm_output", "tool_result", "llm_output", "answer"]
        assert tool.calls == 2 and len(model.calls) == 2
        assert sum(m.content == _SUMMARY for m in model.calls[-1]) == 1
        first_result = next(m.content for m in model.calls[-1] if m.role == "tool")
        assert "Continue any remaining" in first_result
        assert "todo_modify" not in first_result
        assert "禁止再调" not in first_result
        context = await core._react_agent.context_engine.create_context(session=session)
        assert sum(m.content == _SUMMARY for m in context.get_messages()) == 2


@pytest.mark.asyncio
async def test_delivery_does_not_truncate_followup_text_task(assemble_flash, monkeypatch, tmp_path):
    replies = [
        AssistantMessage(content="", tool_calls=[_ppt_call("ppt-1", False)]),
        AssistantMessage(content="这是用户另外要求的邮件草稿"),
    ]
    async with _real_delivery_agent(
        assemble_flash, monkeypatch, tmp_path, replies,
    ) as (core, session, tool, model):
        result = await core.invoke({"query": "生成 PPT，再写一封邮件"}, session=session)
        assert result.get("output") == "这是用户另外要求的邮件草稿"
        assert tool.calls == 1 and len(model.calls) == 2
        assert sum(m.content == _SUMMARY for m in model.calls[-1]) == 1
