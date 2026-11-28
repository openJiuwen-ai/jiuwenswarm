# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application turns on the existing Agent, with real checkpoints and history."""

import copy
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from e2a.wire_codec import encode_agent_chunk_for_wire, parse_agent_server_wire_chunk
from openjiuwen.core.foundation.llm import AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall, UsageMetadata
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.history_service import load_history_page
from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata, patch_session_metadata



TOOL = {"name": "set_counter", "description": "Set the counter", "parameters": {
    "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
}}


class ScriptedModel:
    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []
        self.model_client_config = ModelClientConfig(client_provider="OpenAI", api_key="test", api_base="http://localhost:1")
        self.model_config = ModelRequestConfig(model_name="client-test")

    async def invoke(self, messages, tools=None, **kwargs):
        self.seen.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        calls = self.steps.pop(0) if self.steps else []
        return AssistantMessage(content="" if calls else "done", tool_calls=calls,
                                usage_metadata=UsageMetadata(model_name="client-test", input_tokens=10,
                                                             output_tokens=2, total_tokens=12))

    async def stream(self, messages, tools=None, **kwargs):
        response = await self.invoke(messages, tools, **kwargs)
        yield AssistantMessageChunk(content=response.content, tool_calls=response.tool_calls,
                                    usage_metadata=response.usage_metadata)


def request(rid, **params):
    return AgentRequest(request_id=rid, channel_id="ag-ui", session_id="client-session",
                        req_method=ReqMethod.CHAT_SEND, is_stream=True, params=params)


@pytest_asyncio.fixture
async def host(monkeypatch):
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.core.session.agent import create_agent_session
    from jiuwenswarm.runtime.checkpointer import ensure_persistent_checkpointer, close_persistent_checkpointer
    from jiuwenswarm.runtime.client_tools import ApplicationInteractionRail
    from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    init_session_metadata(session_id="client-session", channel_id="ag-ui", mode="agent", user_id="u")
    await ensure_persistent_checkpointer()
    await Runner.start()
    model = ScriptedModel([])
    agent = create_deep_agent(
        model=model, card=AgentCard(id="existing-agent", name="existing-agent"),
        tool_owner_id="existing-agent-session", workspace=str(__import__('tempfile').mkdtemp()),
        enable_task_loop=True, enable_subagent_runtime=False, add_general_purpose_agent=False,
        enable_sys_operation=False, enable_read_image_multimodal=False,
        enable_model_anomaly_detection_rail=False, enable_security_rail=False,
        rails=[RuntimePromptRail(channel="ag-ui"), ApplicationInteractionRail()],
    )
    await agent.ensure_initialized()
    session = create_agent_session(session_id="client-session", card=agent.card)
    await session.pre_run(inputs={})
    await agent.start(session=session)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._channel_id = "ag-ui"
    adapter._parent_session_id = "client-session"
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _name="": True)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _req: model)
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_update_runtime_config", __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock())
    monkeypatch.setattr(adapter, "_bind_runtime_cron_context", lambda **kwargs: None)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", lambda _tokens: None)
    yield adapter, model
    await agent.stop()
    await Runner.stop()
    await close_persistent_checkpointer()


async def collect(host, rid, **params):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    adapter, _model = host
    facade = JiuWenSwarm.__new__(JiuWenSwarm)
    inputs, _, _ = facade._build_inputs(request(rid, **params))
    return [chunk.payload async for chunk in adapter.process_message_stream_impl(request(rid, **params), inputs)]


