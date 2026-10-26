# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for ``AgentWebSocketServer._handle_team_session_reset`` (Task 3).

The handler is the session-scoped counterpart to ``_handle_team_delete``:
explicitly keyed by a single ``session_id`` (no cross-thread scan), clears
shell-side runtime tracking + first-request flag FIRST, then delegates to
``Runner.reset_agent_team_session`` (which drops session tables + releases
the checkpoint while keeping team_info / roster / team_home / binding).

These tests pin:
- runtime team name is resolved from session metadata (``resolve_session_runtime_team_name(metadata) or team_name``);
- shell state is cleared with ``stop_session_runtime(stop_runner=False)`` + ``clear_session_initialized`` BEFORE the Runner reset;
- ``Runner.reset_agent_team_session`` is called with the resolved runtime team name + same session_id + force=True;
- missing team_name/session_id -> BAD_REQUEST (ok=False);
- a Runner failure degrades to ok=False without raising (chat.send path stays usable).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_request(params: dict, *, request_id: str = "req-1") -> SimpleNamespace:
    return SimpleNamespace(
        params=params,
        request_id=request_id,
        channel_id="officeclaw",
        metadata={"team_name": params.get("team_name", "")},
    )


def _ctx_for_reset(mod, request):
    ctx = MagicMock()
    ctx.request = request

    async def _send_wire(wire):
        return await mod.send_wire_payload(None, wire)

    ctx.sink.send_wire = _send_wire
    return ctx


def _capture_response():
    """Return (capture_dict, fake_encode) wiring encode -> capture resp."""
    captured: dict = {}

    def fake_encode(resp, response_id=None):  # noqa: ANN001
        captured["resp"] = resp
        return b"wire"

    return captured, fake_encode


async def _call_handler(request) -> dict:
    """Invoke the handler with all external deps mocked; returns the captured
    AgentResponse. Uses realistic control-RPC params (session_id + team_name
    only, NO `team`/`mode` marker — the reset RPC is team-scoped by relay
    gate, not by is_team_params)."""
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers

    captured, fake_encode = _capture_response()
    fake_tm = MagicMock()
    fake_tm.stop_session_runtime = AsyncMock(return_value=True)
    fake_tm.clear_session_initialized = MagicMock()

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"channel_id": "officeclaw", "team_name": "oc_team_x"},
    ), patch(
        "jiuwenswarm.server.runtime.session.session_metadata."
        "resolve_session_runtime_team_name",
        return_value="oc_team_x__sess_1",
    ), patch(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        return_value=fake_tm,
    ) as get_tm, patch(
        "openjiuwen.core.runner.Runner.reset_agent_team_session",
        new=AsyncMock(return_value=True),
    ) as runner_reset, patch.object(
        team_handlers, "_sessions_dir_for_request", return_value="/tmp/sessions",
    ), patch.object(
        team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode,
    ), patch.object(
        mod, "send_wire_payload", new=AsyncMock(),
    ) as send_wire:
        await team_handlers.handle_team_session_reset(_ctx_for_reset(mod, request))
        captured["fake_tm"] = fake_tm
        captured["get_tm"] = get_tm
        captured["runner_reset"] = runner_reset
        captured["send_wire"] = send_wire
    return captured


@pytest.mark.asyncio
async def test_handler_resolves_runtime_team_name_and_calls_runner_reset():
    # Realistic relay control-RPC params: session_id + team_name ONLY (no
    # `team`/`mode` marker). The reset RPC is gated team-scoped on the relay
    # side (teamParams.isTeamModeForName && teamParams.teamName); the handler
    # must NOT reject these minimal params.
    params = {"team_name": "oc_team_preset-software-dev", "session_id": "sess_1"}
    request = _make_request(params)

    captured = await _call_handler(request)

    # Runner reset called with the RESOLVED runtime team name + same session_id.
    assert captured["runner_reset"].await_count == 1
    _, kwargs = captured["runner_reset"].call_args
    assert kwargs["team_name"] == "oc_team_x__sess_1"
    assert kwargs["session_id"] == "sess_1"
    assert kwargs["force"] is True

    # Shell state cleared FIRST, before the Runner reset.
    tm = captured["fake_tm"]
    tm.stop_session_runtime.assert_awaited_once()
    call = tm.stop_session_runtime.call_args
    assert call.args[0] == "sess_1"            # session_id passed positionally
    assert call.kwargs["stop_runner"] is False
    tm.clear_session_initialized.assert_called_once_with("sess_1")

    # Response ok=True, payload.reset=True, wire sent.
    resp = captured["resp"]
    assert resp.ok is True
    assert resp.payload["reset"] is True
    assert resp.payload["session_id"] == "sess_1"
    captured["send_wire"].assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_bad_request_when_missing_session_id():
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers

    # Realistic params without `team` marker; session_id absent -> BAD_REQUEST.
    params = {"team_name": "oc_team_x"}  # no session_id
    request = _make_request(params)
    captured, fake_encode = _capture_response()

    with patch.object(team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode), \
            patch.object(mod, "send_wire_payload", new=AsyncMock()):
        await team_handlers.handle_team_session_reset(_ctx_for_reset(mod, request))

    resp = captured["resp"]
    assert resp.ok is False
    assert resp.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_handler_does_not_gate_on_mode_marker():
    """The reset control RPC carries only {team_name, session_id} (no `team`/
    `mode` marker); team-scoping is the relay gate's responsibility, so the
    handler must NOT reject minimal params even when a non-team `mode` is
    present. Guards against the is_team_params regression."""
    params = {"team_name": "oc_team_x", "session_id": "sess_1", "mode": "office"}
    request = _make_request(params)

    captured = await _call_handler(request)

    resp = captured["resp"]
    assert resp.ok is True, "reset must not be rejected for lacking a team-mode marker"
    captured["runner_reset"].assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_runner_failure_returns_ok_false_not_raises():
    """A Runner.reset_agent_team_session failure must degrade to ok=False
    without raising (so the chat.send path stays usable)."""
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers

    params = {"team": "oc_team_x", "team_name": "oc_team_x", "session_id": "sess_1"}
    request = _make_request(params)
    captured, fake_encode = _capture_response()
    fake_tm = MagicMock()
    fake_tm.stop_session_runtime = AsyncMock(return_value=True)
    fake_tm.clear_session_initialized = MagicMock()

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"channel_id": "officeclaw"},
    ), patch(
        "jiuwenswarm.server.runtime.session.session_metadata."
        "resolve_session_runtime_team_name",
        return_value="oc_team_x__sess_1",
    ), patch(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        return_value=fake_tm,
    ), patch(
        "openjiuwen.core.runner.Runner.reset_agent_team_session",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ), patch.object(
        team_handlers, "_sessions_dir_for_request", return_value="/tmp/sessions",
    ), patch.object(
        team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode,
    ), patch.object(
        mod, "send_wire_payload", new=AsyncMock(),
    ):
        # Must not raise.
        await team_handlers.handle_team_session_reset(_ctx_for_reset(mod, request))

    resp = captured["resp"]
    assert resp.ok is False
    assert resp.payload["reset"] is False
    # Shell state was still cleared (best-effort) before the Runner failure.
    fake_tm.clear_session_initialized.assert_called_once_with("sess_1")
