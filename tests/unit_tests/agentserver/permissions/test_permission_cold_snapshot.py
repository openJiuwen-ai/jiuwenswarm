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

def _cancellations(h, pending_ids):
    context = h.adapter._instance.react_agent.context_engine.get_context(session_id=h.adapter._parent_session_id)
    return [message for message in context.get_messages() if getattr(message, "tool_call_id", None) in pending_ids]

async def _select(parent, request):
    return await parent._get_or_create_session_adapter(
        request.session_id, execution_request=request, host_external_input=True,
        history_before_request_id=request.request_id,
    )

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

@pytest.mark.parametrize("field", ["id", "type", "name", "arguments", "response_item_id"])
async def test_restored_comparison_preserves_all_other_call_fields(field):
    expected = [ToolCall(id="call", type="function", name="probe", arguments='{"x": 1}', index=0)]
    actual = [expected[0].model_copy(update={field: "different", "index": None})]
    assert not continuation._same_restored_tool_calls(expected, actual)
