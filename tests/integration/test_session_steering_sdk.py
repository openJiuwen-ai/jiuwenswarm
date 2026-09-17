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
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


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
            await gateway.stop_forwarding()
            await client.disconnect()
    finally:
        tool.release.set()
        await gateway.stop_forwarding()
        await client.disconnect()
        await runtime.close()
        await sdk.stop()
        await Runner.stop()
