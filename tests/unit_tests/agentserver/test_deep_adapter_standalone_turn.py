# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A standalone run such as a manual compaction joins the session's turn sequence."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.observability.turn import SessionTurnTracker
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _session_adapter(tracker: SessionTurnTracker) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._turn_tracker = tracker
    adapter._instance = SimpleNamespace(_loop_session=None)
    return adapter


@pytest.mark.asyncio
async def test_root_adapter_numbers_a_compaction_after_the_session_chat_turns():
    """The root adapter keeps a tracker that never sees the session's chats.

    Resolving there numbered every manual compaction from one, and the viewer,
    which orders turns by number, sorted it in among the earliest turns.
    """
    session_tracker = SessionTurnTracker()
    for _ in range(4):
        session_tracker.resolve(None, continues_turn=False)
    session_adapter = _session_adapter(session_tracker)
    requested_sessions: list[str] = []

    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._turn_tracker = SessionTurnTracker()
    root._instance = SimpleNamespace(_loop_session=None)

    async def get_or_create_session_adapter(session_id):
        requested_sessions.append(session_id)
        return session_adapter

    root._get_or_create_session_adapter = get_or_create_session_adapter

    compaction = await root.resolve_standalone_trajectory_turn("session-1")
    next_chat = session_tracker.resolve(None, continues_turn=False)

    assert requested_sessions == ["session-1"]
    assert compaction.turn_number == 5
    assert next_chat.turn_number == 6
    assert len({compaction.turn_id, next_chat.turn_id}) == 2
    assert root._turn_tracker.current is None


@pytest.mark.asyncio
async def test_session_adapter_opens_a_new_turn_rather_than_continuing_one():
    tracker = SessionTurnTracker()
    previous = tracker.resolve(None, continues_turn=False)
    adapter = _session_adapter(tracker)

    compaction = await adapter.resolve_standalone_trajectory_turn("session-1")

    assert compaction.turn_number == previous.turn_number + 1
    assert compaction.turn_id != previous.turn_id