@pytest.mark.asyncio
async def test_existing_adapter_native_tools_resume_and_context(host):
    adapter, model = host
    model.steps = [[ToolCall(id="call-1", type="function", name="set_counter", arguments='{"value":3}')]]
    events = await collect(host, "run-1", query="set to 3", client_tools=[TOOL],
                           run_context={"state": {"counter": 0}, "metadata": {"secret": "never-in-prompt"}})
    assert events[0]["state"] == "accepted", events
    assert events[-1]["state"] == "waiting_client_tools", events
    [call] = [event for event in events if event.get("event_type") == "chat.client_tool_request"]
    messages = "\n".join(str(message.content) for message in model.seen[0][0])
    assert '\"counter\": 0' in messages and "never-in-prompt" not in messages
    events = await collect(host, "run-2", source="client_tool_interrupt", client_tools=[],
                           answers=[{"request_id": "call-1", "interrupt_id": call["interrupt_id"],
                                     "result": {"content": [{"type": "text", "text": "updated"}]}}],
                           run_context={"state": {"counter": 3}})
    assert events[0]["state"] == "accepted", events
    assert events[-1]["state"] == "completed", events
    messages = "\n".join(str(message.content) for message in model.seen[-1][0])
    assert '\"counter\": 3' in messages and '\"counter\": 0' not in messages
    assert any(getattr(message, "tool_call_id", None) == "call-1" and "updated" in message.content
               for message in model.seen[-1][0])
    await collect(host, "run-3", query="hello")
    assert all('"counter"' not in str(message.content) for message in model.seen[-1][0])
    assert adapter._instance.card.id == "existing-agent"


@pytest.mark.asyncio
async def test_invalid_tools_rejected_before_model(host):
    events = await collect(host, "bad", query="hello", client_tools=[TOOL, TOOL])
    assert events[-1]["state"] == "rejected", events
    assert not host[1].seen


@pytest.mark.asyncio
async def test_backend_tool_survives_client_tool_replacement(host, tmp_path):
    from openjiuwen.core.foundation.tool import Tool, ToolCard

    output = tmp_path / "backend.txt"

    class BackendTool(Tool):
        async def invoke(self, inputs, **kwargs):
            output.write_text(inputs["text"])
            return "written"

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs)

    adapter, model = host
    backend = BackendTool(ToolCard(name="backend_write", description="Write a note", input_params={
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
    }))
    adapter._instance.ability_manager.add_ability(backend.card, backend)
    model.steps = [[ToolCall(id="write-1", type="function", name="backend_write", arguments='{"text":"kept"}')]]
    events = await collect(host, "backend", query="write", client_tools=[TOOL])
    assert events[-1]["state"] == "completed", events
    assert output.read_text() == "kept"
    assert {tool.name for tool in model.seen[0][1]} >= {"backend_write", "set_counter"}
    events = await collect(host, "remove-app", query="hello", client_tools=[])
    assert events[-1]["state"] == "completed", events
    assert adapter._instance.ability_manager.get("backend_write") is backend.card
    assert adapter._instance.ability_manager.get("set_counter") is None
    conflict = dict(TOOL, name="backend_write")
    events = await collect(host, "conflict", query="hello", client_tools=[conflict])
    assert events[-1]["state"] == "rejected", events
    assert adapter._instance.ability_manager.get("backend_write") is backend.card


@pytest.mark.asyncio
async def test_partial_and_duplicate_answers_preserve_pending_batch(host):
    adapter, model = host
    model.steps = [[ToolCall(id=f"call-{number}", type="function", name="set_counter", arguments='{"value":3}')
                    for number in (1, 2)]]
    events = await collect(host, "batch", query="both", client_tools=[TOOL])
    assert events[-1]["state"] == "waiting_client_tools", events
    pending = await adapter.pending_interactions("client-session")
    first = {key: pending[0][key] for key in ("request_id", "interrupt_id")}
    answer = {**first, "result": {"content": []}}
    for answers in ([answer], [answer, answer], [{**answer, "interrupt_id": "forged"}]):
        events = await collect(host, "bad-resume", source="client_tool_interrupt", answers=answers)
        assert events[-1]["state"] == "rejected", events
        assert await adapter.pending_interactions("client-session") == pending
    events = await collect(host, "resume-both", source="client_tool_interrupt", client_tools=[], answers=[
        {"request_id": item["request_id"], "interrupt_id": item["interrupt_id"], "result": {"content": []}}
        for item in pending
    ])
    assert events[-1]["state"] == "completed", events


