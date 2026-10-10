"""Real SDK interrupted-tool continuation through the public Host input path."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, Model, ToolCall
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueueError
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401


class _Script:
    def __init__(self):
        self.responses = []
        self.calls = []

    async def invoke(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        assert self.responses, "unexpected model call"
        return self.responses.pop(0)

    async def stream(self, *args, **kwargs):
        response = await self.invoke(*args, **kwargs)
        yield AssistantMessageChunk(content=response.content, tool_calls=response.tool_calls)


@pytest.fixture
async def answer_host(cold, monkeypatch):
    h = cold
    h.raw["permissions"]["mode"] = "manual"
    h.raw["permissions"]["tools"][h.tool.card.name] = "ask"
    h.script = _Script()
    h.executions = []
    h.dispatches = []
    h.request_count = 0
    monkeypatch.setattr(h.model, "invoke", h.script.invoke)
    monkeypatch.setattr(h.model, "stream", h.script.stream)

    async def reviewer_model(_model, *_args, **_kwargs):
        return AssistantMessage(content=json.dumps({
            "outcome": "manual", "confidence": 1.0, "reason_code": "test_manual",
            "rationale": "Require a person", "manual_reason_code": "test_manual",
            "manual_reason_summary": "Require a person", "user_review_hint": "Review this tool",
        }))

    monkeypatch.setattr(Model, "invoke", reviewer_model)

    async def unexpected_stream(*_args, **_kwargs):
        raise AssertionError("unexpected secondary model stream")
        yield  # Keep the SDK async-iterator transport shape.

    monkeypatch.setattr(Model, "stream", unexpected_stream)
    original_tool = h.tool.invoke

    async def invoke(inputs, **kwargs):
        h.executions.append(deepcopy(inputs))
        return await original_tool(inputs, **kwargs)

    monkeypatch.setattr(h.tool, "invoke", invoke)
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: h.user)
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: h.session)
    monkeypatch.setattr(interface, "get_config", lambda: deepcopy(h.raw))
    h.facade = object.__new__(interface.JiuWenSwarm)
    adapter = h.adapter
    monkeypatch.setattr(adapter, "_handle_slash_command", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_ensure_chat_extensions", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _name="": True)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: h.model)
    from openjiuwen.harness import observability
    from jiuwenswarm.agents.harness import agent_observability
    monkeypatch.setattr(observability, "open_agent_run_span", lambda **_kwargs: None)
    monkeypatch.setattr(observability, "close_agent_run_span", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda: None)
    try:
        yield h
    finally:
        if adapter._instance is not None and adapter._instance.interaction_started:
            await adapter._instance.stop()


async def _request(h, *, session_id=None, **params):
    h.request_count += 1
    request = AgentRequest(
        request_id=f"request-{h.request_count}",
        session_id=session_id or h.adapter._parent_session_id, channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "project_dir": str(h.root), **params},
    )
    inputs, _, _ = h.facade.build_inputs(request)
    return await asyncio.wait_for(h.adapter.process_message_impl(request, inputs), 10)


async def _interrupt(h, monkeypatch, *, mode, kind, count=1):
    h.raw["permissions"]["mode"] = mode
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    original_send = h.adapter._instance.send_input

    async def send(request):
        h.dispatches.append(request)
        return await original_send(request)

    monkeypatch.setattr(h.adapter._instance, "send_input", send)
    calls = []
    for index in range(count):
        value = "once" if count == 1 else f"once-{index}"
        arguments = {"query": "Which colour?"} if kind == "ask" else {"value": value}
        calls.append(ToolCall(
            id=f"call_{index}", type="function", name="ask_user" if kind == "ask" else h.tool.card.name,
            arguments=json.dumps(arguments),
        ))
    h.script.responses = [
        AssistantMessage(content="", tool_calls=calls),
        AssistantMessage(content="completed"),
    ]
    result = await _request(h, query="Run the probe")
    assert result.ok
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert state and state.interrupted_tools
    assert h.executions == [] and len(h.script.calls) == 1
    entry = next(iter(state.interrupted_tools.values()))
    request_id = next(iter(entry.interrupt_requests))
    if kind == "ask":
        return {"source": "ask_user_interrupt", "request_id": request_id,
                "answers": [{"question": "Which colour?", "custom_input": "blue"}]}
    answer = {"selected_options": ["allow_once"]}
    keys = h.adapter._root_permission_queue.snapshot_scope(root_session_id=h.adapter._parent_session_id)
    if mode == "auto":
        assert len(keys) == 1
        answer["card_id"] = keys[0].invocation_id
    return {"source": "permission_interrupt", "request_id": request_id, "answers": [answer]}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind,action", [("permission", "allow_once"), ("permission", "reject"), ("ask", "answer")])
async def test_pending_answer_finishes_old_policy_before_next_task_cutover(answer_host, monkeypatch, mode, kind, action):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode=mode, kind=kind)
    old_permission = h.adapter._permission_rail
    old_epoch = h.adapter._permission_state.permission_epoch
    next_mode = "auto" if mode == "manual" else "manual"
    h.raw["permissions"]["mode"] = next_mode
    h.change()
    with pytest.raises(RuntimeError, match="permission_session_busy"):
        await h.adapter.reload_agent_config(h.raw, reload_scopes={"permissions"})
    if kind == "permission":
        answer["answers"][0]["selected_options"] = [action]
        if action == "reject":
            answer["answers"][0]["custom_input"] = "deny-probe"
    result = await _request(h, **answer)
    assert result.ok
    assert h.executions == ([{"value": "once"}] if action == "allow_once" else [])
    assert len(h.script.calls) == 2
    assert h.adapter._permission_rail is old_permission
    assert h.adapter._permission_state.permission_epoch == old_epoch
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert not state or not state.interrupted_tools
    assert len(h.dispatches) == 2
    assert isinstance(h.dispatches[-1].inputs["query"], InteractiveInput)
    if kind == "ask":
        assert "blue" in repr(h.script.calls[-1])
    elif action == "reject":
        reason = "user_rejected" if mode == "auto" else "deny-probe"
        assert reason in repr(h.script.calls[-1])
    h.script.responses = [AssistantMessage(content="new task completed")]
    result = await _request(h, query="Start the next task")
    assert result.ok
    assert h.adapter._permission_rail is not old_permission
    assert isinstance(h.adapter._permission_rail, AutoPermissionInterruptRail) is (next_mode == "auto")
    assert h.adapter._permission_state.permission_epoch == (h.capture()[0] if next_mode == "auto" else None)
    assert h.executions == ([{"value": "once"}] if action == "allow_once" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("kind", ["permission", "ask"])
@pytest.mark.parametrize("invalid", ["session", "locator", "duplicate"])
async def test_invalid_answer_cannot_consume_or_reexecute_pending_work(answer_host, monkeypatch, mode, kind, invalid):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode=mode, kind=kind)
    old_permission, old_epoch = h.adapter._permission_rail, h.adapter._permission_state.permission_epoch
    h.raw["permissions"]["mode"] = "auto" if mode == "manual" else "manual"
    h.change()
    if invalid == "duplicate":
        accepted = await _request(h, **answer)
        assert accepted.ok
    elif invalid == "locator":
        answer["request_id"] = "foreign-call"
        if "card_id" in answer["answers"][0]:
            answer["answers"][0]["card_id"] = "foreign-card"
    executed, calls = deepcopy(h.executions), len(h.script.calls)
    before = deepcopy(h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY))
    try:
        result = await _request(h, session_id="foreign-session" if invalid == "session" else None, **answer)
    except (RuntimeError, RootPermissionQueueError) as exc:
        assert any(word in str(exc) for word in ("permission", "resume", "session"))
    else:
        assert not result.ok, f"invalid {invalid} answer was acknowledged as successful: {result.payload}"
    assert h.executions == executed
    assert len(h.script.calls) == calls
    assert h.adapter._permission_rail is old_permission and h.adapter._permission_state.permission_epoch == old_epoch
    after = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["free_text", "empty_id", "wrong_id"])
async def test_smart_ask_rejects_legacy_answers_before_sdk_send(answer_host, monkeypatch, invalid):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="auto", kind="ask")
    if invalid == "free_text":
        answer["answers"][0].pop("question")
    else:
        answer["request_id"] = "" if invalid == "empty_id" else "foreign-call"
    before = deepcopy(h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY))
    error = "permission_queue_card_id_invalid" if invalid == "empty_id" else "interaction_resume_"
    with pytest.raises(RootPermissionQueueError, match=error):
        await _request(h, **answer)
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY) == before
    assert len(h.dispatches) == len(h.script.calls) == 1
    assert h.executions == []


@pytest.mark.asyncio
async def test_pending_manual_ask_keeps_free_text_after_smart_is_requested(answer_host, monkeypatch):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="ask")
    answer["answers"][0].pop("question")
    h.raw["permissions"]["mode"] = "auto"
    h.change()
    assert (await _request(h, **answer)).ok
    assert len(h.script.calls) == len(h.dispatches) == 2
    assert "blue" in repr(h.script.calls[-1])
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert not state or not state.interrupted_tools
    assert h.adapter._permission_state.permission_epoch is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission", "ask"])
@pytest.mark.parametrize("desired_smart", [False, True])
async def test_partial_manual_batch_answer_preserves_sdk_semantics(answer_host, monkeypatch, kind, desired_smart):
    h = answer_host
    first_answer = await _interrupt(h, monkeypatch, mode="manual", kind=kind, count=2)
    instance = h.adapter._instance
    pending = instance.loop_session.get_state(INTERRUPTION_KEY).interrupted_tools
    assert len(pending) == 2, "the SDK must produce both pending entries itself"
    locators = {inner_id for entry in pending.values() for inner_id in entry.interrupt_requests}
    assert len(locators) == 2
    original_permission = h.adapter._permission_rail
    h.raw["permissions"]["mode"] = "auto" if desired_smart else "manual"
    h.change()

    result = await _request(h, **first_answer)
    assert result.ok
    assert len(h.script.calls) == 1 and len(h.dispatches) == 2
    remaining = instance.loop_session.get_state(INTERRUPTION_KEY).interrupted_tools
    remaining_ids = {inner_id for entry in remaining.values() for inner_id in entry.interrupt_requests}
    assert len(remaining) == 1 and remaining_ids == locators - {first_answer["request_id"]}
    assert h.adapter._permission_rail is original_permission and h.adapter._permission_state.permission_epoch is None
    assert h.executions == ([{"value": "once-0"}] if kind == "permission" else [])
    submitted = h.dispatches[-1].inputs["query"]
    expected_payload = ({"approved": True, "auto_confirm": False, "feedback": ""}
                        if kind == "permission" else {"answers": {"Which colour?": "blue"}})
    assert submitted.user_inputs == {first_answer["request_id"]: expected_payload}

    second_answer = {**first_answer, "request_id": next(iter(remaining_ids))}
    result = await _request(h, **second_answer)
    assert result.ok
    assert len(h.script.calls) == 2 and len(h.dispatches) == 3
    state = instance.loop_session.get_state(INTERRUPTION_KEY)
    assert not state or not state.interrupted_tools
    assert h.executions == ([{"value": "once-0"}, {"value": "once-1"}] if kind == "permission" else [])
    assert h.adapter._permission_rail is original_permission
    if desired_smart:
        h.script.responses = [AssistantMessage(content="next task")]
        assert (await _request(h, query="Start a new task")).ok
        assert isinstance(h.adapter._permission_rail, AutoPermissionInterruptRail)
        assert h.adapter._permission_state.permission_epoch == h.capture()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [False, True])
async def test_manual_to_smart_rejects_answer_without_exact_locator(answer_host, monkeypatch, raw):
    h = answer_host
    valid_answer = await _interrupt(h, monkeypatch, mode="manual", kind="permission")
    h.raw["permissions"]["mode"] = "auto"
    h.change()
    before = deepcopy(h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY))
    malformed = InteractiveInput("yes") if raw else InteractiveInput()
    with pytest.raises(RootPermissionQueueError, match="interaction_resume_state_missing"):
        await _request(h, query=malformed)
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY) == before
    assert len(h.script.calls) == 1 and len(h.dispatches) == 1 and h.executions == []
    assert (await _request(h, **valid_answer)).ok
    assert len(h.script.calls) == 2 and h.executions == [{"value": "once"}]
