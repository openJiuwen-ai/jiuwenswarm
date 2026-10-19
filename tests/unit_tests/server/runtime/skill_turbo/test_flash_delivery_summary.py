# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""验证 Flash 实际装配及 SDK 回调链中的 Turbo 总结时序与恢复去重。"""

from __future__ import annotations

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
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    ToolCallInputs,
)
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.flash.rails.flash_delivery_summary_rail import FlashDeliverySummaryRail
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
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
        adapter._build_skill_turbo_prompt_rail.assert_not_called()
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
    agent = SimpleNamespace(agent_callback_manager=manager)
    # 故意反向注册，顺序必须由真实 SDK priority 决定，不能靠测试手工排序。
    for rail in reversed(rails):
        if isinstance(rail, (JiuSwarmStreamEventRail, SkillTurboDeliverySummaryRail)):
            await manager.register_rail(rail, agent)
    call = ToolCall(id="outer-ppt", type="function", name=_TOOL, arguments="{}")
    ctx = AgentCallbackContext(
        agent=agent, session=session,
        inputs=ToolCallInputs(tool_call=call, tool_name=_TOOL, tool_args={}),
    )
    try:
        yield ctx
    finally:
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
        finish = ctx.consume_force_finish()
        assert finish.result == {"output": _SUMMARY, "result_type": "answer"}
        # 缓存消费后，重复调用总结组件不能再投递同一份正文。
        await adapter._skill_turbo_delivery_summary_rail.after_tool_call(ctx)
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
        assert ctx.consume_force_finish().result["output"] == _SUMMARY
        await adapter._skill_turbo_delivery_summary_rail.after_tool_call(ctx)
        assert len(session.events) == 2
        assert not ctx.has_force_finish_request


@pytest.mark.asyncio
async def test_flash_adapter_resume_keeps_existing_summary_single(monkeypatch, assemble_flash):
    adapter, rails = assemble_flash()
    assert adapter._skill_turbo_delivery_summary_rail is not None
    session = _Session()

    # 审批暂停先清残留，再进入绕过外层工具循环的 adapter resume 入口。
    async with _tool_context(rails, session) as ctx:
        turbo_tools.set_pending_ppt_delivery_summary(_SUMMARY)
        ctx.exception = ToolInterruptException(request=InterruptRequest(message="请确认"))
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
        assert session.events == []

    class _Turbo:
        def __init__(self, _config):
            self.artifact_holder = _holder()

        async def resume_stream(self, **_kwargs):
            if False:
                yield None

    monkeypatch.setattr("jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo", _Turbo)
    monkeypatch.setattr(adapter, "_clear_pending_skill_turbo_hitl", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_clear_skill_turbo_resume_ctx_via_isolated_session", AsyncMock())
    monkeypatch.setattr(interface_deep, "_skill_turbo_clear_resume_in_flight", AsyncMock())
    monkeypatch.setattr(adapter, "build_skill_turbo_config", lambda: {})
    monkeypatch.setattr(adapter, "_log_and_make_usage_summary_chunk", lambda **_: None)
    monkeypatch.setattr(adapter, "_complete_skill_turbo_outer_todo", AsyncMock(return_value=[]))
    request = AgentRequest(
        request_id="flash-resume", session_id="flash-session", channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"source": "ask_user_interrupt", "answers": [{"question": "风格"}]},
    )
    stream = adapter._make_skill_turbo_resume_stream(
        request=request, inputs={}, session=session,
        resume_ctx={"plan_code": "test", "pending_tool_call_id": "outer-ppt", "inputs": {}},
        answers=[{"question": "风格", "selected_options": ["商务"]}],
    )
    chunks = [chunk async for chunk in stream]
    payloads = [chunk.payload for chunk in chunks if isinstance(chunk.payload, dict)]
    assert [p["content"] for p in payloads if p.get("event_type") == "chat.delta"] == [_SUMMARY]
    assert [p["content"] for p in payloads if p.get("event_type") == "chat.final"] == [""]
    assert chunks[-1].is_complete is True
    assert turbo_tools.take_pending_ppt_delivery_summary() == ""
    # resume 已输出终稿，之后即使收到外层收尾回调也不能再补一份。
    async with _tool_context(rails, session) as ctx:
        ctx.inputs.tool_result = {"success": True}
        await ctx.fire(AgentCallbackEvent.AFTER_TOOL_CALL)
    assert [event.type for event in session.events] == ["tool_result"]
    assert not ctx.has_force_finish_request


@pytest.mark.asyncio
async def test_flash_duplicate_resume_answer_does_not_start_another_delivery(monkeypatch, assemble_flash):
    adapter, _ = assemble_flash()
    adapter._instance = SimpleNamespace(card=object(), _loop_session=None)
    resume = Mock(side_effect=AssertionError("重复作答不得重新执行交付"))
    monkeypatch.setattr(adapter, "_make_skill_turbo_resume_stream", resume)
    session = SimpleNamespace(post_run=AsyncMock(), update_state=Mock())
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", lambda **_: session)
    monkeypatch.setattr(interface_deep, "_skill_turbo_set_agent_id", lambda *_a, **_k: None)
    monkeypatch.setattr(interface_deep, "_skill_turbo_load_resume_ctx", AsyncMock(return_value={
        "pending_tool_call_id": "skill_turbo-tc-ask_user-1",
        "plan_code": "test", "resume_in_flight": True,
    }))
    request = AgentRequest(
        request_id="flash-duplicate", channel_id="web", session_id="flash-session",
        req_method=ReqMethod.CHAT_SEND, params={
            "source": "ask_user_interrupt", "request_id": "skill_turbo-tc-ask_user-1",
            "answers": [{"question": "风格", "selected_options": ["商务"]}],
        },
    )
    stream = await adapter._try_skill_turbo_resume(request, {})
    assert stream is not None
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 1 and chunks[0].is_complete and chunks[0].payload is None
    resume.assert_not_called()


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
            ToolCall(id="outer-ppt", type="function", name=_TOOL, arguments="{}"),
        ])

    async def invoke(self, messages, **kwargs):
        return self._reply(messages)

    async def stream(self, messages, **kwargs):
        message = self._reply(messages)
        yield AssistantMessageChunk(content=message.content, tool_calls=message.tool_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "invoke"])
@pytest.mark.parametrize("delivery_status", ["ok", "partial"])
async def test_flash_summary_finishes_real_sdk_loop_and_next_turn_still_works(
    assemble_flash, monkeypatch, tmp_path, mode, delivery_status,
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
        enable_task_loop=False, max_iterations=3, workspace=str(tmp_path),
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
        await session.post_run()
    finally:
        await core.stop()
