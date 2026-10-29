"""Stale Core permission interrupt must be cleared on fresh user input.

This file covers the regression described in DTS.md Section 5.2 row 2 and 3:
after a permission is allowed and the tool executed, but the SDK leaves the
``INTERRUPTION_KEY`` populated (typical after a backend restart reloads
persisted state), a subsequent fresh user input must NOT replay the old tool.

Cases:

1. ``test_stale_interrupt_clears_on_fresh_input`` — DTS.md 5.2 row 2: stale
   pending (pending tool_call_id + tool result already in messages) on a
   manual session, fresh chat, must clear state and not replay.

2. ``test_stale_interrupt_does_not_duplicate_cancel_for_completed_tool`` —
   stale pending (already-executed tool) must NOT get a duplicate cancel
   ToolMessage appended (would corrupt model context).

3. ``test_active_pending_appends_cancel_marker`` — DTS.md 5.1 regression:
   active pending (no tool result) still appends cancel ToolMessage.

4. ``test_no_pending_state_is_noop`` — DTS.md 5.2 sanity: no pending → no-op.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent.interrupt.response import (
    InterruptRequest,
    ToolCallInterruptRequest,
)
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    ToolInterruptionState,
    ToolInterruptEntry,
)

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _interrupt,
    _request,
    answer_host,
)
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401


pytestmark = pytest.mark.asyncio


async def _run_full_allow_flow(h, monkeypatch, *, mode):
    """Run ``_interrupt`` + allow_once so the tool actually executes and the
    context gets a ToolMessage for the tool_call_id."""
    answer = await _interrupt(h, monkeypatch, mode=mode, kind="permission")
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    interrupted_ids = set(state.interrupted_tools.keys())
    tool_calls_by_id = {
        tool_id: entry.tool_call
        for tool_id, entry in state.interrupted_tools.items()
    }

    answer["answers"][0]["selected_options"] = ["allow_once"]
    if "card_id" in answer["answers"][0]:
        answer["answers"][0]["card_id"] = (
            h.adapter._root_permission_queue.snapshot_scope(
                root_session_id=h.adapter._parent_session_id,
            )[0].invocation_id
        )
    result = await _request(h, **answer)
    assert result.ok, (result.payload, h.script.calls, h.executions)
    assert h.executions == [{"value": "once"}]
    return {
        "interrupted_ids": interrupted_ids,
        "tool_calls_by_id": tool_calls_by_id,
    }


def _restore_stale_state(h, *, allow_once_pending):
    """Re-inject ``INTERRUPTION_KEY`` with the SAME tool_call_id we just allowed.

    After ``allow_once`` + tool execution, SDK clears INTERRUPTION_KEY, but
    a real restart would re-load it from disk because the SDK never persisted
    the cleared value. We simulate that by hand-writing the same call_id back.
    """
    instance = h.adapter._instance
    pending_id = next(iter(allow_once_pending["interrupted_ids"]))
    pending_call = allow_once_pending["tool_calls_by_id"][pending_id]
    interrupt_request = ToolCallInterruptRequest.from_tool_call(
        InterruptRequest(message="stale"), pending_call,
    )
    entry = ToolInterruptEntry(
        tool_call=pending_call,
        interrupt_requests={f"interrupt-{pending_id}": interrupt_request},
    )
    ai_msg = AssistantMessage(content="", tool_calls=[pending_call])
    state = ToolInterruptionState(
        ai_message=ai_msg,
        iteration=1,
        original_query="prior",
        interrupted_tools={pending_id: entry},
    )
    instance.loop_session.update_state({INTERRUPTION_KEY: state})
    return state


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_stale_interrupt_clears_on_fresh_input(
    answer_host, monkeypatch, mode
):
    """DTS.md 5.2 row 2.

    Allow + execute flow leaves messages with a ToolMessage; we manually
    re-populate INTERRUPTION_KEY with the same tool_call_id (simulating the
    SDK failing to clear state across a restart). Fresh chat must drop the
    stale pending and not replay the tool.
    """
    h = answer_host
    pending = await _run_full_allow_flow(h, monkeypatch, mode=mode)
    _restore_stale_state(h, allow_once_pending=pending)
    executions_before_fresh = list(h.executions)

    h.script.responses = [AssistantMessage(content="handled")]
    result = await _request(h, query="I changed my mind, let's just talk.")

    assert result.ok, result.payload
    new_executions = h.executions[len(executions_before_fresh):]
    assert new_executions == [], (
        "stale tool must NOT be replayed when user sends a fresh chat "
        f"(new_executions={new_executions!r} total={h.executions!r})"
    )
    after = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert (after is None) or (not after.interrupted_tools), (
        "stale Core INTERRUPTION_KEY must be cleared after fresh input; "
        f"state={after!r}"
    )


async def test_stale_interrupt_does_not_duplicate_cancel_for_completed_tool(
    answer_host, monkeypatch
):
    """DTS.md 5.2 sanity: stale pending (already-executed tool) must NOT get a
    duplicate cancel ToolMessage appended. Appending one would corrupt the
    model context (tool_call_id paired with two messages)."""
    h = answer_host
    pending = await _run_full_allow_flow(h, monkeypatch, mode="manual")
    _restore_stale_state(h, allow_once_pending=pending)

    context = h.adapter._instance.react_agent.context_engine.get_context(
        session_id=h.adapter._parent_session_id
    )
    cancel_before = [
        m for m in (context.get_messages() or [])
        if "[INTERRUPTED - Superseded" in str(getattr(m, "content", ""))
    ]
    assert not cancel_before, "test fixture must not pre-seed cancel markers"

    h.script.responses = [AssistantMessage(content="handled")]
    result = await _request(h, query="drop the stale interrupt")

    assert result.ok
    cancel_after = [
        m for m in (context.get_messages() or [])
        if "[INTERRUPTED - Superseded" in str(getattr(m, "content", ""))
    ]
    assert cancel_after == [], (
        "stale pending (already-executed tool) must NOT get a cancel "
        f"ToolMessage appended (would break the model context). got={cancel_after!r}"
    )


async def test_active_pending_appends_cancel_marker(answer_host, monkeypatch):
    """DTS.md 5.1 regression: active pending (no tool result yet) still gets
    one cancel ToolMessage per pending tool_call_id when fresh input arrives."""
    h = answer_host
    await _interrupt(h, monkeypatch, mode="manual", kind="permission")
    pending_before = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert pending_before and pending_before.interrupted_tools
    pending_id_set = set(pending_before.interrupted_tools.keys())

    context = h.adapter._instance.react_agent.context_engine.get_context(
        session_id=h.adapter._parent_session_id
    )
    cancel_before = [
        m for m in (context.get_messages() or [])
        if "[INTERRUPTED - Superseded" in str(getattr(m, "content", ""))
    ]
    assert not cancel_before

    h.script.responses = [AssistantMessage(content="handled")]
    result = await _request(h, query="abandon the pending tool")

    assert result.ok
    after = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert (after is None) or (not after.interrupted_tools)
    cancel_after = [
        m for m in (context.get_messages() or [])
        if "[INTERRUPTED - Superseded" in str(getattr(m, "content", ""))
    ]
    assert len(cancel_after) == len(pending_id_set), (
        "each active pending tool must have one cancel marker appended. "
        f"expected={len(pending_id_set)} got={len(cancel_after)} "
        f"pending_ids={pending_id_set!r}"
    )
    cancel_ids = {getattr(m, "tool_call_id", None) for m in cancel_after}
    assert cancel_ids == pending_id_set


async def test_no_pending_state_is_noop(answer_host):
    h = answer_host
    h.raw["permissions"]["mode"] = "manual"
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY) is None

    h.script.responses = [AssistantMessage(content="ok")]
    result = await _request(h, query="nothing pending here")

    assert result.ok
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY) is None
    assert h.executions == []

async def _managed_voice_delegation(h, monkeypatch, query):
    """Send the CHAT_SEND a full-duplex managed task issues (#5486).

    Mirrors ``video_search.execute_core_agent``: channel ``video_tool``,
    ``source``/``log_as_user`` marking a Host-authored prompt, and a managed
    task binding. The Gateway checkpoint binding is out of scope here.
    """
    from contextlib import asynccontextmanager

    from jiuwenswarm.extensions.video_duplex.backend.tasks import execution

    @asynccontextmanager
    async def no_binding(_request, _adapter, _inputs):
        yield

    monkeypatch.setattr(execution, "bind_task_execution", no_binding)
    h.request_count += 1
    request = AgentRequest(
        request_id=f"task-{h.request_count}",
        session_id=h.adapter._parent_session_id, channel_id="video_tool",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "mode": "agent", "project_dir": str(h.root), "query": query, "content": query,
            "source": "video_tool", "log_as_user": False,
            "managed_task_binding": {"endpoint": "gateway", "task_id": "voice-task"},
        },
    )
    inputs, _, _ = h.facade.build_inputs(request)
    return await asyncio.wait_for(h.adapter.process_message_impl(request, inputs), 10)


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_voice_delegation_after_typed_task_does_not_replay_it(
    answer_host, monkeypatch, mode
):
    """#5486: typed task approved and done, then a voice task is delegated.

    The voice task prompt must start a new turn. Before the fix it was fed to
    the stale interrupt as its "answer": Core re-raised the old approval card,
    replayed the old tool and reported its result as the voice task's result.
    """
    h = answer_host
    pending = await _run_full_allow_flow(h, monkeypatch, mode=mode)
    _restore_stale_state(h, allow_once_pending=pending)
    executions_before = list(h.executions)
    calls_before = len(h.script.calls)

    h.script.responses = [AssistantMessage(content="WeChat opened")]
    result = await _managed_voice_delegation(h, monkeypatch, "Open WeChat")

    assert result.ok, result.payload
    assert h.executions[len(executions_before):] == []
    new_calls = h.script.calls[calls_before:]
    assert len(new_calls) == 1, "the voice task must reach the model as a new turn"
    assert "Open WeChat" in repr(new_calls[0])
    after = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert (after is None) or (not after.interrupted_tools)


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_voice_task_drops_leftover_interrupt_that_no_longer_matches_context(
    answer_host, monkeypatch, mode
):
    """Leftover state that cannot be matched to the context never holds a voice task.

    No answer can resume it, so holding the task until it settles would hold it forever.
    """
    h = answer_host
    pending = await _run_full_allow_flow(h, monkeypatch, mode=mode)
    state = _restore_stale_state(h, allow_once_pending=pending)
    state.ai_message = AssistantMessage(content="", tool_calls=[])
    h.adapter._instance.loop_session.update_state({INTERRUPTION_KEY: state})
    assert not h.adapter.session_has_pending_interrupt(h.adapter._parent_session_id)
    executions_before = list(h.executions)

    h.script.responses = [AssistantMessage(content="WeChat opened")]
    result = await _managed_voice_delegation(h, monkeypatch, "Open WeChat")

    assert result.ok, result.payload
    assert h.executions[len(executions_before):] == []
    assert "Open WeChat" in repr(h.script.calls[-1])
    after = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert (after is None) or (not after.interrupted_tools)


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_voice_task_never_answers_or_clears_a_pending_card(answer_host, monkeypatch, mode):
    """A card the user still has to answer stays valid while a voice task waits.

    The voice task is refused (the full-duplex queue holds it), the card is
    neither answered nor cleared, and after the card is approved the old tool
    runs once and the voice task then starts as a new turn.
    """
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode=mode, kind="permission")
    sid = h.adapter._parent_session_id
    before = repr(h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY))
    assert h.adapter.session_has_pending_interrupt(sid)

    try:
        refused = await _managed_voice_delegation(h, monkeypatch, "Open WeChat")
    except RuntimeError as exc:
        assert "审批" in str(exc)
    else:
        assert not refused.ok and "审批" in repr(refused.payload)
    assert h.executions == []
    assert len(h.script.calls) == 1, "the voice prompt must not reach the model or the card"
    assert repr(h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)) == before, "the card must stay intact"
    assert h.adapter.session_has_pending_interrupt(sid)

    result = await _request(h, **answer)
    assert result.ok
    assert h.executions == [{"value": "once"}]
    assert not h.adapter.session_has_pending_interrupt(sid)

    h.script.responses = [AssistantMessage(content="WeChat opened")]
    result = await _managed_voice_delegation(h, monkeypatch, "Open WeChat")
    assert result.ok, result.payload
    assert h.executions == [{"value": "once"}]
    assert "Open WeChat" in repr(h.script.calls[-1])
