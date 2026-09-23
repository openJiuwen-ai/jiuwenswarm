# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Jiuwen wire controls over the real Core loop, with fake model/tool I/O.

Configuration, persistent session setup and team construction are isolated;
normal single-agent streaming, steering dispatch, binding and Core are real.
"""

from __future__ import annotations

import asyncio
from importlib.util import find_spec
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import (
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


# The locked official SDK predates steering. Check the independent schema
# marker so a broken capability implementation in a newer SDK still fails.
_CORE_HAS_STEERING = find_spec("openjiuwen.core.single_agent.schema.steering") is not None


class _BlockingTool(Tool):
    def __init__(self):
        super().__init__(ToolCard(name="blocked_tool", description="Wait for test"))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def invoke(self, inputs, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return "existing tool completed"

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class _Model:
    def __init__(self):
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid"
        )
        self.model_config = ModelRequestConfig(model_name="fake")
        self.calls = []

    async def stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            yield AssistantMessageChunk(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool-1",
                        type="function",
                        name="blocked_tool",
                        arguments="{}",
                    )
                ],
            )
        else:
            yield AssistantMessageChunk(content="Corrected original task result")

    async def invoke(self, messages, **kwargs):
        raise AssertionError("Only streaming fake model expected")


class _Sink:
    def __init__(self):
        self.chunks = []
        self.wires = []

    async def send_chunk(self, chunk, **kwargs):
        self.chunks.append(chunk)
        return True

    async def send_wire(self, wire):
        self.wires.append(wire)


def _isolate_configuration(monkeypatch, adapter, model):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.agents.harness import agent_observability
    from jiuwenswarm.server.runtime.debug_trace import config as debug_config

    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda *_: True)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda *_: model)
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", lambda *_: None)
    monkeypatch.setattr(adapter, "_handle_slash_command", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_sync_prompt_attachments_for_request", AsyncMock())
    monkeypatch.setattr(
        adapter, "_try_skill_turbo_resume", AsyncMock(return_value=None)
    )
    # _arm_skill_turbo_interrupt_recovery_hint 已随三层产物恢复兜底物理移除，
    # 无需再 patch 压制其副作用。
    monkeypatch.setattr(
        adapter, "_inject_extension_config_into_inputs", lambda *_: None
    )
    monkeypatch.setattr(adapter, "_deepresearch_artifact_output_dir", lambda *_: None)
    monkeypatch.setattr(
        adapter, "_update_permission_rail", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        adapter,
        "_is_stream_rewrite_fast_path_eligible",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        agent_observability, "sync_agent_observability", lambda **kwargs: None
    )
    monkeypatch.setattr(
        agent_observability, "open_agent_run_span", lambda **kwargs: None
    )
    monkeypatch.setattr(
        debug_config,
        "resolve_debug_trace_settings",
        lambda **kwargs: SimpleNamespace(
            enabled=False, dump_enabled=False, otel_enabled=False
        ),
    )
    monkeypatch.setattr(
        interface_deep, "set_perf_summary_context", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        interface_deep, "finalize_perf_summary_request", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        interface_deep, "clear_perf_summary_context", lambda *_args, **_kwargs: None
    )


@pytest.mark.asyncio
async def test_chat_pipeline_steers_real_core_during_tool_without_replacing_original_stream(
    monkeypatch,
):
    from jiuwenswarm.server import pipeline, agent_ws_server
    from jiuwenswarm.server.handlers import _default

    await Runner.start()
    tool = _BlockingTool()
    model = _Model()
    core = create_deep_agent(
        model=model,
        tools=[tool],
        system_prompt="Test",
        enable_task_loop=True,
        max_iterations=6,
    )
    sid = "jiuwen-steer-" + uuid.uuid4().hex
    main_task = None
    try:
        await core.start(session=Session(session_id=sid))
        adapter = JiuWenSwarmDeepAdapter()
        adapter.mark_as_session_scoped(sid)
        adapter._instance = core
        _isolate_configuration(monkeypatch, adapter, model)
        facade = JiuWenSwarm.__new__(JiuWenSwarm)
        facade._adapter = adapter
        manager = SimpleNamespace(iter_jiuwenswarm_instances=lambda: [facade])

        async def stream(request):
            async for chunk in adapter.process_message_stream_impl(
                request, {"query": request.params["query"]}
            ):
                yield chunk

        # Keep the real tenant cache lookup: a mocked lookup previously hid
        # a same-event-loop blocking wait that always returned no manager.
        pool = TenantAgentPool()
        await pool._agent_wrappers.put(
            pool._build_cache_key("agent-1", "service-1", "workspace-1"), manager
        )
        monkeypatch.setattr(pool, "process_message_stream", stream)
        monkeypatch.setattr(pool, "_ensure_agent_manager", AsyncMock(
            side_effect=AssertionError("Steering must not create a manager")
        ))
        services = SimpleNamespace(
            agent_manager=manager, tenant_pool=lambda: pool, session_stream_tasks={}
        )
        monkeypatch.setattr(pipeline, "_trigger_before_chat_request_hook", AsyncMock())
        monkeypatch.setattr(pipeline, "_ensure_auto_team_binding_for_chat", AsyncMock())
        monkeypatch.setattr(
            agent_ws_server, "ensure_interface_deep_and_checkpointer", AsyncMock()
        )
        monkeypatch.setattr(
            _default,
            "_prepare_tenant_code_mode_chat_turn",
            AsyncMock(return_value=None),
        )

        original = AgentRequest(
            request_id="original-wire",
            channel_id="officeclaw",
            session_id=sid,
            agent_id="agent-1",
            service_id="service-1",
            workspace_key="workspace-1",
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={
                "session_id": sid,
                "invocation_id": "inv-1",
                "mode": "agent.plan",
                "query": "original request",
            },
        )
        original_sink = _Sink()
        original_context = RequestContext(
            request=original,
            sink=original_sink,
            connection_id="test",
            services=services,
        )
        main_task = asyncio.create_task(
            pipeline.dispatch_parsed_request(original_context, original)
        )
        await asyncio.wait_for(tool.entered.wait(), 10)
        active_task = core.active_round.task_id
        owner = core._interaction_output.current_lease()

        async def control(method, *, content=None, input_id="input-1"):
            params = {
                "session_id": sid,
                "invocation_id": "inv-1",
                "active_request_id": "original-wire",
            }
            if input_id:
                params["input_id"] = input_id
            if content is not None:
                params.update(content=content, client_message_id="client-" + input_id)
            request = AgentRequest(
                request_id="control-" + method.value,
                channel_id=original.channel_id,
                session_id=sid,
                agent_id=original.agent_id,
                service_id=original.service_id,
                workspace_key=original.workspace_key,
                req_method=method,
                params=params,
            )
            sink = _Sink()
            await pipeline.dispatch_parsed_request(
                RequestContext(
                    request=request, sink=sink, connection_id="test", services=services
                ),
                request,
            )
            assert len(sink.wires) == 1
            assert sink.wires[0]["request_id"] == request.request_id
            assert sink.wires[0]["is_final"] is True
            return parse_agent_server_wire_unary(sink.wires[0])

        capability = await control(ReqMethod.CHAT_STEER_STATUS, input_id="")
        if not _CORE_HAS_STEERING:
            assert capability.payload["supported"] is False
            assert capability.payload["reason"] == "STEER_UNSUPPORTED"
            rejected = await control(ReqMethod.CHAT_STEER, content="legacy correction")
            assert not rejected.ok
            assert rejected.payload["reason"] == "STEER_UNSUPPORTED"
            assert not main_task.done()
            assert core.active_round.task_id == active_task
            assert core._interaction_output.current_lease() is owner
            assert len(model.calls) == tool.calls == 1
            tool.release.set()
            await asyncio.wait_for(main_task, 15)
            assert len(model.calls) == 2 and tool.calls == 1
            assert original_sink.chunks and not original_sink.wires
            assert all(chunk.request_id == original.request_id for chunk in original_sink.chunks)
            assert any("Corrected original task result" in str(chunk.payload)
                       for chunk in original_sink.chunks)
            assert not any(isinstance(chunk.payload, dict)
                           and chunk.payload.get("event_type") == "chat.steering_consumed"
                           for chunk in original_sink.chunks)
            assert not any("legacy correction" in str(message.content)
                           for message in model.calls[1])
            return
        assert capability.payload["supported"] is True
        first = await control(ReqMethod.CHAT_STEER, content="Only analyse East China")
        duplicate = await control(
            ReqMethod.CHAT_STEER, content="Only analyse East China"
        )
        second = await control(
            ReqMethod.CHAT_STEER, content="Reply in Chinese", input_id="input-2"
        )
        assert first.ok and duplicate.ok and second.ok
        assert first.payload["status"] == second.payload["status"] == "accepted"
        assert not main_task.done()
        assert core.active_round.task_id == active_task
        assert core._interaction_output.current_lease() is owner
        assert len(model.calls) == tool.calls == 1
        tool.release.set()
        await asyncio.wait_for(main_task, 15)
        assert len(model.calls) == 2
        assert tool.calls == 1
        assert all(
            chunk.request_id == "original-wire" for chunk in original_sink.chunks
        )
        assert not original_sink.wires
        payloads = [chunk.payload for chunk in original_sink.chunks if isinstance(chunk.payload, dict)]
        boundaries = [i for i, payload in enumerate(payloads)
                      if payload.get("event_type") == "chat.steering_consumed"]
        assert len(boundaries) == 1
        assert payloads[boundaries[0]]["input_ids"] == ["input-1", "input-2"]
        corrected_index = next(i for i, payload in enumerate(payloads)
                               if payload.get("event_type") == "chat.delta"
                               and "Corrected original task result" in payload.get("content", ""))
        assert boundaries[0] < corrected_index
        assert any(
            "Corrected original task result" in str(chunk.payload)
            for chunk in original_sink.chunks
        )
        messages = model.calls[1]
        steering = next(
            msg
            for msg in messages
            if "Only analyse East China" in str(getattr(msg, "content", ""))
        )
        assert (
            steering.content == "[STEERING] Only analyse East China\nReply in Chinese"
        )
        assert steering.metadata["steering_input_ids"] == ["input-1", "input-2"]
        tool_index = next(
            i for i, msg in enumerate(messages) if getattr(msg, "role", None) == "tool"
        )
        assert messages[tool_index].tool_call_id == "tool-1"
        assert tool_index < messages.index(steering)
        receipt = await control(ReqMethod.CHAT_STEER_STATUS)
        assert receipt.payload["status"] == "consumed"
        late = await control(
            ReqMethod.CHAT_STEER, content="late input", input_id="late"
        )
        assert late.ok is False
        assert late.payload["reason"] == "RUN_NOT_ACTIVE"
    finally:
        tool.release.set()
        if main_task is not None and not main_task.done():
            main_task.cancel()
            await asyncio.gather(main_task, return_exceptions=True)
        await core.stop()
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "team.plan", "code.team"])
async def test_team_adapter_controls_real_native_leader_with_literal_fifo_inputs(
    monkeypatch, mode,
):
    """Use the production TeamAgent -> TeamHarness -> NativeHarness control chain."""
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.harness import HarnessState, NativeHarness, TeamHarness
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.factory import DeepAgentParts
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.server import pipeline

    await Runner.start()
    sid = "jiuwen-native-steer-" + uuid.uuid4().hex
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    card = AgentCard(name="steering-test-leader", description="Native test leader")
    # Same parts fixture as Core's native harness tests: real task/ReAct kernel,
    # no external configuration or model clients.
    spec = SimpleNamespace(
        resolve_parts=lambda context: DeepAgentParts(
            config=DeepAgentConfig(card=card, enable_task_loop=True),
            rails=[],
            tool_cards=[],
            tool_instances=[],
        )
    )
    core = NativeHarness(spec)
    harness = TeamHarness(spec, None, core, role=TeamRole.LEADER, member_name="leader")
    main_task = None
    calls = []
    round_handles = []
    model = _Model()

    async def model_stream(messages, **kwargs):
        calls.append(list(messages))
        round_handles.append(core.active_round.task_id)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        yield AssistantMessageChunk(
            content="old answer" if len(calls) == 1 else "corrected native answer"
        )

    model.stream = model_stream

    async def on_state(*, new):
        if new is HarnessState.IDLE:
            finished.set()

    try:
        await harness.start()
        core.react_agent.set_llm(model)
        await core.subscribe(on_state=on_state)
        leader = TeamAgent(card)
        leader._configurator.harness = harness
        current_manager = team_manager.TeamManager.__new__(team_manager.TeamManager)
        current_manager._active_team_names = {sid: "active-team"}
        existing_pool = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(agent=leader))
        )
        # Keep this patch scoped so Runner.stop sees its own runtime manager.
        with monkeypatch.context() as wiring:
            wiring.setattr(
                GLOBAL_RUNNER,
                "_team_runtime_manager",
                SimpleNamespace(pool=existing_pool),
                raising=False,
            )
            wiring.setattr(team_manager, "peek_team_manager", lambda: current_manager)
            adapter = JiuWenSwarmDeepAdapter()
            adapter.mark_as_session_scoped(sid)

            async def native_stream(request, inputs):
                # Isolate normal team construction/rendering only. The original
                # output consumer is attached once and kept throughout steering.
                await harness.send(inputs["query"])
                async for chunk in harness.outputs():
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={"native": chunk.payload},
                        is_complete=False,
                    )
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload=None,
                    is_complete=True,
                )

            wiring.setattr(adapter, "_process_message_stream_impl", native_stream)
            facade = JiuWenSwarm.__new__(JiuWenSwarm)
            facade._adapter = adapter
            owner_manager = SimpleNamespace(iter_jiuwenswarm_instances=lambda: [facade])
            tenant_pool = TenantAgentPool()
            await tenant_pool._agent_wrappers.put(
                tenant_pool._build_cache_key("agent-1", "service-1", "workspace-1"),
                owner_manager,
            )
            wiring.setattr(tenant_pool, "_ensure_agent_manager", AsyncMock(
                side_effect=AssertionError("Steering must not create a manager")
            ))
            services = SimpleNamespace(
                agent_manager=owner_manager, tenant_pool=lambda: tenant_pool
            )
            original = AgentRequest(
                request_id="native-original-wire",
                channel_id="officeclaw",
                session_id=sid,
                agent_id="agent-1",
                service_id="service-1",
                workspace_key="workspace-1",
                req_method=ReqMethod.CHAT_SEND,
                is_stream=True,
                params={
                    "session_id": sid,
                    "invocation_id": "native-invocation",
                    "mode": mode,
                },
            )
            outputs = []

            async def consume():
                async for chunk in adapter.process_message_stream_impl(
                    original, {"query": "original task"}
                ):
                    outputs.append(chunk)

            main_task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 10)
            native_handle = core.active_round.task_id
            assert native_handle and native_handle != original.request_id
            output_queue = core._st.output_queue
            # After initial send, strict controls may not start, abort or resume a round.
            send_spy = AsyncMock(wraps=core.send)
            abort_spy = AsyncMock(wraps=core.abort)
            resume_spy = AsyncMock(wraps=core.resume)
            wiring.setattr(core, "send", send_spy)
            wiring.setattr(core, "abort", abort_spy)
            wiring.setattr(core, "resume", resume_spy)

            async def control(method, *, input_id="first", content=None):
                params = {
                    "session_id": sid,
                    "invocation_id": "native-invocation",
                    "active_request_id": original.request_id,
                }
                if input_id:
                    params["input_id"] = input_id
                if content is not None:
                    params.update(
                        content=content, client_message_id="client-" + input_id
                    )
                request = AgentRequest(
                    request_id="control-" + method.value,
                    channel_id=original.channel_id,
                    session_id=sid,
                    agent_id=original.agent_id,
                    service_id=original.service_id,
                    workspace_key=original.workspace_key,
                    req_method=method,
                    params=params,
                )
                sink = _Sink()
                await pipeline.dispatch_parsed_request(
                    RequestContext(
                        request=request,
                        sink=sink,
                        connection_id="test",
                        services=services,
                    ),
                    request,
                )
                assert len(sink.wires) == 1
                response = parse_agent_server_wire_unary(sink.wires[0])
                assert response.payload["active_request_id"] == original.request_id
                return response

            capability = await control(ReqMethod.CHAT_STEER_STATUS, input_id="")
            if not _CORE_HAS_STEERING:
                assert capability.payload["supported"] is False
                assert capability.payload["reason"] == "STEER_UNSUPPORTED"
                rejected = await control(ReqMethod.CHAT_STEER, content="legacy correction")
                assert not rejected.ok
                assert rejected.payload["reason"] == "STEER_UNSUPPORTED"
                assert core.active_round.task_id == native_handle
                assert core._st.output_queue is output_queue
                assert not main_task.done() and len(calls) == 1
                existing_pool.get.assert_awaited_with("active-team")
                release.set()
                await asyncio.wait_for(finished.wait(), 10)
                assert len(calls) == 1 and round_handles == [native_handle]
                send_spy.assert_not_awaited()
                abort_spy.assert_not_awaited()
                resume_spy.assert_not_awaited()
                await harness.stop()
                await asyncio.wait_for(main_task, 10)
                assert any("old answer" in str(chunk.payload) for chunk in outputs)
                assert all(chunk.request_id == original.request_id for chunk in outputs)
                assert not any("legacy correction" in str(message.content)
                               for message in calls[0])
                return
            assert capability.payload["supported"] is True
            assert capability.payload["target"] == "team_leader"
            first_text, second_text = (
                "# @expert $budget literal correction",
                "Reply in Chinese",
            )
            first = await control(ReqMethod.CHAT_STEER, content=first_text)
            second = await control(
                ReqMethod.CHAT_STEER, input_id="second", content=second_text
            )
            retry = await control(ReqMethod.CHAT_STEER, content=first_text)
            assert (
                first.payload["status"]
                == second.payload["status"]
                == retry.payload["status"]
                == "accepted"
            )
            assert leader.get_active_steering_request_id() == native_handle
            assert core._st.output_queue is output_queue
            assert not main_task.done()
            assert len(calls) == 1
            existing_pool.get.assert_awaited_with("active-team")
            release.set()
            await asyncio.wait_for(finished.wait(), 10)
            assert len(calls) == 2
            assert round_handles == [native_handle, native_handle]
            steering = next(
                message
                for message in calls[1]
                if first_text in str(getattr(message, "content", ""))
            )
            assert steering.content == "[STEERING] " + first_text + "\n" + second_text
            assert steering.metadata["steering_input_ids"] == ["first", "second"]
            for input_id in ("first", "second"):
                receipt = await control(ReqMethod.CHAT_STEER_STATUS, input_id=input_id)
                assert receipt.payload["status"] == "consumed"
            # A finished native round rejects even while the original output
            # transport still exists; it must not start/resume a team round.
            late = await control(
                ReqMethod.CHAT_STEER, input_id="late", content="too late"
            )
            assert not late.ok and late.payload["reason"] == "RUN_NOT_ACTIVE"
            send_spy.assert_not_awaited()
            abort_spy.assert_not_awaited()
            resume_spy.assert_not_awaited()
            await harness.stop()
            # Query the actual stopped wrapper, independently of the adapter's receipt cache.
            assert (await leader.get_steering_status(
                active_request_id=native_handle, input_id="first",
            ))["status"] == "consumed"
            assert await leader.get_steering_capability(active_request_id=native_handle) == {
                "supported": False, "reason": "not_active",
            }
            await asyncio.wait_for(main_task, 10)
            assert any(
                "corrected native answer" in str(chunk.payload) for chunk in outputs
            )
            assert all(chunk.request_id == original.request_id for chunk in outputs)
            assert (await control(ReqMethod.CHAT_STEER_STATUS)).payload[
                "status"
            ] == "consumed"
            assert (
                await control(ReqMethod.CHAT_STEER, input_id="closed", content="closed")
            ).payload["reason"] == "RUN_NOT_ACTIVE"
    finally:
        release.set()
        await harness.stop()
        if main_task is not None and not main_task.done():
            main_task.cancel()
            await asyncio.gather(main_task, return_exceptions=True)
        await Runner.stop()