@pytest.mark.asyncio
async def test_ask_user_is_hidden_and_hallucinated_call_cannot_wait(host):
    from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail

    adapter, model = host
    await adapter._instance.register_rail(StructuredAskUserRail())
    model.steps = [[ToolCall(id="ask", type="function", name="ask_user", arguments='{"query":"continue?"}')]]
    events = await collect(host, "ask", query="help")
    assert events[-1]["state"] == "completed", events
    assert not await adapter.pending_interactions("client-session")
    assert all(tool.name != "ask_user" for _, tools in model.seen for tool in tools or [])
    assert any("INTERACTION_UNAVAILABLE" in str(message.content) for message in model.seen[-1][0])


@pytest.mark.asyncio
async def test_context_raw_data_absent_from_persisted_checkpoint(host):
    from jiuwenswarm.common.utils import get_checkpoint_dir
    from jiuwenswarm.runtime.client_tools import _APPLICATION_RUNS

    marker = "raw-invocation-only-7418"
    events = await collect(host, "private", query="hello", run_context={"state": {"secret": marker}})
    assert events[-1]["state"] == "completed", events
    assert any(marker in str(message.content) for message in host[1].seen[-1][0])
    assert marker not in host[0]._instance.system_prompt_builder.build()
    for path in get_checkpoint_dir().rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes(), path
    assert not _APPLICATION_RUNS


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_metadata", [
    {"execution_policy": {"tool_execution": "client_only"}},
    {"client_execution": {"enabled": True}},
])
async def test_legacy_metadata_does_not_reject_session(host, legacy_metadata):
    patch_session_metadata("client-session", legacy_metadata)
    events = await collect(host, "legacy", query="hello")
    assert events[-1]["state"] == "completed", events
    assert host[1].seen


@pytest.mark.asyncio
@pytest.mark.parametrize("level,executed", [("allow", True), ("deny", False), ("ask", False)])
async def test_real_permission_rail_preserves_allow_deny_and_rejects_ask(host, tmp_path, level, executed):
    from openjiuwen.core.foundation.tool import Tool, ToolCard
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import JiuwenSwarmPermissionInterruptRail

    output = tmp_path / "permission-effect"

    class ProtectedTool(Tool):
        async def invoke(self, inputs, **kwargs):
            output.write_text("executed")
            return "ok"

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs)

    adapter, model = host
    backend = ProtectedTool(ToolCard(name="protected_action", input_params={"type": "object"}))
    adapter._instance.ability_manager.add_ability(backend.card, backend)
    rail = JiuwenSwarmPermissionInterruptRail(config={"enabled": True, "defaults": {"*": level}})
    adapter._permission_rail = rail
    await adapter._instance.register_rail(rail)
    model.steps = [[ToolCall(id="protected", type="function", name="protected_action", arguments="{}")]]
    events = await collect(host, "permission", query="do it")
    assert events[-1]["state"] == "completed", events
    assert output.exists() is executed
    assert not await adapter.pending_interactions("client-session")
    if level == "ask":
        assert any("approval interface" in str(message.content) for message in model.seen[-1][0])


@pytest.mark.asyncio
async def test_target_cancel_clears_wait_and_late_cancel_preserves_next_run(host):
    adapter, model = host
    model.steps = [[ToolCall(id="pending-call", type="function", name="set_counter", arguments='{"value":3}')]]
    events = await collect(host, "waiting", query="set", client_tools=[TOOL])
    assert events[-1]["state"] == "waiting_client_tools", events
    cancel = request("cancel", target_request_id="waiting", intent="cancel")
    response = await adapter.process_interrupt(cancel)
    assert response.payload["success"] is True, response
    assert not await adapter.pending_interactions("client-session")
    events = await collect(host, "next", query="hello")
    assert events[-1]["state"] == "completed", events
    response = await adapter.process_interrupt(cancel)
    assert response.payload["success"] is False


