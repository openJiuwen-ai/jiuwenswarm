# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash assembly and real SDK HITL; model and research service are isolated.

Unrelated filesystem, skills and MCP startup are omitted. Skill activation is
supplied through the existing provider; no production skill or checkpoint is read.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
import tiktoken
from openjiuwen.core.foundation.llm import AssistantMessageChunk, ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.interaction import SendInputRequest

from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
    DEEPRESEARCH_EXECUTION_STATE_KEY,
    DeepResearchExecutionRail,
)
from jiuwenswarm.agents.harness.common.tools import deepresearch
from jiuwenswarm.agents.harness.common.tools.deepresearch import execution
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)

_RESEARCH_RAILS = {
    "_deepresearch_execution_rail", "_stream_event_rail",
    "_progressive_tool_rail", "_disabled_tools_rail",
}


def _build_rails(monkeypatch, adapter, config):
    instantiate = adapter._instantiate_rails

    def focused(infos, config_base):
        return instantiate(
            [info for info in infos if info.attr_name in _RESEARCH_RAILS], config_base
        )

    monkeypatch.setattr(adapter, "_instantiate_rails", focused)
    monkeypatch.setattr(adapter, "_load_extra_rails_from_env", list)
    monkeypatch.setattr(adapter, "_build_flash_skill_selection_rail", lambda _: None)
    return adapter._build_agent_rails(config["react"], config, mode="flash")


@pytest.fixture
def setup(monkeypatch, tmp_path):
    config = {
        "memory": {"engine": "none"},
        "react": {"tool_lazy_load": {"enabled": True}},
    }
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(deepresearch, "enable_deepresearch", lambda: True)
    monkeypatch.setattr(execution, "_scan_resumable_checkpoint_by_query", lambda _: None)
    monkeypatch.setattr(execution, "_probe_resumable_checkpoint", lambda _: None)
    monkeypatch.setattr(execution, "salvage_deepresearch_report", AsyncMock(return_value=None))
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _: SimpleNamespace(
        encode=lambda text, **kwargs: list(text),
    ))
    adapter = JiuwenSwarmFlashAdapter(workspace_dir=str(tmp_path))
    config = adapter._apply_flash_react_override(config)
    adapter._config_base_cache = config
    adapter._config_cache = config["react"]
    session_id = "flash-research-" + uuid4().hex
    adapter._current_request_route = {
        "session_id": session_id, "request_id": session_id + "-request",
        "channel_id": "officeclaw", "output_dir": str(tmp_path),
    }
    active_skill = {"name": "deepresearch"}
    monkeypatch.setattr(adapter, "_get_progressive_active_skill", lambda: active_skill["name"])
    return adapter, config, active_skill


def test_cold_build_keeps_research_execution_bridge(monkeypatch, setup):
    adapter, config, _ = setup
    rails = _build_rails(monkeypatch, adapter, config)
    assert isinstance(adapter._deepresearch_execution_rail, DeepResearchExecutionRail)
    assert rails.count(adapter._deepresearch_execution_rail) == 1
    assert adapter._deepresearch_execution_rail.priority > adapter._stream_event_rail.priority
    assert adapter._context_overflow_recovery_rail is None
    assert config["react"]["enable_task_loop"] is False


class _Model:
    def __init__(self, report_type="professional"):
        self.calls = []
        self.messages = []
        self.report_type = report_type
        self.invoke = AsyncMock(return_value=SimpleNamespace(content=json.dumps({
            "items": [{"question_index": 0, "options": [{"label": "国内"}, {"label": "海外"}]}],
        })))

    async def stream(self, *, messages, tools=None, **kwargs):
        names = {getattr(tool, "name", "") for tool in tools or []}
        self.calls.append(names)
        self.messages.append(messages)
        if "deepresearch_execute" not in names:
            yield AssistantMessageChunk(content="NO_RESEARCH_TOOL")
        elif len(self.calls) == 1:
            yield AssistantMessageChunk(content="", tool_calls=[ToolCall(
                id="research-call", type="function", name="deepresearch_execute",
                arguments=json.dumps({"query": "研究测试主题", "report_type": self.report_type}),
            )])
        else:
            yield AssistantMessageChunk(content="UNEXPECTED_EXTRA_MODEL_CALL")


