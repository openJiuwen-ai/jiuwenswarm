# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A Team turn rejected before dispatch must not leave the chat processing."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.team import team_manager
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter.interface import _team_preflight_failure_chunks

_REJECTION = RuntimeError("Model selection cannot change while the session is executing")


async def _collect(session_id: str) -> list[AgentResponseChunk]:
    return [
        chunk
        async for chunk in _team_preflight_failure_chunks("req_2", "web", session_id, _REJECTION)
    ]


def _event_types(chunks: list[AgentResponseChunk]) -> list[str | None]:
    return [chunk.payload.get("event_type") if isinstance(chunk.payload, dict) else None for chunk in chunks]


@pytest.mark.asyncio
async def test_rejected_turn_on_idle_team_settles_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(team_manager, "_team_manager", team_manager.TeamManager())

    chunks = await _collect("sess_a")

    assert _event_types(chunks) == ["chat.error", "chat.processing_status", None]
    assert chunks[0].payload["error"] == str(_REJECTION)
    assert chunks[1].payload == {
        "event_type": "chat.processing_status",
        "session_id": "sess_a",
        "is_processing": False,
        "is_complete": True,
    }
    assert chunks[-1].is_complete is True


@pytest.mark.asyncio
async def test_rejected_turn_keeps_processing_while_round_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    manager.begin_round("sess_a", "req_1")

    chunks = await _collect("sess_a")

    assert _event_types(chunks) == ["chat.error", None]
    assert chunks[-1].is_complete is True


def test_team_active_work_tracks_admitted_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(team_manager, "_team_manager", None)
    assert team_manager.team_session_has_active_work("sess_a") is False

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    assert team_manager.team_session_has_active_work("sess_a") is False

    manager.begin_request("sess_a", "req_1")
    assert team_manager.team_session_has_active_work("sess_a") is True

    manager.end_request("sess_a", "req_1")
    assert team_manager.team_session_has_active_work("sess_a") is False
