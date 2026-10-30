# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The loop Session is persisted when a round ends, not only at dispatch.

A rebuilt adapter (restart, idle eviction) restores the persisted Session.
Persisting only at dispatch leaves the snapshot taken before an answered
interrupt resumed, so the next message would be taken as its answer.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig, CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointer, PersistenceCheckpointerProvider
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep

from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (  # noqa: F401
    _interrupt,
    _request,
    answer_host,
    cold,
)


@pytest.fixture
def checkpointer(monkeypatch):
    persistent = PersistenceCheckpointer(InMemoryKVStore())
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", persistent)
    return persistent


async def _persisted_interrupt(h, checkpointer):
    """What a rebuilt adapter would restore for this Session."""
    restored = create_agent_session(
        session_id=h.adapter._parent_session_id, card=h.adapter._instance.card
    )
    await checkpointer._agent_storage.recover(restored._inner)
    state = restored.get_state(INTERRUPTION_KEY)
    return dict(state.interrupted_tools) if state and state.interrupted_tools else {}


@pytest.fixture(autouse=True)
def disable_observability(answer_host, monkeypatch):
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)


async def _send_request(h, streaming=False, **params):
    if not streaming:
        result = await _request(h, **params)
        assert result.ok
        return result
    h.request_count += 1
    request = AgentRequest(
        request_id=f"request-{h.request_count}",
        session_id=h.adapter._parent_session_id,
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"mode": "agent", "project_dir": str(h.root), **params},
    )
    inputs, _, _ = h.facade.build_inputs(request)

    async def consume():
        return [
            chunk
            async for chunk in h.adapter.process_message_stream_impl(request, inputs)
        ]

    return await asyncio.wait_for(consume(), 10)


@pytest.mark.asyncio
async def test_pending_interrupt_is_persisted_when_the_round_ends(
    answer_host,  # noqa: F811 - the pytest fixture imported above
    checkpointer,
    monkeypatch,
):
    h = answer_host

    await _interrupt(h, monkeypatch, mode="manual", kind="ask")

    assert list(await _persisted_interrupt(h, checkpointer)) == ["call_0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_answered_interrupt_is_not_restored_after_the_round_ends(
    answer_host,  # noqa: F811 - the pytest fixture imported above
    checkpointer,
    monkeypatch,
    streaming,
):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="ask")

    await _send_request(h, streaming=streaming, query="", **answer)

    assert not h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert await _persisted_interrupt(h, checkpointer) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_request_accepted_into_another_readers_round_does_not_commit_it(
    answer_host,  # noqa: F811 - the pytest fixture imported above
    checkpointer,
    monkeypatch,
    streaming,
):
    h = answer_host
    await _interrupt(h, monkeypatch, mode="manual", kind="ask")
    # Another reader holds the output lease, and its round has moved past
    # the interrupt without ending yet.
    monkeypatch.setattr(
        h.adapter._instance, "attach_output", AsyncMock(return_value=None)
    )
    h.adapter._instance.loop_session.update_state({INTERRUPTION_KEY: None})
    # The dispatch-time turn checkpoint is a separate write, not under test.
    monkeypatch.setattr(h.adapter._turn_tracker, "sync", AsyncMock())

    await _send_request(h, streaming=streaming, query="next")

    assert list(await _persisted_interrupt(h, checkpointer)) == ["call_0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_cancel_during_the_round_commit_still_releases_the_session(
    answer_host,  # noqa: F811 - the pytest fixture imported above
    checkpointer,
    monkeypatch,
    streaming,
):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="ask")
    monkeypatch.setattr(h.adapter._turn_tracker, "sync", AsyncMock())
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_commit():
        started.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(h.adapter._instance.loop_session, "commit", slow_commit)
    request = asyncio.create_task(_send_request(h, streaming=streaming, query="", **answer))
    await asyncio.wait_for(started.wait(), 10)
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)

    try:
        assert not +h.adapter._active_session_ids
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 10)


@pytest.fixture
async def sqlite_checkpoint(tmp_path, monkeypatch):
    """Close and reopen the actual SDK SQLite store, without a cached reader."""
    PersistenceCheckpointerProvider()
    config = CheckpointerConfig(
        type="persistence",
        conf={"db_type": "sqlite", "db_path": str(tmp_path / "round-state")},
    )
    current = None

    async def reopen():
        nonlocal current
        if current is not None:
            await current._kv_store.engine.dispose()
        current = await CheckpointerFactory.create(config)
        monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", current)
        return current

    try:
        await reopen()
        yield reopen
    finally:
        if current is not None:
            await current._kv_store.engine.dispose()