@pytest_asyncio.fixture
async def runtime(monkeypatch, setup):
    adapter, config, _ = setup
    agents = []
    # Own the Runner lifecycle on this fixture's event loop.
    await Runner.start()

    async def create(*, enabled=True, disabled=False, report_type="professional"):
        monkeypatch.setattr(deepresearch, "enable_deepresearch", lambda: enabled)
        if disabled:
            config["react"]["disabled_tools"] = ["deepresearch_execute"]
        rails = _build_rails(monkeypatch, adapter, config)
        cards = []
        adapter._register_deepresearch_tool_cards(cards)
        agent = DeepAgent(AgentCard(id="flash-" + uuid4().hex, name="flash-research"))
        agent.configure(DeepAgentConfig(
            rails=rails, tools=cards, enable_task_loop=False,
            enable_read_image_multimodal=False, system_prompt="Research integration test",
            max_iterations=2,
        ))
        for card in cards:
            agent.ability_manager.add(card)
        agents.append(agent)
        await agent.ensure_initialized()
        model = _Model(report_type)
        adapter._model = model
        adapter._instance = agent
        monkeypatch.setattr(agent._react_agent, "_get_llm", lambda: model)
        await adapter._update_rails_for_mode("flash")
        await adapter.start_interaction(adapter._current_request_route["session_id"])
        return agent, model

    yield create
    try:
        for agent in agents:
            await agent.stop()
            # The SDK pinned by CI does not expose aclose().
            aclose = getattr(agent, "aclose", None)
            if callable(aclose):
                await aclose()
    finally:
        await Runner.stop()


async def _turn(agent, query):
    stream = await agent.attach_output()
    assert stream is not None
    try:
        await agent.send_input(SendInputRequest(
            request_id=uuid4().hex, inputs={"query": query},
        ))
        async with asyncio.timeout(10):
            return [chunk async for chunk in stream]
    finally:
        await stream.close(abort_active_round=False)


def _answer(agent, selection="", *, status="answered"):
    state = agent._interaction_session.get_state(INTERRUPTION_KEY)
    assert state is not None
    ids = list(state.interrupted_tools)
    assert len(ids) == 1
    answer = InteractiveInput()
    answer.update(ids[0], {"status": status, "answers": [{"selected_options": [selection]}]})
    return answer


def _result(status="completed", **extra):
    return json.dumps({"status": status, "conversation_id": "research-conversation", **extra})


@pytest.mark.asyncio
async def test_native_clarification_outline_and_resume(monkeypatch, runtime, setup):
    adapter, _, _ = setup
    sdk = AsyncMock(side_effect=[
        _result("interrupted", node_id="feedback_handler", marker={"questions": "1. 研究哪个市场？"}),
        _result("interrupted", node_id="outline_interaction", marker={"outline": "### P1: 市场格局"}),
        _result(report_delivered=True),
    ])
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, model = await runtime()
    await _turn(agent, "研究测试主题")
    session = agent._interaction_session
    assert session.get_state(DEEPRESEARCH_EXECUTION_STATE_KEY)["research-call"]["phase"] == "wait_feedback"
    await adapter._update_rails_for_mode("flash")
    await _turn(agent, _answer(agent, "国内"))
    assert session.get_state(DEEPRESEARCH_EXECUTION_STATE_KEY)["research-call"]["phase"] == "wait_outline"
    chunks = await _turn(agent, _answer(agent, "确认大纲，继续研究"))
    assert [call.kwargs["action"] for call in sdk.await_args_list] == ["start", "resume", "resume"]
    assert all(call.kwargs["conversation_id"] == "research-conversation" for call in sdk.await_args_list[1:])
    assert all(call.kwargs["report_type"] == "professional" for call in sdk.await_args_list)
    assert len(model.calls) == 1
    assert not session.get_state(INTERRUPTION_KEY)
    states = session.get_state(DEEPRESEARCH_EXECUTION_STATE_KEY) or {}
    assert all(state["phase"] == "completed" for state in states.values())
    assert "深度研究已完成" in str(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,expected", [
    (_result(report_delivered=True), "深度研究已完成"),
    (_result("error", error_code="runner_failed", error="controlled failure"), "执行失败"),
    (_result(report_delivered=False), "执行失败"),
])
async def test_brief_and_failed_delivery_finish_without_extra_model(monkeypatch, runtime, outcome, expected):
    sdk = AsyncMock(return_value=outcome)
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, model = await runtime(report_type="brief")
    chunks = await _turn(agent, "研究测试主题")
    assert expected in str(chunks)
    assert len(model.calls) == 1
    assert sdk.await_count == 1
    assert sdk.await_args.kwargs["report_type"] == "brief"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "inactive_skill", "feature_off"])
