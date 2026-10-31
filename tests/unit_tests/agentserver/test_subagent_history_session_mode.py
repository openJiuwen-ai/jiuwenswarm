"""Regression tests for history tags stored as session modes.

A history tag must not select a second agent for an existing session.
"""

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.server.runtime.session import (
    session_metadata as session_metadata_module,
)


def _capture_metadata_updates(monkeypatch):
    calls = []

    def _fake_update(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        session_metadata_module, "update_session_metadata", _fake_update
    )
    monkeypatch.setattr(
        session_metadata_module,
        "set_session_delivery_context",
        lambda **kwargs: None,
    )
    return calls


def test_subagent_record_does_not_relabel_session_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    calls = _capture_metadata_updates(monkeypatch)

    session_history.append_history_record(
        session_id="s-parent",
        subagent_id="s-parent_sub_general-purpose_1234",
        request_id="s-parent_sub_general-purpose_1234:7",
        channel_id="subagent",
        role="assistant",
        content="subagent said something",
        timestamp=1.0,
        event_type="chat.final",
        mode="subagent",
    )

    assert len(calls) == 1
    assert calls[0]["mode"] is None


def test_parent_record_still_sets_session_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    calls = _capture_metadata_updates(monkeypatch)

    session_history.append_history_record(
        session_id="s-parent",
        request_id="req-1",
        channel_id="slack",
        role="user",
        content="hello",
        timestamp=1.0,
        mode="agent",
    )

    assert len(calls) == 1
    assert calls[0]["mode"] == "agent"


class _RecordingAgentManager:
    def __init__(self):
        self.requested_modes = []

    async def wait_for_session_prewarm(self, session_id):
        return None

    async def get_agent_for_request(self, request, *, mode, **kwargs):
        self.requested_modes.append(mode)
        return object()


def _prepare_turn(monkeypatch, stored_mode, *, params=None):
    """Run ``_prepare_code_mode_chat_turn`` against a stored session mode."""
    monkeypatch.setattr(
        session_metadata_module,
        "get_session_metadata",
        lambda sid, **kwargs: {"session_id": sid, "mode": stored_mode},
    )

    request = AgentRequest(
        request_id="req-steer",
        channel_id="slack",
        session_id="slack_T_C_1.2",
        params=dict(params or {}),
    )

    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    manager = _RecordingAgentManager()
    server._agent_manager = manager

    mode, _sub_mode, _agent = asyncio.run(
        server._prepare_code_mode_chat_turn(
            request, "slack", sync_metadata=False
        )
    )
    return mode, manager, request


@pytest.mark.parametrize("stored_mode", ["subagent", "SubAgent", "unknown"])
def test_non_runtime_stored_mode_is_not_inherited(monkeypatch, stored_mode):
    mode, manager, request = _prepare_turn(monkeypatch, stored_mode)

    # Neither the agent lookup nor the request may inherit the history tag.
    assert manager.requested_modes == ["agent"]
    assert mode == "agent"
    assert request.params.get("mode") == "agent"
    assert agent_ws_server_module._SESSION_PREVIOUS_MODE_KEY not in request.params


def test_real_stored_mode_is_still_inherited(monkeypatch):
    mode, manager, request = _prepare_turn(monkeypatch, "team")

    assert manager.requested_modes == ["team"]
    assert mode == "team"
    assert (
        request.params.get(agent_ws_server_module._SESSION_PREVIOUS_MODE_KEY)
        == "team"
    )


def test_explicit_request_mode_still_wins(monkeypatch):
    _mode, manager, _request = _prepare_turn(
        monkeypatch, "subagent", params={"mode": "team"}
    )

    assert manager.requested_modes == ["team"]
