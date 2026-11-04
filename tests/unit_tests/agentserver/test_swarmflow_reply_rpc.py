# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for handle_swarmflow_reply — adapter-side early validation.

Happy-path delivery builds a HumanAgentMessage via agent-core helpers
(``format_swarmflow_human_reply_target`` / ``HumanAgentMessage``); those
symbols are version-sensitive and are not pinned here. This suite only
covers the missing-params short-circuit that stays inside jiuwenswarm.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_handler() -> JiuWenSwarmDeepAdapter:
    """Bypass the heavy __init__; only the handler needs the session-scoped flag."""
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    return adapter


class _FakeTeamManager:
    """Captures the interact() call; returns a configurable (ok, reason)."""

    def __init__(self, ok: bool = True, reason: str | None = None) -> None:
        self.ok = ok
        self.reason = reason
        self.calls: list[tuple[str, object]] = []

    async def interact(self, session_id: str, user_input: object) -> tuple[bool, str | None]:
        self.calls.append((session_id, user_input))
        return self.ok, self.reason


def _req(**params) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="tui",
        session_id=params.get("session_id", "sess-1"),
        req_method=None,
        params=params,
    )


@pytest.mark.anyio
async def test_handle_swarmflow_reply_rejects_missing_fields():
    """Missing session_id / correlation_id / answer short-circuits with an error."""
    handler = _make_handler()
    tm = _FakeTeamManager()
    with patch(
        "jiuwenswarm.agents.harness.team.get_team_manager", return_value=tm
    ):
        # No answer -> no interact call, error payload.
        resp = await handler.handle_swarmflow_reply(_req(
            session_id="sess-1",
            run_id="run-1",
            correlation_id="review:host:0",
            answer="",
        ))
    assert resp.ok is False
    assert resp.payload == {"ok": False, "error": "missing session_id/correlation_id/answer"}
    assert tm.calls == []


@pytest.mark.anyio
async def test_handle_swarmflow_reply_skip_maps_to_sentinel_without_answer():
    """skip=true carries no answer yet still delivers: the message body is the
    backend's explicit-skip marker, so the human turn skips immediately
    instead of hanging until the human timeout."""
    try:
        from openjiuwen.agent_teams.workflow.backends.avatar_session_backend import HUMAN_REPLY_SKIP
    except ImportError:
        # Only runs where the installed core has the sentinel; the pinned-core
        # behavior is covered by the fail-fast test below.
        pytest.skip("installed agent-core predates HUMAN_REPLY_SKIP")

    handler = _make_handler()
    tm = _FakeTeamManager()
    with patch(
        "jiuwenswarm.agents.harness.team.get_team_manager", return_value=tm
    ):
        resp = await handler.handle_swarmflow_reply(_req(
            session_id="sess-1",
            run_id="run-1",
            correlation_id="review:host:0",
            skip=True,
        ))
    assert resp.ok is True
    assert len(tm.calls) == 1
    _session, msg = tm.calls[0]
    assert msg.body == HUMAN_REPLY_SKIP


@pytest.mark.anyio
async def test_handle_swarmflow_reply_skip_fails_when_core_lacks_sentinel(monkeypatch):
    """skip=true on a core without HUMAN_REPLY_SKIP fails fast instead of
    delivering a literal the old core would render as the person's words."""
    backend = pytest.importorskip(
        "openjiuwen.agent_teams.workflow.backends.avatar_session_backend"
    )
    if hasattr(backend, "HUMAN_REPLY_SKIP"):
        # Modern core: hide the sentinel to emulate the pinned one. A pinned
        # core already lacks it and fails the import naturally.
        monkeypatch.delattr(backend, "HUMAN_REPLY_SKIP")

    handler = _make_handler()
    tm = _FakeTeamManager()
    with patch(
        "jiuwenswarm.agents.harness.team.get_team_manager", return_value=tm
    ):
        resp = await handler.handle_swarmflow_reply(_req(
            session_id="sess-1",
            run_id="run-1",
            correlation_id="review:host:0",
            skip=True,
        ))
    assert resp.ok is False
    assert resp.payload == {"ok": False, "error": "agent-core pin predates HUMAN_REPLY_SKIP"}
    assert tm.calls == []
