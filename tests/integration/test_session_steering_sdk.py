# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real locked SDK, deterministic model and tool: no remote credentials needed.

The real interaction runner, output lease, steering queue, ReAct loop and
model-context admission run unchanged. Only the external model is scripted.
"""

import asyncio
import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall, UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session import InteractiveInput
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.session.model import SessionExecutionState


class ScriptedModel:
    def __init__(self, *, tools_count=1):
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="local-test", api_base="http://127.0.0.1:1",
        )
        self.model_config = ModelRequestConfig(model_name="steering-test")
        self.messages = []
        self.tools_count = tools_count

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        if len(self.messages) == 1:
            calls = [ToolCall(id="gate-call", type="function", name="steering_gate", arguments="{}")]
            if self.tools_count == 2:
                calls.insert(0, ToolCall(id="fast-call", type="function", name="fast_tool", arguments="{}"))
            return AssistantMessage(
                content="", tool_calls=calls,
                usage_metadata=UsageMetadata(model_name="steering-test", finish_reason="tool_calls"),
            )
        return AssistantMessage(
            content="completed", usage_metadata=UsageMetadata(model_name="steering-test", finish_reason="stop"),
        )

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=response.content, tool_calls=response.tool_calls, usage_metadata=response.usage_metadata,
        )


class GatedTool(Tool):
    def __init__(self):
        super().__init__(ToolCard(name="steering_gate", description="Wait for the test to release execution"))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def invoke(self, inputs, **kwargs):
        self.entered.set()
        try:
            await self.release.wait()
            return "gate released"
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class ImmediateTool(Tool):
    def __init__(self):
        super().__init__(ToolCard(name="fast_tool", description="Completes before the gated tool"))

    async def invoke(self, inputs, **kwargs):
        return "fast tool completed"

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class ClarificationModel(ScriptedModel):
    """Hold the initial and resumed model calls at reproducible input boundaries."""

    def __init__(self):
        super().__init__()
        self.entered = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        index = len(self.messages) - 1
        if index < 2:
            self.entered[index].set()
            await self.release[index].wait()
        if index == 0:
            return AssistantMessage(
                content="Please choose the code type.",
                tool_calls=[ToolCall(
                    id="clarify-code", type="function", name="ask_user",
                    arguments=json.dumps({"questions": [{
                        "header": "Type", "question": "Which code type?",
                        "options": [
                            {"label": "Python", "description": "Python utility"},
                            {"label": "JavaScript", "description": "Web utility"},
                        ],
                    }]}),
                )],
                usage_metadata=UsageMetadata(model_name="steering-test", finish_reason="tool_calls"),
            )
        return AssistantMessage(
            content="completed", usage_metadata=UsageMetadata(model_name="steering-test", finish_reason="stop"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["before_question", "during_resume"])
@pytest.mark.parametrize("bound", [False, True])
async def test_steering_survives_ask_user_resume(tmp_path, arrival, bound):
    """Run the actual SDK pause/resume loop and inspect subsequent model inputs."""
    await Runner.start()
    model = ClarificationModel()
    agent = create_deep_agent(
        model=model, workspace=str(tmp_path), rails=[StructuredAskUserRail()],
        enable_task_loop=True, max_iterations=5,
        enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
    )
    session = create_agent_session(session_id=f"steering-resume-{uuid.uuid4().hex}", card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session.get_session_id()
    stream, reader = None, None

    async def collect(output):
        return [chunk async for chunk in output]

    async def supplement():
        facade = JiuWenSwarm()
        facade._adapter = adapter
        request = AgentRequest(
            request_id="supplement", channel_id="web", session_id=session.get_session_id(),
            req_method=ReqMethod.CHAT_SEND, is_stream=True,
            params={"query": "CHANGE_TO_100_LINES", "input_mode": "steer", "mode": "agent"},
        )
        if bound:
            request.params["expected_execution_id"] = "original-execution"
        execution = SimpleNamespace(
            session_id=session.get_session_id(), state=SessionExecutionState.RUNNING, cancellation_requested=False,
        )
        token = set_runtime_context(SimpleNamespace(get_session_execution=Mock(return_value=execution)), None)
        try:
            events = [event async for event in facade.deliver_session_input(request)]
        finally:
            reset_runtime_context(token)
        assert events[0].payload["event_type"] == "runtime.accepted"
        assert not reader.done()

    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        stream = await agent.attach_output()
        reader = asyncio.create_task(collect(stream))
        await agent.send_input(SendInputRequest(request_id="original", inputs={"query": "Write 200 lines"}))
        await asyncio.wait_for(model.entered[0].wait(), 15)
        if arrival == "before_question":
            await supplement()
        model.release[0].set()
        assert await asyncio.wait_for(reader, 15)
        assert len(model.messages) == 1
        assert "CHANGE_TO_100_LINES" not in str(model.messages[0])
        await stream.close()

        answer = InteractiveInput()
        answer.update("clarify-code", {"answers": {"Which code type?": "Python utility"}})
        stream = await agent.attach_output()
        reader = asyncio.create_task(collect(stream))
        await agent.send_input(SendInputRequest(request_id="answer", inputs={"query": answer}))
        await asyncio.wait_for(model.entered[1].wait(), 15)
        if arrival == "during_resume":
            await supplement()
        model.release[1].set()
        assert await asyncio.wait_for(reader, 15)
        assert len(model.messages) == (2 if arrival == "before_question" else 3)
        final_messages = model.messages[-1]
        assert "Write 200 lines" in str(final_messages)
        assert "Python utility" in str(final_messages)
        assert str(final_messages).count("CHANGE_TO_100_LINES") == 1
        answer_index = next(
            i for i, msg in enumerate(final_messages) if getattr(msg, "tool_call_id", None) == "clarify-code"
        )
        steer_index = next(i for i, msg in enumerate(final_messages) if "CHANGE_TO_100_LINES" in str(msg.content))
        assert answer_index < steer_index
        assert agent.event_handler.interaction_queues.steering.empty()
        assert agent.event_handler.interaction_queues.drain_follow_up() == []
    finally:
        for release in model.release:
            release.set()
        if stream is not None:
            await stream.close(abort_active_round=True)
        await agent.stop()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["sdk", "harness"])
@pytest.mark.parametrize("arrival", ["tool", "final_save"])
@pytest.mark.parametrize("tools_count", [1, 2])
async def test_locked_sdk_steering_consumption_and_final_save_admission(
    tmp_path, monkeypatch, ingress, arrival, tools_count,
):
    await Runner.start()
    model, tool = ScriptedModel(tools_count=tools_count), GatedTool()
    agent = create_deep_agent(
        model=model, tools=[tool, ImmediateTool()], workspace=str(tmp_path),
        system_prompt="Run the requested tool and respond.", enable_task_loop=True,
        max_iterations=4, enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=False,
    )
    session = create_agent_session(session_id=f"steering-{uuid.uuid4().hex}", card=agent.card)
    await session.pre_run(inputs={})
    reader = None
    stream = None
    try:
        adapter = JiuWenSwarmDeepAdapter()
        adapter._instance = agent
        adapter._is_session_scoped_adapter = True
        adapter._parent_session_id = session.get_session_id()
        if ingress == "harness":
            await adapter.install_session_input_guard()
        await agent.start(session=session)
        stream = await agent.attach_output()
        assert stream is not None

        async def read():
            return [chunk async for chunk in stream]

        reader = asyncio.create_task(read())
        await agent.send_input(SendInputRequest(request_id="original", inputs={"query": "run the gate"}))
        await asyncio.wait_for(tool.entered.wait(), 15)
        save_entered, save_release = asyncio.Event(), asyncio.Event()
        if arrival == "final_save":
            save_contexts = agent.react_agent.context_engine.save_contexts

            async def gated_save(*args, **kwargs):
                if len(model.messages) == 2 and not save_release.is_set():
                    save_entered.set()
                    await save_release.wait()
                return await save_contexts(*args, **kwargs)

            monkeypatch.setattr(agent.react_agent.context_engine, "save_contexts", gated_save)
            tool.release.set()
            await asyncio.wait_for(save_entered.wait(), 10)
        assert await agent.attach_output() is None
        if ingress == "sdk":
            await agent.send_input(SendInputRequest(
                request_id="new-unrelated-id", inputs={"query": "STEERING_ACCEPTANCE_731"},
                mode=InputDispatchMode.STEER,
            ))
        else:
            facade = JiuWenSwarm()
            facade._adapter = adapter
            req = AgentRequest(
                request_id="new-unrelated-id", channel_id="web", session_id=session.get_session_id(),
                req_method=ReqMethod.CHAT_SEND, is_stream=True,
                params={"query": "STEERING_ACCEPTANCE_731", "input_mode": "steer", "mode": "agent"},
            )
            if arrival == "final_save":
                with pytest.raises(RuntimeError, match="finishing"):
                    _ = [event async for event in facade.deliver_session_input(req)]
            else:
                events = [event async for event in facade.deliver_session_input(req)]
                assert events[0].payload["event_type"] == "runtime.accepted"
                assert events[0].request_id == "new-unrelated-id"
        assert not reader.done()
        assert len(model.messages) == (1 if arrival == "tool" else 2)
        assert not tool.cancelled
        tool.release.set()
        save_release.set()
        chunks = await asyncio.wait_for(reader, 15)
        assert chunks
        if ingress == "harness" and arrival == "tool":
            received_index = next(i for i, chunk in enumerate(chunks)
                                  if getattr(chunk, "type", None) == "session_input_received")
            applied_index = next(i for i, chunk in enumerate(chunks)
                                 if getattr(chunk, "type", None) == "session_output_phase"
                                 and chunk.payload["applied_input_ids"] == ["new-unrelated-id"])
            assert received_index < applied_index
            phases = [chunk.payload["output_phase_id"] for chunk in chunks
                      if getattr(chunk, "type", None) == "session_output_phase"]
            assert len(phases) == len(set(phases)) == 2
        assert len(model.messages) == 2
        assert "STEERING_ACCEPTANCE_731" not in str(model.messages[0])
        if arrival == "tool":
            assert "STEERING_ACCEPTANCE_731" in str(model.messages[1])
        else:
            # Characterize the locked SDK's final-save admission window:
            # accepted into its queue, but no later model call consumes it.
            assert "STEERING_ACCEPTANCE_731" not in str(model.messages[1])
        assert not tool.cancelled
    finally:
        tool.release.set()
        if "save_release" in locals():
            save_release.set()
        if stream is not None:
            await stream.close(abort_active_round=True)
        await agent.stop()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("original_stream", [False, True])
@pytest.mark.parametrize("input_stream", [False, True])
async def test_gateway_websocket_runtime_harness_sdk(tmp_path, monkeypatch, original_stream, input_stream):
    """Live loopback E2A with production client/server handlers and SDK.

    Fixture composition supplies a deterministic model, tool and prepared
    agent; it does not start the desktop application or external IM services.
    """
    from websockets.legacy.server import serve
    from jiuwenswarm.common.e2a.models import E2AEnvelope
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
    from jiuwenswarm.common.schema.message import Message
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    await Runner.start()
    model, tool = ScriptedModel(), GatedTool()
    sdk = create_deep_agent(
        model=model, tools=[tool], workspace=str(tmp_path), enable_task_loop=True,
        max_iterations=4, enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=False,
    )
    sid = f"sess_steering_{uuid.uuid4().hex}"
    session = create_agent_session(session_id=sid, card=sdk.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance, adapter._parent_session_id = sdk, sid
    adapter._is_session_scoped_adapter = True
    await adapter.install_session_input_guard()
    await sdk.start(session=session)
    facade = JiuWenSwarm()
    facade._adapter = adapter

    class PreparedAgent:
        deliver_session_input = facade.deliver_session_input

        async def process_message_stream(self, req):
            output = await sdk.attach_output()
            assert output is not None
            try:
                await sdk.send_input(SendInputRequest(request_id=req.request_id, inputs={"query": req.params["query"]}))
                async for _ in output:
                    pass
                yield AgentResponseChunk(
                    request_id=req.request_id, channel_id=req.channel_id,
                    payload={"event_type": "chat.final", "content": "original completed"}, is_complete=True,
                )
            finally:
                await output.close(abort_active_round=True)

        async def execute_message(self, req):
            chunks = [chunk async for chunk in self.process_message_stream(req)]
            return AgentResponse(request_id=req.request_id, channel_id=req.channel_id, payload=chunks[-1].payload)

    prepared = PreparedAgent()
    manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(), cleanup=AsyncMock(),
        begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock(),
        get_agent_for_session_nowait=Mock(return_value=prepared),
    )
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        plan_controller=SimpleNamespace(
            ensure_state=AsyncMock(return_value=SimpleNamespace(events=[])),
            check_post_process_exit=AsyncMock(return_value=[]), reset_session=Mock(),
        ),
    )
    monkeypatch.setattr(runtime, "_prepare_chat_turn", AsyncMock(return_value=("agent", None, prepared)))
    await runtime._register_session(session_id=sid, channel_id="web")
    server = object.__new__(AgentWebSocketServer)
    server._execution_runtime = lambda: runtime
    server._open_session_message_resume = AsyncMock(return_value=None)
    server._finalize_session_message_resume = AsyncMock()

    async def connection(ws):
        await ws.send(json.dumps({"type": "event", "event": "connection.ack"}))
        lock, tasks = asyncio.Lock(), set()

        async def dispatch(req):
            try:
                if req.is_stream:
                    await server._handle_stream_impl(ws, req, lock)
                else:
                    await server._handle_unary_impl(ws, req, lock)
            except Exception as exc:
                await server._send_runtime_event(ws, RuntimeEvent.error(
                    request_id=req.request_id, channel_id=req.channel_id, session_id=sid, error=exc,
                ), lock, streaming=req.is_stream, sequence=0)

        try:
            async for raw in ws:
                req = e2a_to_agent_request(E2AEnvelope.from_dict(json.loads(raw)))
                task = asyncio.create_task(dispatch(req))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    client = WebSocketAgentServerClient()
    monkeypatch.setattr(MessageHandler, "_instance", None)
    gateway = MessageHandler(client)
    gateway._sync_agentos_cron_jobs = AsyncMock()
    gateway._broadcast_task_global_running = AsyncMock()
    gateway._trigger_before_chat_request_hook = AsyncMock()
    observed = []
    original_publish = gateway.publish_robot_messages

    async def publish(msg):
        observed.append(msg)
        await original_publish(msg)

    gateway.publish_robot_messages = publish

    def message(rid, streaming, **params):
        return Message(
            id=rid, type="req", channel_id="web", session_id=sid, timestamp=0,
            req_method=ReqMethod.CHAT_SEND, is_stream=streaming, ok=True,
            params={"query": "run the gate", "mode": "agent", **params},
        )

    async def receive(rid, event_type):
        async with asyncio.timeout(15):
            while True:
                msg = await gateway.consume_robot_messages()
                if msg.id == rid and (msg.payload or {}).get("event_type") == event_type:
                    return msg

    try:
        async with serve(connection, "127.0.0.1", 0) as listener:
            await client.connect(f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}")
            assert client.server_ready
            await gateway.start_forwarding()
            await gateway.publish_user_messages(message("original", original_stream))
            await asyncio.wait_for(tool.entered.wait(), 15)
            await gateway.publish_user_messages(message(
                "unrelated-supplement", input_stream, input_mode="steer", query="STEERING_WIRE_934",
            ))
            ack = await receive("unrelated-supplement", "runtime.accepted")
            assert ack.ok and ack.session_id == sid
            assert not tool.cancelled and not tool.release.is_set()
            assert len(model.messages) == 1
            assert not any((msg.payload or {}).get("is_processing") is False for msg in observed)
            tool.release.set()
            await receive("original", "chat.final")
            assert "STEERING_WIRE_934" in str(model.messages[1])
            assert not tool.cancelled
            runtime._prepare_chat_turn.assert_awaited_once()
            # Idle chat.send streams acknowledge ordinary admission before real SDK output.
            # The unary wire path retains its single final response.
            await gateway.publish_user_messages(message(
                "idle-supplement", input_stream, input_mode="steer", query="IDLE_WIRE_935",
            ))
            if input_stream:
                idle_ack = await receive("idle-supplement", "runtime.accepted")
                assert idle_ack.payload["input_delivery"] == "chat"
            idle_final = await receive("idle-supplement", "chat.final")
            assert idle_final.payload["content"] == "original completed"
            assert "IDLE_WIRE_935" in str(model.messages[-1])
            assert runtime._prepare_chat_turn.await_count == 2
            await gateway.stop_forwarding()
            await client.disconnect()
    finally:
        tool.release.set()
        await gateway.stop_forwarding()
        await client.disconnect()
        await runtime.close()
        await sdk.stop()
        await Runner.stop()


@pytest.mark.asyncio
async def test_continuous_streaming_steers_have_exact_batch_identity_and_order(tmp_path):
    """Hold the real SDK inside model streaming; never use sleeps for admission."""
    class StreamingModel(ScriptedModel):
        def __init__(self):
            super().__init__()
            self.entered = [asyncio.Event() for _ in range(3)]
            self.release = [asyncio.Event() for _ in range(3)]

        async def stream(self, messages, **kwargs):
            index = len(self.messages)
            self.messages.append(copy.deepcopy(messages))
            yield AssistantMessageChunk(content=f"prefix-{index}")
            self.entered[index].set()
            await self.release[index].wait()
            yield AssistantMessageChunk(content=f"tail-{index}", usage_metadata=UsageMetadata(
                model_name="steering-test", finish_reason="stop",
            ))

    await Runner.start()
    model = StreamingModel()
    agent = create_deep_agent(model=model, workspace=str(tmp_path), enable_task_loop=True,
                              max_iterations=5, enable_model_anomaly_detection_rail=False,
                              enable_read_image_multimodal=False)
    session = create_agent_session(session_id=f"phase-stream-{uuid.uuid4().hex}", card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session.get_session_id()
    stream, reader = None, None

    async def collect(output):
        return [chunk async for chunk in output]

    async def send_input(request_id):
        request = AgentRequest(request_id=request_id, channel_id="web", session_id=session.get_session_id(),
                               params={"content": "same instruction", "input_mode": "steer", "mode": "agent"})
        assert await adapter.deliver_active_session_input(request, {"query": "same instruction"})
        assert not reader.done()

    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        stream = await agent.attach_output()
        reader = asyncio.create_task(collect(stream))
        await agent.send_input(SendInputRequest(request_id="original", inputs={"query": "begin"}))
        await asyncio.wait_for(model.entered[0].wait(), 15)
        await send_input("input-1")
        await send_input("input-2")
        model.release[0].set()
        await asyncio.wait_for(model.entered[1].wait(), 15)
        await send_input("input-3")
        model.release[1].set()
        await asyncio.wait_for(model.entered[2].wait(), 15)
        model.release[2].set()
        chunks = await asyncio.wait_for(reader, 15)
        phases = [chunk for chunk in chunks if getattr(chunk, "type", None) == "session_output_phase"]
        assert [p.payload["applied_input_ids"] for p in phases] == [[], ["input-1", "input-2"], ["input-3"]]
        assert len({p.payload["output_phase_id"] for p in phases}) == 3
        receipts = [c for c in chunks if getattr(c, "type", None) == "session_input_received"]
        assert [r.payload["input_request_id"] for r in receipts] == ["input-1", "input-2", "input-3"]
        assert chunks.index(receipts[0]) < chunks.index(receipts[1]) < chunks.index(phases[1])
        assert chunks.index(phases[1]) < chunks.index(receipts[2]) < chunks.index(phases[2])
        assert "same instruction" not in str(model.messages[0])
        assert str(model.messages[1]).count("same instruction") == 2
        assert str(model.messages[2]).count("same instruction") == 3
    finally:
        for release in model.release:
            release.set()
        if stream is not None:
            await stream.close(abort_active_round=True)
        await agent.stop()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["automatic", "steer", "pause", "resume", "clear"])
async def test_goal_steering_uses_real_adapter_stream_and_closes_after_clear(tmp_path, monkeypatch, action):
    """Keep SDK scheduling and the host event formatter real through Goal teardown."""
    from jiuwenswarm.agents.harness import agent_observability

    class GoalModel(ScriptedModel):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def invoke(self, messages, **kwargs):
            # The Goal assessor asks for another attempt; max_attempts bounds
            # autonomous execution without relying on timing or remote models.
            return AssistantMessage(content=json.dumps({
                "status": "continue", "evidence": "another attempt required",
                "next_instruction": "continue",
            }))

        async def stream(self, messages, **kwargs):
            self.messages.append(copy.deepcopy(messages))
            index = len(self.messages)
            yield AssistantMessageChunk(content=f"prefix-{index}")
            if index == 1:
                self.entered.set()
                await self.release.wait()
            yield AssistantMessageChunk(content=f"tail-{index}", usage_metadata=UsageMetadata(
                model_name="steering-test", finish_reason="stop",
            ))

    await Runner.start()
    model = GoalModel()
    agent = create_deep_agent(
        model=model, workspace=str(tmp_path), enable_task_loop=True, max_iterations=5,
        enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
    )
    session = create_agent_session(session_id=f"goal-steering-{uuid.uuid4().hex}", card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session.get_session_id()
    # Isolate host configuration/observability from the running desktop. The
    # output iterator, Goal manager, steering admission and formatter stay real.
    monkeypatch.setattr(adapter, "_model_config_error", lambda _request: None)
    monkeypatch.setattr(adapter, "_ensure_chat_extensions", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: model)
    monkeypatch.setattr(adapter, "_prepare_root_input_dispatch", AsyncMock(side_effect=lambda _req, inputs: inputs))
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)
    reader = None
    payloads = []
    prefix_received = asyncio.Event()
    input_received = asyncio.Event()

    async def collect():
        request = AgentRequest(
            request_id="goal-output", channel_id="web", session_id=session.get_session_id(),
            params={"mode": "agent", "attach_goal": True},
        )
        async for chunk in adapter._process_message_stream_impl(request, {"query": ""}):
            payload = chunk.payload
            if not isinstance(payload, dict):
                continue
            payloads.append(payload)
            if payload.get("event_type") == "chat.delta":
                prefix_received.set()
            if payload.get("event_type") == "chat.input_received":
                input_received.set()
        assert not any(p.get("event_type") == "chat.error" for p in payloads), payloads

    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        await agent.goal_manager.set("Verify Goal streaming", max_attempts=2)
        reader = asyncio.create_task(collect())
        await asyncio.wait_for(prefix_received.wait(), 10)
        assert model.entered.is_set() and not reader.done()
        if action != "automatic":
            request = AgentRequest(
                request_id="goal-supplement", channel_id="web", session_id=session.get_session_id(),
                params={"content": "NEW_GOAL_CONSTRAINT", "input_mode": "steer", "mode": "agent"},
            )
            assert await adapter.deliver_active_session_input(request, {"query": "NEW_GOAL_CONSTRAINT"})
            await asyncio.wait_for(input_received.wait(), 10)
        if action in ("pause", "resume", "clear"):
            await agent.goal_manager.pause()
        if action == "resume":
            await agent.goal_manager.resume()
        if action == "clear":
            await agent.goal_manager.clear()
        model.release.set()
        await asyncio.wait_for(reader, 15)

        phases = [p for p in payloads if p.get("event_type") == "chat.output_phase"]
        goal = await agent.goal_manager.get()
        expected_status = None if action == "clear" else "paused" if action == "pause" else "blocked"
        assert (goal.status.value if goal is not None else None) == expected_status
        if action == "clear":
            assert len(model.messages) == 1
            assert [p["applied_input_ids"] for p in phases] == [[]]
            assert not agent.event_handler.interaction_queues.steering.empty()
        elif action == "automatic":
            assert len(model.messages) == 2
            assert [p["applied_input_ids"] for p in phases] == [[], []]
        else:
            assert "NEW_GOAL_CONSTRAINT" not in str(model.messages[0])
            assert str(model.messages[1]).count("NEW_GOAL_CONSTRAINT") == 1
            assert [p["applied_input_ids"] for p in phases if p["applied_input_ids"]] == [["goal-supplement"]]
            assert agent.event_handler.interaction_queues.steering.empty()
        finals = [p for p in payloads if p.get("event_type") == "chat.final"]
        assert finals and not finals[-1].get("output_suppressed"), payloads
        if action == "clear":
            assert finals[-1]["content"] == ""
    finally:
        model.release.set()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await agent.stop()
        await Runner.stop()
