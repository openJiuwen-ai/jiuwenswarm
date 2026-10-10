# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in remote-model acceptance for the durable cross-Session steer path.

Both Agents use the configured provider. Only the target's waiting tool and
host composition are fixtures; model responses and tool calls are never scripted.
Requires RUN_LIVE_CROSS_SESSION_STEERING_TESTS=1 and an isolated data directory.
"""

import asyncio
import copy
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.interaction import SendInputRequest

from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    SessionMessagingRoute, SessionMessagingRouteRail, SessionMessagingToolkit,
    with_session_messaging_route,
)
from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter, build_model_from_entry,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageService, SessionMessageSource,
)
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore
from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata


pytestmark = [
    pytest.mark.system,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_CROSS_SESSION_STEERING_TESTS") != "1",
        reason="requires explicit opt-in and a configured remote model",
    ),
]


class RecordingLiveModel:
    """Record actual provider requests/results without changing either."""

    def __init__(self):
        entry = get_default_models(get_config())[0]
        self.delegate = build_model_from_entry(
            entry["model_client_config"], entry.get("model_config_obj") or {},
        )
        self.model_client_config = self.delegate.model_client_config
        self.model_config = self.delegate.model_config
        self.messages = []
        self.responses = []

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        response = await self.delegate.invoke(messages, **kwargs)
        self.responses.append(response)
        return response

    async def stream(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        async for chunk in self.delegate.stream(messages, **kwargs):
            self.responses.append(chunk)
            yield chunk


class LiveGate(Tool):
    def __init__(self):
        super().__init__(ToolCard(
            name="steering_gate", description="Wait for the collaborator's task specification update.",
            input_params={"type": "object", "properties": {}},
        ))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def invoke(self, inputs, **kwargs):
        self.entered.set()
        try:
            await self.release.wait()
            return "The wait has ended. Use the latest task specification in the conversation."
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


async def wait_record(store, message_id):
    while True:
        record = store.get(message_id)
        if record.status not in {"queued", "running"}:
            return record
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_delivery", ["human", "idle_steer"])
async def test_cross_session_steering_real_models(tmp_path, monkeypatch, initial_delivery):
    assert os.environ.get("JIUWENSWARM_DATA_DIR"), "Use an isolated data directory"
    started_at = time.time()
    suffix = uuid.uuid4().hex
    sid, source_sid = f"live_target_{suffix}", f"live_source_{suffix}"
    marker = f"LIVE_STEER_{uuid.uuid4().hex[:12]}"
    for session_id in (sid, source_sid):
        init_session_metadata(
            session_id=session_id, channel_id="web", user_id="live-test-owner",
            title=session_id, mode="agent", persist_session=True,
        )
    await Runner.start()
    target_model, source_model, gate = RecordingLiveModel(), RecordingLiveModel(), LiveGate()
    target = create_deep_agent(
        model=target_model, tools=[gate], workspace=str(tmp_path / "target"),
        enable_task_loop=True, max_iterations=5,
        enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
    )
    target_session = create_agent_session(session_id=sid, card=target.card)
    await target_session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance, adapter._parent_session_id = target, sid
    adapter._is_session_scoped_adapter = True
    await adapter.install_session_input_guard()
    await target.start(session=target_session)
    facade = JiuWenSwarm()
    facade._adapter = adapter
    chunks = []

    class PreparedTarget:
        deliver_session_input = facade.deliver_session_input

        async def process_message_stream(self, request):
            output = await target.attach_output()
            assert output is not None
            try:
                inputs, _, _ = facade._build_inputs(request)
                inputs = with_session_messaging_route(inputs, SessionMessagingRoute(
                    sid, request.request_id, "live-test-owner",
                ))
                await target.send_input(SendInputRequest(request_id=request.request_id, inputs=inputs))
                yield AgentResponseChunk(
                    request_id=request.request_id, channel_id="web",
                    payload={"event_type": "runtime.accepted"},
                )
                async for chunk in output:
                    chunks.append(chunk)
                # Completion comes from the actual provider answer, never a fixture string.
                answer = "".join(str(r.content or "") for r in target_model.responses if not r.tool_calls)
                yield AgentResponseChunk(
                    request_id=request.request_id, channel_id="web",
                    payload={"event_type": "chat.final", "content": answer}, is_complete=True,
                )
            finally:
                await output.close(abort_active_round=True)

    prepared = PreparedTarget()
    manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(), cleanup=AsyncMock(),
        begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock(),
        create_session=AsyncMock(return_value=sid), get_agent_for_session_nowait=Mock(return_value=prepared),
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
    server.send_push = AsyncMock()
    store = SessionMessageStore(tmp_path / "mailbox.sqlite3")
    mailbox = SessionMessageService(
        store=store, execute=server.execute_internal_session_message,
        admission=SimpleNamespace(
            begin_session_message=AsyncMock(side_effect=AssertionError("steer requested task admission")),
            end_session_message=AsyncMock(),
        ),
    )
    runtime.set_session_message_service(mailbox)
    server._session_message_service = mailbox
    source = None
    source_output = None
    reader = None
    report = {"scenario": initial_delivery, "model": target_model.model_config.model_name,
              "target_session": sid, "source_session": source_sid, "marker": marker}
    try:
        prompt = (
            "这是一个协作验收任务。先调用 steering_gate 一次等待协作者补充规格，不能提前结束。"
            "初始任务是生成颜色 BLUE 的结果，marker 为 INITIAL。等待结束后，"
            "采用协作者的最新规格，最终仅输出 JSON，字段 color、marker、source。"
            "source 填写最新规格消息的来源类型。不要调用其他工具。"
        )
        if initial_delivery == "idle_steer":
            first = await mailbox.send_message(
                SessionMessageSource(source_sid, "initial", "initial-call", f"initial-{suffix}", "live-test-owner"),
                target_session_id=sid, message=prompt, input_mode="steer",
            )
            reader = asyncio.create_task(wait_record(store, first["message_id"]))
        else:
            request = AgentRequest(
                request_id=f"original-{suffix}", channel_id="web", session_id=sid,
                user_id="live-test-owner", req_method=ReqMethod.CHAT_SEND, is_stream=True,
                params={"mode": "agent", "query": prompt},
            )
            async def collect_target():
                return [event async for event in runtime.stream(request, trigger_hook=False)]
            reader = asyncio.create_task(collect_target())
        await asyncio.wait_for(gate.entered.wait(), 120)
        assert not reader.done()
        report["target_calls_before_steer"] = len(target_model.messages)
        report["gate_entered_seconds"] = round(time.time() - started_at, 3)

        send_tool = next(t for t in SessionMessagingToolkit(service=mailbox).get_tools()
                         if t.card.name == "session_send_message")
        source = create_deep_agent(
            model=source_model, tools=[send_tool], rails=[SessionMessagingRouteRail()],
            workspace=str(tmp_path / "source"), enable_task_loop=True, max_iterations=4,
            enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
        )
        source_session = create_agent_session(session_id=source_sid, card=source.card)
        await source_session.pre_run(inputs={})
        # Match Host startup: start() alone does not register pending SDK rails.
        await source.ensure_initialized()
        await source.start(session=source_session)
        source_output = await source.attach_output()
        supplemental = f"将当前任务的 color 改为 RED，marker 改为 {marker}。这是来自其他 Session 的 Agent 规格补充。"
        source_inputs = with_session_messaging_route({"query": (
            "只调用 session_send_message 一次，参数如下：" + json.dumps({
                "target_session_id": sid, "message": supplemental, "input_mode": "steer",
            }, ensure_ascii=False) + "。工具返回后仅输出返回结果，不调用其他工具。"
        )}, SessionMessagingRoute(source_sid, f"source-{suffix}", "live-test-owner"))
        await source.send_input(SendInputRequest(request_id=f"source-{suffix}", inputs=source_inputs))
        async def consume_source():
            return [chunk async for chunk in source_output]
        await asyncio.wait_for(consume_source(), 120)
        records = store.list_messages(owner_scope_id="live-test-owner", session_id=source_sid)
        supplements = [r for r in records if marker in r.content]
        assert supplements, "source model did not persist the requested supplemental message"
        supplement = supplements[0]
        received = await asyncio.wait_for(wait_record(store, supplement.message_id), 30)
        report["receipt"] = received.status
        report["message_id"] = received.message_id
        report["source_tool_call_id"] = received.source_tool_call_id
        report["delivered_seconds"] = round(time.time() - started_at, 3)
        assert received.status == "delivered", received
        assert received.input_mode == "steer" and received.source_session_id == source_sid
        assert not reader.done() and not gate.cancelled
        assert len(target_model.messages) == report["target_calls_before_steer"]
        if initial_delivery == "idle_steer":
            assert store.get(first["message_id"]).status == "running"
        gate.release.set()
        completed = await asyncio.wait_for(reader, 120)
        if initial_delivery == "idle_steer":
            assert completed.status == "succeeded", completed
        answer = "".join(str(r.content or "") for r in target_model.responses if not r.tool_calls)
        report["target_answer"] = answer
        report["target_model_calls"] = len(target_model.messages)
        report["source_model_calls"] = len(source_model.messages)
        report["tool_cancelled"] = gate.cancelled
        report["final_mailbox_status"] = store.get(received.message_id).status
        assert marker in answer and "RED" in answer and "agent_session" in answer, answer
        assert marker not in str(target_model.messages[0])
        model_input = str(target_model.messages[-1])
        assert marker in model_input and "internal_dispatch" in model_input and source_sid in model_input
        boundary = next(c.payload for c in chunks if getattr(c, "type", None) == "session_input_received")
        assert boundary["cross_session"]["message_id"] == received.message_id
        assert boundary["message_origin"] == "cross_session_agent"
        assert store.get(received.message_id).status == "delivered"
        report["passed"] = True
    finally:
        report["source_answer"] = "".join(str(r.content or "") for r in source_model.responses)
        report["source_tool_calls"] = [
            t.model_dump() for m in (source_model.messages[-1] if source_model.messages else [])
            for t in (getattr(m, "tool_calls", None) or [])
        ]
        report["source_tool_results"] = [
            str(m.content) for messages in source_model.messages for m in messages
            if str(getattr(m, "role", "")) == "tool"
        ]
        report["elapsed_seconds"] = round(time.time() - started_at, 3)
        report_dir = Path(os.environ["JIUWENSWARM_DATA_DIR"]) / "live-reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{initial_delivery}-{suffix}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        gate.release.set()
        await mailbox.stop()
        if reader is not None:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if source_output is not None:
            await source_output.close(abort_active_round=True)
        if source is not None:
            await source.stop()
        await target.stop()
        await runtime.close()
        await Runner.stop()
