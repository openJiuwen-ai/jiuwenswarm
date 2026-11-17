"""Unanswered streaming approvals survive native snapshots without replaying tools."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard

from jiuwenswarm.agents.harness.common import session_ops_service
from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context_checkpointer import ContextCheckpointer
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, permission_continuation as continuation
from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
from jiuwenswarm.server.runtime.session import history_artifacts, lifecycle, session_history
from tests.unit_tests.agentserver.permissions.test_permission_cold_fresh_input import hosts as cold_hosts
from tests.unit_tests.agentserver.permissions.test_round_state_persistence import (
    _recovered_ids, _send_request, sqlite_checkpoint,
)
from tests.unit_tests.test_context_snapshots import user
from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController

pytestmark = pytest.mark.asyncio


@pytest.fixture
def hosts(cold_hosts, monkeypatch):
    workspaces = {}

    async def make(name, sid, mode="auto"):
        h = await cold_hosts(name, sid, mode=mode)
        # A cold restart creates a new Host, retaining the session's project.
        h.root = workspaces.setdefault(sid, h.root)
        h.raw["react"]["workspace_dir"] = str(h.root)
        h.sysop = SysOperation(SysOperationCard(
            id=h.sysop.id, work_config=LocalWorkConfig(sandbox_root=[str(h.root)]),
        ))
        monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)
        return h

    return make


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    sessions = tmp_path / "sessions"
    for module in (snapshots, history_artifacts, lifecycle, session_history):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path)
    sid = "cold-snapshot"
    lifecycle.claim_runtime(sid)
    path = session_history.get_write_history_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    return SimpleNamespace(sid=sid, path=path, write=write)


@pytest.fixture
async def snapshot_checkpoint(sqlite_checkpoint, monkeypatch):
    """Use the production snapshot wrapper around the actual reopenable SQLite store."""
    async def reopen():
        delegate = await sqlite_checkpoint()
        monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", ContextCheckpointer(delegate))

    await reopen()
    return reopen


async def _suspend(hosts, stored, monkeypatch, mode, count):
    stored.write([user("seed", "HISTORICAL-ANCHOR"), user("request-1", "OLD-SNAPSHOT-PROBE")])
    h = await hosts("old", stored.sid, mode=mode)
    await h.create()
    await h.adapter.start_interaction(stored.sid)
    assert await session_ops_service.warmup_session_context(
        deep_agent=h.adapter._instance, session_id=stored.sid, history_before_request_id="request-1",
    )

    async def fragmented_stream(*args, **kwargs):
        response = await h.script.invoke(*args, **kwargs)
        for index, call in enumerate(response.tool_calls or []):
            middle = len(call.arguments) // 2
            yield AssistantMessageChunk(content="", tool_calls=[call.model_copy(update={
                "arguments": call.arguments[:middle], "index": index,
            })])
            yield AssistantMessageChunk(content="", tool_calls=[call.model_copy(update={
                "name": "", "arguments": call.arguments[middle:], "index": index,
            })])

    monkeypatch.setattr(h.model, "stream", fragmented_stream)
    h.script.responses = [AssistantMessage(content="", tool_calls=[
        ToolCall(id=f"snapshot_call_{index}", type="function", name=h.tool.card.name,
                 arguments=json.dumps({"value": f"old-{index}"}))
        for index in range(count)
    ])]
    output = await _send_request(h, streaming=True, query="OLD-SNAPSHOT-PROBE")
    assert "permission_interrupt" in repr(output)
    instance = h.adapter._instance
    state = instance.loop_session.get_state(INTERRUPTION_KEY)
    assert [call.index for call in state.ai_message.tool_calls] == list(range(count))
    reference = instance.loop_session.get_state(snapshots.SNAPSHOT_KEY)
    raw = json.loads((snapshots.snapshot_directory(stored.sid) / reference["file"]).read_text())
    pending_ids = {call.id for call in state.ai_message.tool_calls}
    saved_calls = [call for context in raw["contexts"].values() for message in context["messages"]
                   for call in message.get("tool_calls", []) if call["id"] in pending_ids]
    assert {call["id"] for call in saved_calls} == pending_ids
    assert all("index" not in call for call in saved_calls)
    assert not h.executions
    return h, pending_ids


def _parent(h, monkeypatch):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    parent._session_instance_config = {"channel_id": "web", "project_dir": str(h.root)}
    monkeypatch.setattr(parent, "_new_session_scoped_adapter", lambda _sid: h.adapter)
    monkeypatch.setattr(parent, "_load_skill_retrieval_session_profile", lambda _sid: None)
    monkeypatch.setattr(parent, "_reload_session_adapter_if_stale", AsyncMock())
    return parent


def _fresh(h, number=11, text="FRESH-SNAPSHOT-TOKEN"):
    h.request_count = number - 1
    return AgentRequest(request_id=f"request-{number}", session_id=h.adapter._parent_session_id,
                        channel_id="web", req_method=ReqMethod.CHAT_SEND,
                        params={"mode": "agent", "query": text})


def _append(stored, request):
    with stored.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(user(request.request_id, request.params["query"])) + "\n")


def _runtime_with_history(h, monkeypatch):
    """Keep the real Runtime/Host/Core and production history writer together."""
    monkeypatch.setattr(h.facade, "_ensure_adapter", lambda **_kwargs: h.adapter)
    h.facade._session_manager = SimpleNamespace(get_session_id=lambda value: value)
    h.facade._sdk_name = "deep"
    monkeypatch.setattr(h.facade, "reconcile_session_mcp", AsyncMock())
    from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
    monkeypatch.setattr(prompt_suggestion.forks, "request_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt_suggestion.forks, "turn_ended", lambda *_args, **_kwargs: None)

    class HostEntry:
        async def process_message_stream(self, request):
            async for chunk in h.facade.process_message_stream(request):
                yield chunk

        async def deliver_control_input(self, request):
            async for chunk in h.facade.deliver_control_input(request):
                yield chunk

    manager = FakeAgentManager()
    manager.agent = HostEntry()
    return AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=FakePlanController())


async def _recorded_turn(runtime, request):
    output = [event async for event in runtime.stream(request, trigger_hook=False)]
    assert await asyncio.to_thread(session_history.flush_pending_writes)
    return output


def _observe_native_interrupt(monkeypatch, stored, pending_ids, result_id):
    """Capture actual native storage commits, without adding a repair commit."""
    delegate = CheckpointerFactory._default_checkpointer.delegate
    ready, snapshots_at_commit = asyncio.Event(), []

    def observe(commit):
        async def wrapped(session):
            result = await commit(session)
            state = session.state().get_global()
            pending = state.get(INTERRUPTION_KEY)
            if pending and set(pending.interrupted_tools) == pending_ids:
                reference = state[snapshots.SNAPSHOT_KEY]
                saved = snapshots.load_checkpoint_snapshot(stored.sid, reference)
                messages = saved.contexts["default_context_id"]["messages"]
                if any(getattr(message, "tool_call_id", None) == result_id for message in messages):
                    snapshots_at_commit.append(saved)
                    ready.set()
            return result
        return wrapped

    for name in ("interrupt_agent_execute", "post_agent_execute"):
        monkeypatch.setattr(delegate, name, observe(getattr(delegate, name)))
    return ready, snapshots_at_commit


def _cancellations(h, pending_ids):
    context = h.adapter._instance.react_agent.context_engine.get_context(session_id=h.adapter._parent_session_id)
    return [message for message in context.get_messages() if getattr(message, "tool_call_id", None) in pending_ids]


async def _select(parent, request):
    return await parent._get_or_create_session_adapter(
        request.session_id, execution_request=request, host_external_input=True,
        history_before_request_id=request.request_id,
    )


async def _two_batch_first_card(hosts, stored, monkeypatch, mode):
    old = await hosts("two-batches", stored.sid, mode=mode)
    await old.create()
    await old.adapter.start_interaction(stored.sid)
    old.script.responses = [AssistantMessage(content="", tool_calls=[ToolCall(
        id=call_id, name=old.tool.card.name, type="function", arguments=json.dumps({"value": value}), index=0,
    )]) for call_id, value in [("written-once", "FIRST_ONCE"), ("unanswered-read", "SECOND_PENDING")]]
    runtime = _runtime_with_history(old, monkeypatch)
    request = AgentRequest(request_id="original-query", session_id=stored.sid, channel_id="web",
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"mode": "agent", "project_dir": str(old.root), "query": "Perform two steps in order"})
    first = await _recorded_turn(runtime, request)
    card = next(event.payload for event in first if event.event_type == "chat.ask_user_question")
    assert not old.executions
    return old, runtime, card


def _first_answer(old, stored, card, mode):
    answer = {"selected_options": ["allow_once"]}
    if mode == "auto":
        answer["card_id"] = card["questions"][0]["card_id"]
    return AgentRequest(request_id="first-answer", session_id=stored.sid, channel_id="web",
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"mode": "agent", "project_dir": str(old.root), "query": "", "source": "permission_interrupt",
                "request_id": card["request_id"], "interrupt_id": card.get("interrupt_id"), "answers": [answer]})


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("history_order", ["before_snapshot", "after_snapshot"])
async def test_two_tool_batches_keep_consumption_through_real_runtime_and_restart(
    hosts, stored, snapshot_checkpoint, monkeypatch, mode, history_order,
):
    old, runtime, card = await _two_batch_first_card(hosts, stored, monkeypatch, mode)
    native_ready, committed = _observe_native_interrupt(monkeypatch, stored, {"unanswered-read"}, "written-once")
    result_written = asyncio.Event()
    persist = old.facade._persist_control_chunk_to_history

    async def ordered_history(chunk, **kwargs):
        event_type = (chunk.payload or {}).get("event_type")
        if event_type == "chat.tool_call" and history_order == "after_snapshot":
            await asyncio.wait_for(native_ready.wait(), 10)
        await persist(chunk, **kwargs)
        if event_type == "chat.tool_result":
            assert await asyncio.to_thread(session_history.flush_pending_writes)
            result_written.set()

    monkeypatch.setattr(old.facade, "_persist_control_chunk_to_history", ordered_history)
    stream = old.script.stream

    async def ordered_model(*args, **kwargs):
        if history_order == "before_snapshot":
            await asyncio.wait_for(result_written.wait(), 10)
        async for chunk in stream(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(old.model, "stream", ordered_model)
    second = await _recorded_turn(runtime, _first_answer(old, stored, card, mode))
    assert any(event.event_type == "chat.ask_user_question" for event in second)
    assert not any(event.event_type == "chat.error" for event in second)
    assert old.executions == [{"value": "FIRST_ONCE"}] and len(old.script.calls) == 2
    assert committed and "first-answer" in committed[0].covered_requests
    instance = old.adapter._instance
    pending = instance.loop_session.get_state(INTERRUPTION_KEY)
    assert [call.id for call in pending.ai_message.tool_calls] == ["unanswered-read"]
    reference = instance.loop_session.get_state(snapshots.SNAPSHOT_KEY)
    raw = json.loads((snapshots.snapshot_directory(stored.sid) / reference["file"]).read_text())
    assert raw["source"]["offset"] > 0 or "original-query" in raw["covered_requests"]
    await old.adapter._isolate_permission_instance()
    await snapshot_checkpoint()

    new = await hosts("after-two-batches", stored.sid, mode=mode)
    fresh = _fresh(new, text="CURRENT-ONLY-ONCE")
    assert await _select(_parent(new, monkeypatch), fresh) is new.adapter
    context = new.adapter._instance.react_agent.context_engine.get_context(session_id=stored.sid)
    messages = context.get_messages()
    assert [call.id for message in messages for call in getattr(message, "tool_calls", ()) or ()] == [
        "written-once", "unanswered-read",
    ]
    assert len([message for message in messages if getattr(message, "tool_call_id", None) == "written-once"]) == 1
    assert len(_cancellations(new, {"unanswered-read"})) == 1
    new.script.responses = [AssistantMessage(content="CURRENT-RECOVERED")]
    result = await _recorded_turn(_runtime_with_history(new, monkeypatch), fresh)
    assert "CURRENT-RECOVERED" in repr(result)
    assert repr(new.script.calls).count("CURRENT-ONLY-ONCE") == 1
    assert not new.executions
    await new.adapter._isolate_permission_instance()
    await snapshot_checkpoint()
    assert await _recovered_ids(new) == set()

    again = await hosts("second-two-batch-restart", stored.sid, mode=mode)
    request = _fresh(again, number=21, text="AFTER-SECOND-TWO-BATCH-RESTART")
    assert await _select(_parent(again, monkeypatch), request) is again.adapter
    assert len(_cancellations(again, {"unanswered-read"})) == 1
    again.script.responses = [AssistantMessage(content="SECOND-RESTART-RECOVERED")]
    result = await _recorded_turn(_runtime_with_history(again, monkeypatch), request)
    assert "SECOND-RESTART-RECOVERED" in repr(result)
    assert repr(again.script.calls).count(request.params["query"]) == 1
    assert not again.executions


async def test_legacy_bad_boundary_is_not_silently_healed(hosts, stored, snapshot_checkpoint, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputGuard
    # Generate a minimal legacy fixture through Core, without the new producers.
    with monkeypatch.context() as legacy:
        legacy.setattr(SessionInputGuard, "_record_consumed_inputs", lambda *_args: None)
        legacy.setattr(SessionInputGuard, "_tag_continuation_result", lambda *_args: None)
        old, runtime, card = await _two_batch_first_card(hosts, stored, monkeypatch, "auto")
        await _recorded_turn(runtime, _first_answer(old, stored, card, "auto"))
    reference = old.adapter._instance.loop_session.get_state(snapshots.SNAPSHOT_KEY)
    path = snapshots.snapshot_directory(stored.sid) / reference["file"]
    original = path.read_bytes()
    raw = json.loads(original)
    assert raw["source"]["offset"] == 0 and not raw["covered_requests"]
    await old.adapter._isolate_permission_instance()
    await snapshot_checkpoint()
    new = await hosts("legacy-bad", stored.sid)
    with pytest.raises(continuation.RootPermissionQueueError, match="permission_continuation_.*(mismatch|failed)"):
        await _select(_parent(new, monkeypatch), _fresh(new))
    assert old.executions == [{"value": "FIRST_ONCE"}] and not new.executions
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("outcome", ["allow_once", "reject", "tool_error"])
async def test_partial_resume_first_native_commit_covers_its_actual_result(
    hosts, stored, snapshot_checkpoint, monkeypatch, mode, outcome,
):
    old = await hosts("partial", stored.sid, mode=mode)
    await old.create()
    await old.adapter.start_interaction(stored.sid)
    old.script.responses = [AssistantMessage(content="", tool_calls=[ToolCall(
        id=f"snapshot_call_{index}", type="function", name=old.tool.card.name,
        arguments=json.dumps({"value": f"old-{index}"}), index=index,
    ) for index in range(2)])]
    runtime = _runtime_with_history(old, monkeypatch)
    await _recorded_turn(runtime, AgentRequest(request_id="partial-root", session_id=stored.sid,
        channel_id="web", req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"mode": "agent", "project_dir": str(old.root), "query": "Run the two probes"}))
    ready, committed = _observe_native_interrupt(monkeypatch, stored, {"snapshot_call_1"}, "snapshot_call_0")
    persist = old.facade._persist_control_chunk_to_history

    async def late_history(chunk, **kwargs):
        if (chunk.payload or {}).get("event_type") in {"chat.tool_call", "chat.tool_result"}:
            await asyncio.wait_for(ready.wait(), 10)
        await persist(chunk, **kwargs)

    monkeypatch.setattr(old.facade, "_persist_control_chunk_to_history", late_history)
    pending = old.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    entry = pending.interrupted_tools["snapshot_call_0"]
    question = next(iter(entry.interrupt_requests.values()))
    if outcome == "allow_once":
        async def rewrite_result(ctx):
            if ctx.inputs.tool_call.id == "snapshot_call_0" and ctx.inputs.tool_msg is not None:
                ctx.inputs.tool_msg = ctx.inputs.tool_msg.model_copy(update={
                    "content": "rewritten actual result", "metadata": {"earlier_rail": True},
                })
        await old.adapter._instance.react_agent.register_callback(
            AgentCallbackEvent.AFTER_TOOL_CALL, rewrite_result, 70,
        )
    if outcome == "tool_error":
        async def fail(inputs, **_kwargs):
            old.executions.append(inputs)
            raise RuntimeError("controlled actual tool failure")
        monkeypatch.setattr(old.tool, "invoke", fail)
    answer = {"selected_options": ["reject" if outcome == "reject" else "allow_once"]}
    if mode == "auto":
        answer["card_id"] = "snapshot_call_0"
    request = AgentRequest(request_id="partial-answer", session_id=stored.sid, channel_id="web",
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"mode": "agent", "project_dir": str(old.root), "query": "", "source": "permission_interrupt",
                "request_id": "snapshot_call_0", "interrupt_id": question.interrupt_id, "answers": [answer]})
    output = await _recorded_turn(runtime, request)
    assert not any(event.event_type == "chat.error" for event in output)
    assert ready.is_set() and committed
    # Observe the first stored result, not the earlier dispatch-only checkpoint.
    saved = committed[0]
    messages = saved.contexts["default_context_id"]["messages"]
    results = [message for message in messages if getattr(message, "tool_call_id", None) == "snapshot_call_0"]
    assert len(results) == 1, [(s.cursor.offset, s.covered_requests,
        [(getattr(m, "role", None), getattr(m, "tool_call_id", None))
         for m in s.contexts["default_context_id"]["messages"]]) for s in committed]
    assert results[0].metadata["jiuwenswarm_request_ids"] == ["partial-answer"]
    if outcome == "allow_once":
        assert results[0].content == "rewritten actual result" and results[0].metadata["earlier_rail"]
    assert "partial-answer" in saved.covered_requests
    assert not [message for message in messages if getattr(message, "tool_call_id", None) == "snapshot_call_1"]
    assert len(old.executions) == (0 if outcome == "reject" else 1)


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("prewarmed", [False, True])
async def test_streaming_snapshot_cancels_old_approval_and_survives_second_restart(
    hosts, stored, snapshot_checkpoint, monkeypatch, mode, count, prewarmed,
):
    old, pending_ids = await _suspend(hosts, stored, monkeypatch, mode, count)
    await old.adapter._isolate_permission_instance()
    await snapshot_checkpoint()
    assert await _recovered_ids(old) == pending_ids
    h = await hosts("new", stored.sid, mode=mode)
    parent = _parent(h, monkeypatch)
    request = _fresh(h)
    _append(stored, request)
    if prewarmed:
        await parent._get_or_create_session_adapter(stored.sid, history_before_request_id=request.request_id)
        context = h.adapter._instance.react_agent.context_engine.get_context(session_id=stored.sid)
        restored = [call for message in context.get_messages()
                    for call in getattr(message, "tool_calls", None) or [] if call.id in pending_ids]
        assert [call.index for call in restored] == [None] * count
    assert await _select(parent, request) is h.adapter
    assert not h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    cancellations = _cancellations(h, pending_ids)
    assert {message.tool_call_id for message in cancellations} == pending_ids
    assert len(cancellations) == count
    assert all("Superseded by new user input" in message.content for message in cancellations)
    h.script.responses = [AssistantMessage(content="FRESH-SNAPSHOT-COMPLETED")]
    output = await _send_request(h, streaming=True, query=request.params["query"])
    assert "FRESH-SNAPSHOT-COMPLETED" in repr(output)
    assert len(h.script.calls) == 1
    assert repr(h.script.calls).count(request.params["query"]) == 1
    await h.adapter._isolate_permission_instance()
    await snapshot_checkpoint()
    assert await _recovered_ids(h) == set()

    again = await hosts("again", stored.sid, mode=mode)
    request = _fresh(again, number=21, text="AFTER-SECOND-RESTART")
    _append(stored, request)
    assert await _select(_parent(again, monkeypatch), request) is again.adapter
    assert len(_cancellations(again, pending_ids)) == count
    again.script.responses = [AssistantMessage(content="SECOND-RESTART-COMPLETED")]
    output = await _send_request(again, streaming=True, query=request.params["query"])
    assert "SECOND-RESTART-COMPLETED" in repr(output)
    assert len(again.script.calls) == 1
    assert old.executions == h.executions == again.executions == []


@pytest.mark.parametrize("fault", ["save", "cancel"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_snapshot_cleanup_failure_rolls_back_and_evicts_before_retry(
    hosts, stored, snapshot_checkpoint, monkeypatch, mode, fault,
):
    old, pending_ids = await _suspend(hosts, stored, monkeypatch, mode, 1)
    await old.adapter._isolate_permission_instance()
    await snapshot_checkpoint()
    h = await hosts("new", stored.sid, mode=mode)
    parent = _parent(h, monkeypatch)
    request = _fresh(h)
    _append(stored, request)
    await parent._get_or_create_session_adapter(stored.sid, history_before_request_id=request.request_id)
    instance = h.adapter._instance
    context = instance.react_agent.context_engine.get_context(session_id=stored.sid)
    before = deepcopy(context.save_state())
    pending = deepcopy(instance.loop_session.get_state(INTERRUPTION_KEY))
    error = OSError if fault == "save" else asyncio.CancelledError
    with monkeypatch.context() as patch:
        patch.setattr(instance.react_agent.context_engine, "save_contexts", AsyncMock(side_effect=error("injected")))
        with pytest.raises((continuation.RootPermissionQueueError, asyncio.CancelledError)):
            await _select(parent, request)
    assert context.save_state() == before
    assert instance.loop_session.get_state(INTERRUPTION_KEY) == pending
    cached = parent._session_adapters.get(stored.sid)
    assert cached is None or cached._permission_state.permission_isolated
    assert not instance.interaction_started
    await snapshot_checkpoint()
    assert await _recovered_ids(h) == pending_ids
    retry = await hosts("retry", stored.sid, mode=mode)
    assert await _select(_parent(retry, monkeypatch), _fresh(retry)) is retry.adapter
    assert len(_cancellations(retry, pending_ids)) == 1
    assert old.executions == h.executions == retry.executions == []


@pytest.mark.parametrize("damage", ["id", "type", "name", "arguments", "response_item_id",
                                     "count", "order", "duplicate", "result"])
async def test_restored_snapshot_rejects_actual_call_conflicts(
    hosts, stored, snapshot_checkpoint, monkeypatch, damage,
):
    old, pending_ids = await _suspend(hosts, stored, monkeypatch, "manual", 2)
    await old.adapter._isolate_permission_instance()
    await snapshot_checkpoint()
    h = await hosts("new", stored.sid, mode="manual")
    parent = _parent(h, monkeypatch)
    await parent._get_or_create_session_adapter(stored.sid)
    instance = h.adapter._instance
    context = instance.react_agent.context_engine.get_context(session_id=stored.sid)
    assistant = next(message for message in context.get_messages()
                     if any(call.id in pending_ids for call in getattr(message, "tool_calls", None) or []))
    if damage == "count":
        assistant.tool_calls.pop()
    elif damage == "order":
        assistant.tool_calls.reverse()
    elif damage == "duplicate":
        await context.add_messages(deepcopy(assistant))
    elif damage == "result":
        await context.add_messages(ToolMessage(tool_call_id=assistant.tool_calls[0].id, content="already completed"))
    else:
        # Keep the second id so the actual Host guard selects this same batch.
        assistant.tool_calls[0] = assistant.tool_calls[0].model_copy(update={damage: "changed"})
    before = deepcopy(context.save_state())
    pending = deepcopy(instance.loop_session.get_state(INTERRUPTION_KEY))
    with pytest.raises(continuation.RootPermissionQueueError, match="^permission_continuation_context_mismatch$"):
        await continuation.discard_restored_permission_batch(instance, stored.sid, stored.sid)
    assert context.save_state() == before
    assert instance.loop_session.get_state(INTERRUPTION_KEY) == pending
    assert old.executions == h.executions == []


@pytest.mark.parametrize("indexes", [(0, None), (None, None), (0, 0)])
async def test_restored_comparison_ignores_only_index_without_mutating_inputs(indexes):
    expected = [ToolCall(id="call", type="function", name="probe", arguments='{"x": 1}', index=indexes[0])]
    actual = [expected[0].model_copy(update={"index": indexes[1]})]
    original = deepcopy((expected, actual))
    assert continuation._same_restored_tool_calls(expected, actual)
    assert (expected, actual) == original