async def test_existing_tool_gates_still_apply(monkeypatch, runtime, setup, gate):
    _, _, active_skill = setup
    if gate == "inactive_skill":
        active_skill["name"] = None
    sdk = AsyncMock()
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, model = await runtime(enabled=gate != "feature_off", disabled=gate == "disabled")
    await _turn(agent, "研究测试主题")
    assert "deepresearch_execute" not in model.calls[0]
    sdk.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_clarification_does_not_resume_research(monkeypatch, runtime):
    sdk = AsyncMock(return_value=_result(
        "interrupted", node_id="feedback_handler", marker={"questions": "1. 研究哪个市场？"},
    ))
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, model = await runtime()
    await _turn(agent, "研究测试主题")
    chunks = await _turn(agent, _answer(agent, status="cancelled"))
    assert "任务已取消" in str(chunks)
    assert not agent._interaction_session.get_state(INTERRUPTION_KEY)
    assert len(model.calls) == 1
    assert sdk.await_count == 1


@pytest.mark.asyncio
async def test_partial_reconfigure_retains_bridge_and_updates_model(monkeypatch, runtime, setup):
    adapter, _, _ = setup
    sdk = AsyncMock(return_value=_result(
        "interrupted", node_id="feedback_handler", marker={"questions": "1. 研究哪个市场？"},
    ))
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, old_model = await runtime()
    bridge = adapter._deepresearch_execution_rail
    agent.configure(replace(agent.deep_config, rails=[]))
    await agent.ensure_initialized()
    model = _Model()
    adapter._model = model
    monkeypatch.setattr(agent._react_agent, "_get_llm", lambda: model)
    await adapter._update_rails_for_mode("flash")
    assert agent._registered_rails.count(bridge) == 1
    await _turn(agent, "研究测试主题")
    model.invoke.assert_awaited_once()
    old_model.invoke.assert_not_awaited()
    assert sdk.await_count == 1
    assert agent._interaction_session.get_state(INTERRUPTION_KEY)


@pytest.mark.asyncio
async def test_detaching_output_does_not_restart_research(monkeypatch, runtime):
    started, release = asyncio.Event(), asyncio.Event()

    async def research(**kwargs):
        started.set()
        await release.wait()
        return _result(report_delivered=True)

    sdk = AsyncMock(side_effect=research)
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, model = await runtime()
    original = await agent.attach_output()
    try:
        await agent.send_input(SendInputRequest(request_id=uuid4().hex, inputs={"query": "研究测试主题"}))
        await asyncio.wait_for(started.wait(), timeout=5)
        await original.close(abort_active_round=False)
        resumed = await agent.attach_output()
        assert resumed is not None
        try:
            release.set()
            async with asyncio.timeout(5):
                chunks = [chunk async for chunk in resumed]
        finally:
            await resumed.close(abort_active_round=False)
    finally:
        release.set()
        await original.close(abort_active_round=False)
    assert "深度研究已完成" in str(chunks)
    assert sdk.await_count == 1
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_stop_cancels_active_research_without_restart(monkeypatch, runtime):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def research(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sdk = AsyncMock(side_effect=research)
    monkeypatch.setattr(execution, "_call_deepresearch_stream_impl", sdk)
    agent, model = await runtime()
    turn = asyncio.create_task(_turn(agent, "研究测试主题"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await agent._cancel_active_round(reason="user_stop")
        await asyncio.wait_for(turn, timeout=5)
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
    assert cancelled.is_set()
    assert sdk.await_count == 1
    assert "请勿代替用户决定继续" in str(model.messages[-1])
