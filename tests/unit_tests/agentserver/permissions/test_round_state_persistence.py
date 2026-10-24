# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The loop Session is persisted when a round ends, not only at dispatch.

A rebuilt adapter (restart, idle eviction) restores the persisted Session.
Persisting only at dispatch leaves the snapshot taken before an answered
interrupt resumed, so the next message would be taken as its answer.
"""

from __future__ import annotations

import asyncio

import pytest
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointer
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.agents.harness import agent_observability
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod

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


async def _stream_request(h, **params):
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

    if streaming:
        # The streaming path forces an observability sync; the shared host
        # stubs it for the unary path only.
        monkeypatch.setattr(
            agent_observability, "sync_agent_observability", lambda **_kwargs: None
        )
        await _stream_request(h, query="", **answer)
    else:
        assert (await _request(h, query="", **answer)).ok

    assert not h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert await _persisted_interrupt(h, checkpointer) == {}
