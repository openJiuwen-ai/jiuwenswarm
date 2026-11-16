"""A real RuntimeService control delivery waits for its real Host output owner."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import answer_host, cold
from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["allow_once", "reject"])
async def test_runtime_smart_early_answer_preserves_control_and_executes_once(
    answer_host, monkeypatch, action,
):
    h = answer_host
    h.raw["permissions"]["mode"] = "auto"
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)
    monkeypatch.setattr(prompt_suggestion.forks, "request_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt_suggestion.forks, "turn_ended", lambda *_args, **_kwargs: None)
    await h.create()
    sid = h.adapter._parent_session_id
    await h.adapter.start_interaction(sid)
    h.script.responses = [AssistantMessage(content="", tool_calls=[ToolCall(
        id="call_early", type="function", name=h.tool.card.name,
        arguments=json.dumps({"value": "ONCE"}),
    )]), AssistantMessage(content="EARLY-ANSWER-COMPLETED")]
    original_send = h.adapter._instance.send_input

    async def send(request):
        h.dispatches.append(request)
        return await original_send(request)

    monkeypatch.setattr(h.adapter._instance, "send_input", send)
    # Runtime admission selects the fixture's actually assembled Host. The
    # control side retains the production facade and all permission dispatch.
    monkeypatch.setattr(h.facade, "_ensure_adapter", lambda **_kwargs: h.adapter)
    h.facade._session_manager = SimpleNamespace(get_session_id=lambda value: value)
    monkeypatch.setattr(h.facade, "reconcile_session_mcp", AsyncMock())
    seen_card, release_old = asyncio.Event(), asyncio.Event()
    cards = []

    class HostEntry:
        async def process_message_stream(self, request):
            inputs, _, _ = h.facade.build_inputs(request)
            stream = h.adapter.process_message_stream_impl(request, inputs)
            try:
                async for chunk in stream:
                    yield chunk
                    if (chunk.payload or {}).get("event_type") == "chat.ask_user_question":
                        cards.append(chunk.payload)
                        seen_card.set()
                        # Model a reader paused after delivering the actual
                        # card; no fake card/EOF/permission answer is installed.
                        await release_old.wait()
            finally:
                await stream.aclose()

        async def deliver_control_input(self, request):
            async for chunk in h.facade.deliver_control_input(request):
                yield chunk

    manager = FakeAgentManager()
    manager.agent = HostEntry()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(),
                           plan_controller=FakePlanController())

    async def collect(request):
        return [event async for event in runtime.stream(request, trigger_hook=False)]

    parent_request = AgentRequest(request_id="early-parent", channel_id="web", session_id=sid,
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"mode": "agent", "project_dir": str(h.root), "query": "Write ONCE after approval"})
    producer = asyncio.create_task(collect(parent_request))
    answer_task = None
    try:
        async with asyncio.timeout(10):
            while not seen_card.is_set():
                if producer.done():
                    await producer
                    raise AssertionError("Host ended before producing a real approval card")
                await asyncio.sleep(0)
        assert len(cards) == 1 and not h.executions
        payload = cards[0]
        card_id = payload["questions"][0]["card_id"]
        before = runtime._session_coordinator.snapshot_session(sid)
        parent = next(row for row in before.executions if row.request_id == "early-parent")
        assert card_id in parent.waiting_control_ids
        answer = AgentRequest(request_id="early-answer", channel_id="web", session_id=sid,
            req_method=ReqMethod.CHAT_SEND, is_stream=True,
            params={"mode": "agent", "project_dir": str(h.root), "query": "",
                    "source": "permission_interrupt", "request_id": payload["request_id"],
                    "interrupt_id": payload.get("interrupt_id"),
                    "answers": [{"card_id": card_id, "selected_options": [action]}]})
        answer_task = asyncio.create_task(collect(answer))
        async with asyncio.timeout(10):
            while not h.adapter._permission_dispatch.lock.locked():
                if answer_task.done():
                    await answer_task
                    raise AssertionError("Control delivery ended before waiting for the old reader")
                await asyncio.sleep(0)
        assert not answer_task.done() and not producer.done()
        assert len(h.dispatches) == 1 and not h.executions
        release_old.set()
        async with asyncio.timeout(10):
            parent_events, answer_events = await asyncio.gather(producer, answer_task)
        assert not [event for event in parent_events + answer_events if event.event_type == "chat.error"]
        assert any(event.event_type == "runtime.accepted" for event in answer_events)
        assert "EARLY-ANSWER-COMPLETED" in repr(answer_events)
        assert h.executions == ([{"value": "ONCE"}] if action == "allow_once" else [])
        assert len(h.dispatches) == 2 and len(h.script.calls) == 2
        assert not h.adapter._permission_dispatch.lock.locked()
        assert not h.adapter._root_permission_queue.has_live(root_session_id=sid)
        assert not h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
        finished = runtime._session_coordinator.snapshot_session(sid)
        original = next(row for row in finished.executions if row.execution_id == parent.execution_id)
        controls = [row for row in finished.executions if row.work_kind.value == "control_input"]
        assert len(controls) == 1
        control = controls[0]
        assert control.parent_execution_id == original.execution_id
        assert original.state.value == control.state.value == "succeeded"
        assert not original.waiting_control_ids and not control.waiting_control_ids
    finally:
        release_old.set()
        for task in (producer, answer_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (producer, answer_task) if task is not None),
                             return_exceptions=True)