async def _recovered_ids(h, *, session_id=None):
    restored = create_agent_session(
        session_id=session_id or h.adapter._parent_session_id,
        card=h.adapter._instance.card,
    )
    await restored.pre_run(inputs={})
    try:
        state = restored.get_state(INTERRUPTION_KEY)
        return set(state.interrupted_tools) if state and state.interrupted_tools else set()
    finally:
        await restored.close_stream()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("count,first", [(1, 0), (2, 0), (2, 1)])
@pytest.mark.parametrize("action", ["allow_once", "reject", "session_allow", "always_allow"])
async def test_sqlite_reopen_recovers_only_unanswered_tools(
    answer_host, sqlite_checkpoint, monkeypatch, streaming, count, first, action,
):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="permission", count=count)
    pending = {f"call_{index}" for index in range(count)}
    order = [first] if count == 1 else [first, 1 - first]

    # A genuine pending interrupt must survive a fresh Session and store.
    await sqlite_checkpoint()
    assert await _recovered_ids(h) == pending
    assert await _recovered_ids(h, session_id="unrelated-session") == set()
    for index in order:
        if not pending:
            break
        payload = deepcopy(answer)
        payload["request_id"] = f"call_{index}"
        payload["answers"][0]["selected_options"] = [action]
        await _send_request(h, streaming=streaming, **payload)
        pending.remove(f"call_{index}")
        # A session grant can also authorize the next tool of the same name.
        if action == "session_allow" and index == 0:
            pending.clear()
        await sqlite_checkpoint()
        assert await _recovered_ids(h) == pending

    expected = [] if action == "reject" else [
        {"value": "once" if count == 1 else f"once-{index}"} for index in range(count)
    ]
    assert sorted(item["value"] for item in h.executions) == sorted(item["value"] for item in expected)
    assert len(h.script.calls) == 2
    assert not +h.adapter._active_session_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("boundary", ["close", "commit", "observer"])
async def test_round_persistence_and_cleanup_at_output_boundaries(
    answer_host, checkpointer, monkeypatch, streaming, boundary,
):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="ask")
    monkeypatch.setattr(h.adapter._turn_tracker, "sync", AsyncMock())
    warning = Mock(wraps=interface_deep.logger.warning)
    monkeypatch.setattr(interface_deep.logger, "warning", warning)
    if boundary == "close":
        attach = h.adapter._instance.attach_output

        async def failing_close_output():
            output = await attach()
            close = output.close

            async def fail(**kwargs):
                await close(**kwargs)
                raise RuntimeError("test stream close failure")

            monkeypatch.setattr(output, "close", fail)
            return output

        monkeypatch.setattr(h.adapter._instance, "attach_output", failing_close_output)
    elif boundary == "commit":
        monkeypatch.setattr(h.adapter._instance.loop_session, "commit", AsyncMock(side_effect=OSError("test store unavailable")))
    else:
        class ObservedOutput:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            close = AsyncMock()

        output, commit = ObservedOutput(), AsyncMock()
        monkeypatch.setattr(h.adapter._instance.loop_session, "commit", commit)
        monkeypatch.setattr(h.adapter, "_attach_and_send_inputs", AsyncMock(return_value=(output, False)))
    params = {"query": "", "attach_only": True} if boundary == "observer" else answer
    await _send_request(h, streaming=streaming, **params)
    assert not +h.adapter._active_session_ids
    pending = await _persisted_interrupt(h, checkpointer)
    assert list(pending) == ([] if boundary == "close" else ["call_0"])
    if boundary == "commit":
        assert any("round state commit failed" in str(call.args[0]) for call in warning.call_args_list)
    elif boundary == "observer":
        output.close.assert_awaited_once()
        commit.assert_not_awaited()
        assert len(h.script.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
async def test_session_rebuild_after_idle_cleanup_recovers_round_state(
    answer_host, sqlite_checkpoint, monkeypatch, answered,
):
    h = answer_host
    answer = await _interrupt(h, monkeypatch, mode="manual", kind="ask")
    if answered:
        assert (await _request(h, **answer)).ok
    old_session = h.adapter._instance.loop_session
    sid = h.adapter._parent_session_id
    assert await h.adapter.cleanup_session_adapter(sid)
    # The fixture keeps the Host wiring; discard its disposed DeepAgent and
    # recreate it through the same factory used by a new cached adapter.
    h.adapter._instance = None
    await sqlite_checkpoint()
    await h.create()
    await h.adapter.start_interaction(sid)
    assert h.adapter._instance.loop_session is not old_session
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    pending = set(state.interrupted_tools) if state and state.interrupted_tools else set()
    assert pending == (set() if answered else {"call_0"})
    if answered:
        h.script.responses = [AssistantMessage(content="fresh round completed")]
        assert (await _request(h, query="Start a fresh round")).ok
        assert len(h.script.calls) == 3
