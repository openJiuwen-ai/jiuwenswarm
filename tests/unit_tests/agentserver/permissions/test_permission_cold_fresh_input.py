"""Cold, fully unanswered Smart batches yield to a new user turn."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness.common import session_ops_service
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, permission_continuation as continuation
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _interrupt, _request, answer_host,
)
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold
from tests.unit_tests.agentserver.permissions.test_round_state_persistence import (
    _recovered_ids, sqlite_checkpoint,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def hosts(tmp_path, monkeypatch, sqlite_checkpoint):
    """Independent Host objects retain actual SDK construction and fixture cleanup."""
    generators = []

    async def make(name, sid):
        root = tmp_path / name / sid.removeprefix("cold-")
        root.mkdir(parents=True)
        generator = cold.__wrapped__(root, monkeypatch)
        h = await anext(generator)
        generators.append(generator)
        h.adapter.mark_as_session_scoped(sid)
        generator = answer_host.__wrapped__(h, monkeypatch)
        h = await anext(generator)
        generators.append(generator)
        h.raw["permissions"]["mode"] = "auto"
        monkeypatch.setattr(h.adapter, "persist_skill_retrieval_session_profile", Mock())
        return h

    try:
        yield make
    finally:
        for generator in reversed(generators):
            await generator.aclose()


def fresh(h, **params):
    return AgentRequest(request_id="fresh-cold", session_id=h.adapter._parent_session_id,
        channel_id="web", req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "query": "FRESH-COLD-TOKEN", **params})


def parent_for(h, monkeypatch):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    parent._session_instance_config = {"channel_id": "web", "project_dir": str(h.root)}
    monkeypatch.setattr(parent, "_new_session_scoped_adapter", lambda _sid: h.adapter)
    monkeypatch.setattr(parent, "_load_skill_retrieval_session_profile", lambda _sid: None)
    monkeypatch.setattr(parent, "_reload_session_adapter_if_stale", AsyncMock())
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _sid: True)
    monkeypatch.setattr(session_ops_service, "load_history_records", lambda _sid: [
        {"role": "user", "request_id": "original", "content": "Run the old probe"},
    ])
    return parent


async def stopped_checkpoint(h, reopen):
    assert await _recovered_ids(h)
    await h.adapter._isolate_permission_instance()
    assert h.adapter._permission_state.permission_cleanup_complete
    await reopen()


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("prewarmed", [False, True])
async def test_cold_fresh_text_replaces_real_sqlite_smart_batch(
    hosts, sqlite_checkpoint, monkeypatch, count, prewarmed,
):
    old = await hosts("old", "cold-fresh")
    await _interrupt(old, monkeypatch, mode="auto", kind="permission", count=count)
    assert await _recovered_ids(old) == {f"call_{i}" for i in range(count)}
    await stopped_checkpoint(old, sqlite_checkpoint)
    h = await hosts("new", "cold-fresh")
    assert h.adapter._root_permission_queue is not old.adapter._root_permission_queue
    parent = parent_for(h, monkeypatch)
    request = fresh(h)
    if prewarmed:
        assert await parent._get_or_create_session_adapter(request.session_id) is h.adapter
        assert await _recovered_ids(h) == {f"call_{i}" for i in range(count)}
        assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY).interrupted_tools
    child = await parent._get_or_create_session_adapter(
        request.session_id, execution_request=request, host_external_input=True,
    )
    assert child is h.adapter and child._instance is not old.adapter._instance
    assert not child._root_permission_queue.has_live(root_session_id=request.session_id)
    context = child._instance.react_agent.context_engine.get_context(session_id=request.session_id)
    messages = context.get_messages()
    cancellations = [m for m in messages if getattr(m, "role", None) == "tool"]
    assert {m.tool_call_id for m in cancellations} == {f"call_{i}" for i in range(count)}
    assert len(cancellations) == count
    h.script.responses = [AssistantMessage(content="FRESH-COLD-COMPLETED")]
    result = await _request(h, query=request.params["query"])
    assert result.ok and "FRESH-COLD-COMPLETED" in repr(result.payload)
    assert len(h.script.calls) == 1 and "FRESH-COLD-TOKEN" in repr(h.script.calls)
    assert old.executions == h.executions == []
    assert not child._instance.is_invoke_active and child._instance.active_round is None
    await sqlite_checkpoint()
    assert await _recovered_ids(h) == set()


@pytest.mark.parametrize("fault", ["save", "cancel"])
async def test_restored_batch_rolls_back_context_and_session_on_base_exception(
    hosts, sqlite_checkpoint, monkeypatch, fault,
):
    old = await hosts("old", "cold-rollback")
    await _interrupt(old, monkeypatch, mode="auto", kind="permission")
    await stopped_checkpoint(old, sqlite_checkpoint)
    h = await hosts("new", "cold-rollback")
    await h.create()
    await h.adapter.start_interaction("cold-rollback")
    parent_for(h, monkeypatch)
    assert await session_ops_service.warmup_session_context(
        deep_agent=h.adapter._instance, session_id="cold-rollback",
    )
    instance = h.adapter._instance
    engine = instance.react_agent.context_engine
    context = engine.get_context(session_id="cold-rollback")
    before = deepcopy(context.save_state())
    pending = deepcopy(instance.loop_session.get_state(INTERRUPTION_KEY))
    session_context = deepcopy(instance.loop_session.get_state("context"))
    original = engine.save_contexts
    error = asyncio.CancelledError if fault == "cancel" else OSError
    monkeypatch.setattr(engine, "save_contexts", AsyncMock(side_effect=error("injected")))
    with pytest.raises((OSError, asyncio.CancelledError, continuation.RootPermissionQueueError)):
        await continuation.discard_restored_permission_batch(instance, "cold-rollback", "cold-rollback")
    assert context.save_state() == before
    assert instance.loop_session.get_state(INTERRUPTION_KEY) == pending
    assert instance.loop_session.get_state("context") == session_context
    monkeypatch.setattr(engine, "save_contexts", original)
    assert await continuation.discard_restored_permission_batch(instance, "cold-rollback", "cold-rollback")
    assert len([m for m in context.get_messages() if getattr(m, "role", None) == "tool"]) == 1
    assert h.executions == []


@pytest.mark.parametrize("fault,prewarmed", [
    ("warmup", False), ("save", False), ("cancel", False),
    ("save", True), ("cancel", True),
])
async def test_failed_cold_initialization_cannot_cache_bypass_on_second_request(
    hosts, sqlite_checkpoint, monkeypatch, fault, prewarmed,
):
    old = await hosts("old", "cold-parent-failure")
    await _interrupt(old, monkeypatch, mode="auto", kind="permission")
    await stopped_checkpoint(old, sqlite_checkpoint)
    h = await hosts("first", "cold-parent-failure")
    parent = parent_for(h, monkeypatch)
    warmup = session_ops_service.warmup_session_context

    async def fail(**kwargs):
        if fault == "warmup":
            return False
        assert await warmup(**kwargs)
        engine = kwargs["deep_agent"].react_agent.context_engine
        error = asyncio.CancelledError if fault == "cancel" else OSError
        monkeypatch.setattr(engine, "save_contexts", AsyncMock(side_effect=error("injected")))
        return True

    monkeypatch.setattr(session_ops_service, "warmup_session_context", fail)
    for attempt in range(2):
        if attempt:
            h = await hosts("second", "cold-parent-failure")
            monkeypatch.setattr(parent, "_new_session_scoped_adapter", lambda _sid: h.adapter)
            await sqlite_checkpoint()
        if prewarmed:
            assert await parent._get_or_create_session_adapter("cold-parent-failure") is h.adapter
            assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY).interrupted_tools
        with pytest.raises((OSError, asyncio.CancelledError, continuation.RootPermissionQueueError)):
            await parent._get_or_create_session_adapter(
                "cold-parent-failure", execution_request=fresh(h), host_external_input=True,
            )
        cached = parent._session_adapters.get("cold-parent-failure")
        assert cached is None or cached._permission_state.permission_isolated
        assert h.adapter._permission_state.permission_isolated
        assert not h.adapter._instance.interaction_started
        assert h.executions == []


@pytest.mark.parametrize("owner,params", [
    ("permission", {}), ("goal", {}),
    (None, {"answers": []}), (None, {"card_id": "old"}), (None, {"request_id": "old"}),
    (None, {"input_mode": "steer"}), (None, {"attach_goal": True}), (None, {"source": "internal"}),
])
async def test_ineligible_input_or_live_owner_excludes_cold_validation(hosts, monkeypatch, owner, params):
    h = await hosts("old", "cold-ineligible")
    await _interrupt(h, monkeypatch, mode="auto", kind="permission")
    validator = Mock(side_effect=AssertionError("ineligible input must not enter cold validation"))
    monkeypatch.setattr(interface_deep, "unanswered_permission_batch", validator)
    if owner != "permission":
        monkeypatch.setattr(h.adapter, "_permission_work_pending", lambda *_args, **_kwargs: False)
    if owner == "goal":
        monkeypatch.setattr(h.adapter, "_goal_record_is_active", lambda: True)
    assert not h.adapter._restored_permission_fresh_input(fresh(h, **params))
    validator.assert_not_called()
    assert h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY).interrupted_tools
    assert h.executions == []


@pytest.mark.parametrize("malformed", ["id", "arguments", "root", "partial"])
async def test_canonical_batch_validation_rejects_mismatch_without_mutation(
    hosts, monkeypatch, malformed,
):
    h = await hosts("old", "cold-invalid")
    await _interrupt(h, monkeypatch, mode="auto", kind="permission", count=2)
    instance = h.adapter._instance
    state = deepcopy(instance.loop_session.get_state(INTERRUPTION_KEY))
    entry = state.interrupted_tools["call_0"]
    if malformed == "id":
        entry.tool_call = entry.tool_call.model_copy(update={"id": "foreign"})
    elif malformed == "arguments":
        entry.tool_call = entry.tool_call.model_copy(update={"arguments": '{"value":"foreign"}'})
    elif malformed == "root":
        request = next(iter(entry.interrupt_requests.values()))
        request.metadata["tool_invocation_key"]["root_session_id"] = "foreign"
    else:
        state.interrupted_tools.pop("call_0")
    instance.loop_session.update_state({INTERRUPTION_KEY: state})
    before = deepcopy(state)
    if malformed == "partial":
        assert continuation.unanswered_permission_batch(instance, "cold-invalid", "cold-invalid") is None
    else:
        with pytest.raises(continuation.RootPermissionQueueError, match="identity|state"):
            continuation.unanswered_permission_batch(instance, "cold-invalid", "cold-invalid")
    assert instance.loop_session.get_state(INTERRUPTION_KEY) == before
    assert h.executions == []