@pytest.mark.asyncio
async def test_target_cancel_commit_failure_returns_unconfirmed_and_can_retry(host, monkeypatch):
    from unittest.mock import AsyncMock

    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    adapter, model = host
    model.steps = [[ToolCall(id="pending-call", type="function", name="set_counter", arguments='{"value":3}')]]
    events = await collect(host, "waiting", query="set", client_tools=[TOOL])
    assert events[-1]["state"] == "waiting_client_tools"
    cancel = request("cancel", target_request_id="waiting", intent="cancel")
    session = adapter._instance.loop_session
    handoff = await adapter._permission_dispatch.acquire("client-session")
    with monkeypatch.context() as failure:
        failure.setattr(session, "commit", AsyncMock(side_effect=OSError("checkpoint unavailable")))
        failure.setattr(adapter._permission_dispatch, "start_cutover", AsyncMock(return_value=handoff))
        result = await adapter.process_interrupt(cancel)

    assert result.payload["success"] is False
    assert result.payload["error"] == "application_cancellation_persist_failed"
    assert not handoff.lock.locked()
    assert get_session_metadata("client-session")["application_execution"]["state"] == "waiting_client_tools"

    result = await adapter.process_interrupt(cancel)
    assert result.payload["success"] is True
    assert get_session_metadata("client-session")["application_execution"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_target_cancel_waits_for_actual_model_producer(host):
    adapter, model = host
    entered, stopped = asyncio.Event(), asyncio.Event()
    original = model.invoke

    async def blocked(messages, tools=None, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.invoke = blocked
    running = asyncio.create_task(collect(host, "blocked", query="wait"))
    await asyncio.wait_for(entered.wait(), 5)
    response = await adapter.process_interrupt(request("cancel", target_request_id="blocked", intent="cancel"))
    assert response.payload["success"] is True, response
    assert stopped.is_set()
    events = await asyncio.wait_for(running, 5)
    assert events[-1]["state"] != "completed", events
    model.invoke = original
    events = await collect(host, "after-stop", query="hello")
    assert events[-1]["state"] == "completed", events


@pytest_asyncio.fixture
async def facade(host, monkeypatch):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    adapter, _ = host
    swarm = JiuWenSwarm()
    swarm._adapter = adapter
    swarm._sdk_name = "test-native"
    monkeypatch.setattr(swarm, "reconcile_session_mcp", AsyncMock())
    monkeypatch.setattr(swarm, "_resolve_runtime_binding_for_request", AsyncMock(return_value=None))
    return swarm


@pytest.mark.asyncio
async def test_facade_history_and_wire_round_trip(facade, host):
    model = host[1]
    model.steps = [[ToolCall(id="history-call", type="function", name="set_counter", arguments='{"value":3}')]]
    chunks = [chunk async for chunk in facade.process_message_stream(request(
        "history-run", query="set it", mode="agent", client_tools=[TOOL],
        run_context={"state": {"secret": "history-private-state"}},
    ))]
    assert chunks[-1].payload["state"] == "waiting_client_tools", [chunk.payload for chunk in chunks]
    [call] = [chunk.payload for chunk in chunks if (chunk.payload or {}).get("event_type") == "chat.client_tool_request"]
    chunks = [chunk async for chunk in facade.deliver_control_input(request(
        "history-resume", source="client_tool_interrupt", mode="agent", answers=[{
            "request_id": "history-call", "interrupt_id": call["interrupt_id"],
            "result": {"content": [{"type": "text", "text": "history-tool-result"}]},
        }], client_tools=[], run_context={"state": {"counter": 3}},
    ))]
    assert chunks[-1].payload["state"] == "completed", [chunk.payload for chunk in chunks]
    page = load_history_page("client-session", 1)
    assert "history-private-state" not in str(page)
    assert "history-tool-result" in str(page)
    records = page["items"] if "items" in page else page["messages"]
    assert any(record.get("role") == "tool" and record.get("tool_call_id") == "history-call" for record in records), page
    assert any(record.get("tool_call_id") == "history-call" and record.get("request_id") == "history-run" for record in records), page
    wire = encode_agent_chunk_for_wire(chunks[-1], response_id="wire-test", sequence=1)
    assert parse_agent_server_wire_chunk(wire).payload["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("level,executed", [("allow", True), ("deny", False), ("ask", False)])
async def test_synchronous_child_permissions_without_application_tools(host, tmp_path, level, executed):
    from openjiuwen.core.foundation.tool import Tool, ToolCard
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.harness.schema.config import SubAgentConfig
    from openjiuwen.harness.rails import SubagentRail
    from jiuwenswarm.runtime.client_tools import ApplicationInteractionRail

    output = tmp_path / "child-effect"

    class ChildTool(Tool):
        async def invoke(self, inputs, **kwargs):
            output.write_text("child executed")
            return "child effect observed"

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs)

    adapter, model = host
    child_model = ScriptedModel([[ToolCall(id="child-call", type="function", name="child_action", arguments="{}")]])
    tool = ChildTool(ToolCard(name="child_action", input_params={"type": "object"}))
    spec = SubAgentConfig(
        agent_card=AgentCard(name="worker"), system_prompt="Complete the task.", model=child_model,
        tools=[tool], rails=[ApplicationInteractionRail()], factory_kwargs={
            "enable_sys_operation": False,
            "enable_model_anomaly_detection_rail": False, "enable_security_rail": False,
            "permissions": {"enabled": True, "defaults": {"*": level}},
        },
    )
    adapter._instance.deep_config.subagents = [spec]
    await adapter._instance.register_rail(SubagentRail(enable_subagent_runtime=False))
    model.steps = [[ToolCall(id="delegate", type="function", name="task_tool", arguments=__import__('json').dumps({
        "subagent_type": "worker", "task_description": "Do the action",
    }))]]
    events = await collect(host, "delegate", query="delegate", client_tools=[TOOL])
    assert events[-1]["state"] == "completed", events
    assert child_model.seen, [str(message.content) for message in model.seen[-1][0]]
    assert output.exists() is executed
    assert all(tool.name != "set_counter" for _, tools in child_model.seen for tool in tools or [])
    assert any(getattr(message, "tool_call_id", "") == "child-call" for message in child_model.seen[-1][0])


@pytest.mark.parametrize("params", [{"mode": "agent.code.normal"}, {"work_mode": "code"}, {"mode": "agent.work.plan"}, {"goal": "stay"}])
def test_runtime_rejects_unsupported_modes_before_routing(params):
    from jiuwenswarm.runtime import AgentRuntime
    init_session_metadata(session_id="client-session", channel_id="ag-ui", mode="agent", user_id="u")
    with pytest.raises(ValueError, match="ordinary Work"):
        AgentRuntime.normalize_request(request("unsupported", **params))


@pytest.mark.asyncio
async def test_checkpoint_failure_cannot_report_completed(host, monkeypatch):
    from unittest.mock import AsyncMock
    session = host[0]._instance.loop_session
    monkeypatch.setattr(session, "commit", AsyncMock(side_effect=OSError("checkpoint unavailable")))
    events = await collect(host, "commit-error", query="hello")
    assert events[-1]["state"] == "failed", events
    assert all(event.get("state") != "completed" for event in events)


@pytest.mark.asyncio
async def test_runtime_through_facade_uses_normal_chain_and_skips_plan(facade, host):
    from unittest.mock import AsyncMock
    from jiuwenswarm.runtime import AgentRuntime
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController

    manager, plan = FakeAgentManager(), FakePlanController()
    manager.agent = facade
    plan.ensure_state = AsyncMock(side_effect=AssertionError("AG-UI must not enter Plan"))
    plan.check_post_process_exit = AsyncMock(side_effect=AssertionError("AG-UI must not enter Plan"))
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=plan)
    model = host[1]
    model.steps = [[ToolCall(id="runtime-call", type="function", name="set_counter", arguments='{"value":3}')]]
    events = [event async for event in runtime.stream(request(
        "runtime-run", query="set", mode="agent", client_tools=[TOOL],
    ), trigger_hook=False)]
    assert events[-1].payload.get("state") == "waiting_client_tools", [event.payload for event in events]
    [call] = [event.payload for event in events if event.payload.get("event_type") == "chat.client_tool_request"]
    events = [event async for event in runtime.stream(request(
        "runtime-resume", mode="agent", source="client_tool_interrupt", answers=[{
            "request_id": "runtime-call", "interrupt_id": call["interrupt_id"], "result": {"content": []},
        }], client_tools=[], run_context={"state": {"counter": 3}},
    ), trigger_hook=False)]
    assert events[-1].payload.get("state") == "completed", [event.payload for event in events]
    assert not await runtime.pending_interactions("client-session")
    plan.ensure_state.assert_not_called()
    plan.check_post_process_exit.assert_not_called()


@pytest.mark.asyncio
async def test_facade_history_failure_cannot_report_success(facade, host, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    original = session_history._write_item

    def fail_output(session_id, item, *args, **kwargs):
        if item.get("request_id") == "write-failure" and item.get("role") == "assistant":
            raise OSError("history disk failed")
        return original(session_id, item, *args, **kwargs)

    monkeypatch.setattr(session_history, "_write_item", fail_output)
    chunks = []
    with pytest.raises(OSError, match="history disk failed"):
        async for chunk in facade.process_message_stream(request("write-failure", query="hello", mode="agent")):
            chunks.append(chunk)
    assert not any((chunk.payload or {}).get("state") == "completed" for chunk in chunks)


async def reopen_host(host):
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.core.session.agent import create_agent_session
    from jiuwenswarm.runtime.checkpointer import ensure_persistent_checkpointer, close_persistent_checkpointer
    from jiuwenswarm.runtime.client_tools import ApplicationInteractionRail
    from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail

    adapter, model = host
    old = adapter._instance
    card, workspace = old.card, old.deep_config.workspace
    await old.stop()
    old.ability_manager.teardown_tools()
    await Runner.stop()
    await close_persistent_checkpointer()
    await ensure_persistent_checkpointer()
    await Runner.start()
    agent = create_deep_agent(
        model=model, card=card, tool_owner_id="existing-agent-session", workspace=workspace,
        enable_task_loop=True, enable_subagent_runtime=False, add_general_purpose_agent=False,
        enable_sys_operation=False, enable_read_image_multimodal=False,
        enable_model_anomaly_detection_rail=False, enable_security_rail=False,
        rails=[RuntimePromptRail(channel="ag-ui"), ApplicationInteractionRail()],
    )
    await agent.ensure_initialized()
    session = create_agent_session(session_id="client-session", card=card)
    await session.pre_run(inputs={})
    await agent.start(session=session)
    adapter._instance = agent


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cold_reopen_waiting_checkpoint(host, cancel):
    adapter, model = host
    model.steps = [[ToolCall(id="cold-call", type="function", name="set_counter", arguments='{"value":3}')]]
    events = await collect(host, "cold-wait", query="set", client_tools=[TOOL])
    assert events[-1]["state"] == "waiting_client_tools", events
    pending = await adapter.pending_interactions("client-session")
    await reopen_host(host)
    assert await adapter.pending_interactions("client-session") == pending
    if cancel:
        response = await adapter.process_interrupt(request("cold-cancel", target_request_id="cold-wait", intent="cancel"))
        assert response.payload["success"] is True, response
        assert not await adapter.pending_interactions("client-session")
    else:
        events = await collect(host, "cold-resume", source="client_tool_interrupt", client_tools=[], answers=[{
            "request_id": "cold-call", "interrupt_id": pending[0]["interrupt_id"], "result": {"content": []},
        }], run_context={"state": {"counter": 3}})
        assert events[-1]["state"] == "completed", events
        assert any(getattr(message, "tool_call_id", "") == "cold-call" for message in model.seen[-1][0])
        assert all(tool.name != "set_counter" for tool in model.seen[-1][1] or [])


@pytest.mark.asyncio
async def test_disconnect_stops_model_and_releases_invocation_data(host):
    from jiuwenswarm.runtime.client_tools import _APPLICATION_RUNS
    adapter, model = host
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(messages, tools=None, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.invoke = blocked
    running = asyncio.create_task(collect(host, "disconnect", query="wait", run_context={"state": "private"}))
    await asyncio.wait_for(entered.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert stopped.is_set()
    assert not _APPLICATION_RUNS
    assert "application.invocation" not in adapter._instance.system_prompt_builder.build()


@pytest.mark.asyncio
async def test_disconnect_cleanup_timeout_preserves_cancellation(host, monkeypatch, caplog):
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.runtime.client_tools import APPLICATION_RUN_REF, _APPLICATION_RUNS

    adapter, _ = host
    entered = asyncio.Event()
    release = asyncio.Event()

    async def producer():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    async def submitted_stream(req, inputs):
        reference = inputs["run"]["context"]["extra"][APPLICATION_RUN_REF]
        _APPLICATION_RUNS[reference]["submitted"] = True
        yield AgentResponseChunk(request_id=req.request_id, channel_id=req.channel_id, is_complete=False,
                                 payload={"event_type": "runtime.accepted"})
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(producer())
    cancel_tasks = adapter._cancel_scheduler_running_tasks
    monkeypatch.setattr(adapter, "_cancel_scheduler_running_tasks", lambda: cancel_tasks(task))
    monkeypatch.setattr(adapter, "_process_message_stream_impl", submitted_stream)
    wait = asyncio.wait

    async def shorten_stop_wait(tasks, *, timeout=None):
        return await wait(tasks, timeout=0.01 if task in tasks else timeout)

    monkeypatch.setattr(asyncio, "wait", shorten_stop_wait)
    running = asyncio.create_task(collect(host, "disconnect", query="wait"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
        assert "Application execution stop unconfirmed" in caplog.text
        assert not task.done()
        assert not _APPLICATION_RUNS
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_scheduler_stop_wait_excludes_current_and_finished_tasks(host):
    adapter, _ = host
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    assert adapter._cancel_scheduler_running_tasks(finished) == set()
    assert adapter._cancel_scheduler_running_tasks(asyncio.current_task()) == set()


@pytest.mark.asyncio
async def test_rejected_client_declaration_does_not_append_user_or_answer_history(facade, host):
    chunks = [chunk async for chunk in facade.process_message_stream(request(
        "bad-history", query="must-not-be-recorded", mode="agent", client_tools=[TOOL, TOOL],
    ))]
    assert chunks[-1].payload["state"] == "rejected", [chunk.payload for chunk in chunks]
    assert "must-not-be-recorded" not in str(load_history_page("client-session", 1))


@pytest.mark.asyncio
async def test_unary_facade_persists_actual_output(facade):
    response = await facade.execute_message(request("unary-history", query="hello", mode="agent"))
    assert response.ok and response.payload["state"] == "completed", response
    page = load_history_page("client-session", 1)
    assert any(row.get("role") == "assistant" and row.get("content") == "done"
               and row.get("request_id") == "unary-history" for row in page["messages"]), page


@pytest.mark.asyncio
@pytest.mark.parametrize("remembered", [False, True])
async def test_cua_retains_authorization_and_rejects_new_approval(host, remembered):
    from jiuwenswarm.agents.harness.common.rails.cua_delegation_approval_rail import CuaDelegationApprovalRail
    from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, RejectResult
    from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext

    token = CURRENT_RUN_CONTEXT.set(RunContext(supports_permission_approval=False))
    try:
        decision = await CuaDelegationApprovalRail().resolve_interrupt(
            SimpleNamespace(), ToolCall(id="desktop", name="task_tool", type="function",
                                        arguments='{"subagent_type":"cua_agent","task_description":"open app"}'),
            None, {"task_tool:cua_agent": True} if remembered else {},
        )
    finally:
        CURRENT_RUN_CONTEXT.reset(token)
    assert isinstance(decision, ApproveResult if remembered else RejectResult)


@pytest.mark.asyncio
async def test_smart_final_human_confirmation_is_denied_on_real_dispatch(host, tmp_path, monkeypatch):
    from openjiuwen.core.foundation.tool import Tool, ToolCard
    from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import PolicyEvaluation
    from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import build_manual_approval_required_response
    from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _rail

    output = tmp_path / "smart-action"

    class SmartTool(Tool):
        async def invoke(self, inputs, **kwargs):
            output.write_text("must not execute")
            return "executed"

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs)

    adapter, model = host
    tool = SmartTool(ToolCard(name="smart_action", input_params={"type": "object"}))
    adapter._instance.ability_manager.add_ability(tool.card, tool)
    rail, _, _, _ = _rail(tmp_path, PolicyEvaluation(level="allow", reason="allowed"))
    monkeypatch.setattr(rail, "_normalize_runtime_permission_result", lambda *_args, **_kwargs:
                        build_manual_approval_required_response("explicit final confirmation"))
    await adapter._instance.register_rail(rail)
    model.steps = [[ToolCall(id="smart-call", name="smart_action", type="function", arguments="{}")]]
    events = await collect(host, "smart-final", query="do action")
    assert events[-1]["state"] == "completed", events
    assert not output.exists()
    assert not await adapter.pending_interactions("client-session")
    assert any("approval interface" in str(message.content) for message in model.seen[-1][0])


@pytest.mark.asyncio
async def test_runtime_wrong_execution_fence_preserves_waiting(facade, host):
    from unittest.mock import AsyncMock
    from jiuwenswarm.runtime import AgentRuntime
    from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController

    host[1].steps = [[ToolCall(id="fenced-call", type="function", name="set_counter", arguments='{"value":3}')]]
    events = await collect(host, "fenced-run", query="set", client_tools=[TOOL])
    assert events[-1]["state"] == "waiting_client_tools"
    pending = await host[0].pending_interactions("client-session")
    manager = FakeAgentManager()
    manager.agent = facade
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=FakePlanController())
    result = await runtime.cancel_request(request("wrong-fence", target_request_id="fenced-run", expected_execution_id="stale-execution", intent="cancel"))
    assert result.payload["success"] is False
    assert await host[0].pending_interactions("client-session") == pending


@pytest.mark.asyncio
async def test_rejected_resume_conflict_keeps_wait_and_omits_tool_history(facade, host):
    from openjiuwen.core.foundation.tool import ClientTool, ToolCard

    adapter, model = host
    model.steps = [[ToolCall(id="resume-conflict", type="function", name="set_counter", arguments='{"value":3}')]]
    events = await collect(host, "conflict-wait", query="set", client_tools=[TOOL])
    assert events[-1]["state"] == "waiting_client_tools"
    pending = await adapter.pending_interactions("client-session")
    backend = ClientTool(ToolCard(name="other_owner", input_params={"type": "object"}))
    adapter._instance.ability_manager.add_ability(backend.card, backend)
    chunks = [chunk async for chunk in facade.deliver_control_input(request(
        "bad-conflict-resume", source="client_tool_interrupt", mode="agent", client_tools=[dict(TOOL, name="other_owner")],
        answers=[{"request_id": "resume-conflict", "interrupt_id": pending[0]["interrupt_id"],
                  "result": {"content": [{"type": "text", "text": "not-accepted-result"}]}}],
    ))]
    assert chunks[-1].payload["state"] == "rejected", [chunk.payload for chunk in chunks]
    assert await adapter.pending_interactions("client-session") == pending
    assert "not-accepted-result" not in str(load_history_page("client-session", 1))


@pytest.mark.asyncio
async def test_smart_router_can_reopen_application_tool_owner(host, monkeypatch):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    router = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(router, "_coordinates_smart_permission_lifecycle", lambda *_args: True)
    monkeypatch.setattr(router, "_get_cached_session_adapter", lambda *_args: None)
    monkeypatch.setattr(router, "_get_or_create_session_adapter", AsyncMock(return_value=host[0]))
    selected = await router._get_session_adapter_for_request(request(
        "reopen-route", source="client_tool_interrupt", answers=[],
    ), reserve_activity=False)
    assert selected is host[0]
    router._get_or_create_session_adapter.assert_awaited_once()
