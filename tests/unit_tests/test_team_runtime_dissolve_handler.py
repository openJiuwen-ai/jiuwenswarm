# tests/unit_tests/test_team_runtime_dissolve_handler.py
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for ``handle_team_runtime_dissolve`` (mirrors reset handler test)."""
from __future__ import annotations

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


def _ctx_for(mod, request):
    ctx = MagicMock()
    ctx.request = request

    async def _send_wire(wire):
        return await mod.send_wire_payload(None, wire)

    ctx.sink.send_wire = _send_wire
    return ctx


def _capture():
    captured: dict = {}

    def fake_encode(resp, response_id=None):  # noqa: ANN001
        captured["resp"] = resp
        return b"wire"

    return captured, fake_encode


async def _call_dissolve(
    request, *, resolved_name: str | None = "oc_team_x__sess_1", clear_raises=None
) -> dict:
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers

    captured, fake_encode = _capture()
    fake_tm = MagicMock()
    fake_tm.stop_session_runtime = AsyncMock(return_value=True)
    fake_tm.clear_session_initialized = MagicMock(side_effect=clear_raises)

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"channel_id": "officeclaw", "team_name": "oc_team_x"},
    ), patch(
        "jiuwenswarm.server.runtime.session.session_metadata."
        "resolve_session_runtime_team_name",
        return_value=resolved_name,
    ), patch(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        return_value=fake_tm,
    ), patch(
        "openjiuwen.core.runner.Runner.reset_agent_team_session",
        new=AsyncMock(return_value=True),
    ) as runner_reset, patch.object(
        team_handlers, "_sessions_dir_for_request", return_value="/tmp/sessions",
    ), patch.object(
        team_handlers, "_effective_config_for_request", return_value={},
    ), patch.object(
        team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode,
    ), patch.object(
        mod, "send_wire_payload", new=AsyncMock(),
    ):
        await team_handlers.handle_team_runtime_dissolve(_ctx_for(mod, request))
        captured["fake_tm"] = fake_tm
        captured["runner_reset"] = runner_reset
    return captured


@pytest.mark.asyncio
async def test_dissolve_calls_runner_reset_with_resolved_name():
    params = {"team_name": "oc_team_preset-software-dev", "session_id": "sess_1"}
    captured = await _call_dissolve(_make_request(params))

    assert captured["runner_reset"].await_count == 1
    _, kwargs = captured["runner_reset"].call_args
    assert kwargs["team_name"] == "oc_team_x__sess_1"
    assert kwargs["session_id"] == "sess_1"
    assert kwargs["force"] is True

    tm = captured["fake_tm"]
    tm.stop_session_runtime.assert_awaited_once()
    sargs, skwargs = tm.stop_session_runtime.call_args
    assert sargs[0] == "sess_1"
    assert skwargs["reason"] == "team.runtime.dissolve"
    assert skwargs["stop_runner"] is False
    tm.clear_session_initialized.assert_called_once_with("sess_1")

    resp = captured["resp"]
    assert resp.ok is True
    assert resp.payload["dissolved"] is True
    assert resp.payload["session_id"] == "sess_1"
    # 回显解析后的权威运行时名，而非请求参数里的 team_name。
    assert resp.payload["team_name"] == "oc_team_x__sess_1"


@pytest.mark.asyncio
async def test_dissolve_continues_when_clear_initialized_fails():
    params = {"team_name": "oc_team_x", "session_id": "sess_1"}
    captured = await _call_dissolve(
        _make_request(params), clear_raises=RuntimeError("clear failed")
    )

    captured["runner_reset"].assert_awaited_once()
    captured["fake_tm"].clear_session_initialized.assert_called_once_with("sess_1")
    assert captured["resp"].ok is True
    assert captured["resp"].payload["dissolved"] is True


@pytest.mark.asyncio
async def test_dissolve_missing_session_id_is_bad_request():
    captured = await _call_dissolve(_make_request({"team_name": "oc_team_x"}))
    resp = captured["resp"]
    assert resp.ok is False
    assert resp.payload["code"] == "BAD_REQUEST"
    captured["runner_reset"].assert_not_called()


@pytest.mark.asyncio
async def test_dissolve_runner_failure_degrades_ok_false():
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers

    captured, fake_encode = _capture()
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
    ), patch(
        "openjiuwen.core.runner.Runner.reset_agent_team_session",
        new=AsyncMock(return_value=False),
    ), patch.object(
        team_handlers, "_sessions_dir_for_request", return_value="/tmp/sessions",
    ), patch.object(
        team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode,
    ), patch.object(
        mod, "send_wire_payload", new=AsyncMock(),
    ):
        await team_handlers.handle_team_runtime_dissolve(
            _ctx_for(mod, _make_request({"session_id": "sess_1"}))
        )

    assert captured["resp"].ok is False
    assert captured["resp"].payload["dissolved"] is False


@pytest.mark.asyncio
async def test_dissolve_team_name_optional():
    # relay 发 dissolve 帧时 team_name 可选（仅 session_id 恒带）。
    captured = await _call_dissolve(_make_request({"session_id": "sess_1"}))
    assert captured["runner_reset"].await_count == 1
    assert captured["resp"].ok is True


@pytest.mark.asyncio
async def test_dissolve_no_bound_team_and_no_param_is_ok_noop():
    # 会话无绑定运行时团队且请求未带 team_name：无可 dissolve，ok=True
    # 短路（与 relay resetTeamSession 接受 NOT_FOUND 的语义对齐），
    # 不触发任何 stop/reset。
    captured = await _call_dissolve(
        _make_request({"session_id": "sess_1"}), resolved_name=None
    )
    assert captured["resp"].ok is True
    assert captured["resp"].payload["dissolved"] is True
    assert captured["resp"].payload["team_name"] == ""
    captured["runner_reset"].assert_not_called()
    captured["fake_tm"].stop_session_runtime.assert_not_called()
    captured["fake_tm"].clear_session_initialized.assert_not_called()


@pytest.mark.asyncio
async def test_dissolve_registered_in_dispatch():
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.dispatch import HANDLERS

    spec = HANDLERS.get(ReqMethod.TEAM_RUNTIME_DISSOLVE)
    assert spec is not None
    assert spec.fn.__name__ == "handle_team_runtime_dissolve"


# ---- dissolve + roster prune ----------------------------------------------


async def _call_dissolve_prune(
    request,
    *,
    keep=None,
    prune_return=None,
    prune_raises=None,
    reset_return=True,
    resolved_name: str | None = "oc_team_x__sess_1",
) -> dict:
    """Drive dissolve with ``resolve_dissolve_keep_members`` and the Runner
    reset/prune pair mocked, so prune wiring can be asserted in isolation."""
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers
    from openjiuwen.core.runner import Runner

    captured, fake_encode = _capture()
    fake_tm = MagicMock()
    fake_tm.stop_session_runtime = AsyncMock(return_value=True)
    fake_tm.clear_session_initialized = MagicMock()

    runner_reset = AsyncMock(return_value=reset_return)
    runner_prune = AsyncMock(return_value=prune_return)
    if prune_raises is not None:
        runner_prune.side_effect = prune_raises

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"channel_id": "officeclaw", "team_name": "oc_team_x"},
    ), patch(
        "jiuwenswarm.server.runtime.session.session_metadata."
        "resolve_session_runtime_team_name",
        return_value=resolved_name,
    ), patch(
        "jiuwenswarm.agents.harness.team.get_team_manager", return_value=fake_tm,
    ), patch(
        "openjiuwen.core.runner.Runner.reset_agent_team_session", new=runner_reset,
    ), patch.object(
        Runner, "prune_agent_team_roster", new=runner_prune, create=True,
    ), patch.object(
        team_handlers, "_sessions_dir_for_request", return_value="/tmp/sessions",
    ), patch.object(
        team_handlers, "_effective_config_for_request", return_value={},
    ), patch(
        "jiuwenswarm.server.runtime.team_snapshot_refresh."
        "resolve_dissolve_keep_members",
        return_value=keep,
    ), patch.object(
        team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode,
    ), patch.object(
        mod, "send_wire_payload", new=AsyncMock(),
    ):
        await team_handlers.handle_team_runtime_dissolve(_ctx_for(mod, request))
        captured["runner_reset"] = runner_reset
        captured["runner_prune"] = runner_prune
        captured["fake_tm"] = fake_tm
    return captured


@pytest.mark.asyncio
async def test_dissolve_prunes_roster_when_keep_set_resolved():
    request = _make_request({"session_id": "sess_1", "team_name": "oc_team_x"})
    captured = await _call_dissolve_prune(
        request, keep={"leader", "m1"}, prune_return=["m2"],
    )
    assert captured["runner_reset"].await_count == 1
    assert captured["runner_prune"].await_count == 1
    _, pkwargs = captured["runner_prune"].call_args
    assert pkwargs["team_name"] == "oc_team_x__sess_1"
    assert pkwargs["session_id"] == "sess_1"
    assert pkwargs["keep_members"] == {"leader", "m1"}
    resp = captured["resp"]
    assert resp.ok is True
    assert resp.payload["dissolved"] is True


@pytest.mark.asyncio
async def test_dissolve_prune_failure_degrades_ok_false():
    # prune raises → ok=false so relay keeps the dirty flag and retries.
    request = _make_request({"session_id": "sess_1"})
    captured = await _call_dissolve_prune(
        request, keep={"leader"}, prune_raises=RuntimeError("db down"),
    )
    assert captured["runner_prune"].await_count == 1
    resp = captured["resp"]
    assert resp.ok is False
    assert resp.payload["dissolved"] is False


@pytest.mark.asyncio
async def test_dissolve_skips_prune_when_keep_set_none():
    # keep=None (template unavailable) → skip prune, ok stays True (fail-open).
    request = _make_request({"session_id": "sess_1"})
    captured = await _call_dissolve_prune(request, keep=None, prune_return=[])
    assert captured["runner_prune"].await_count == 0
    assert captured["resp"].ok is True


@pytest.mark.asyncio
async def test_dissolve_skips_prune_when_agent_core_too_old():
    # agent-core predates prune_agent_team_roster: dissolve must not crash and
    # must leave ok at reset's True. ok=True proves the hasattr guard fired
    # (without the guard the missing method would raise → ok=False).
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.handlers import team as team_handlers

    class _OldRunner:
        @classmethod
        async def reset_agent_team_session(cls, **kwargs):  # noqa: ANN001
            return True

    captured, fake_encode = _capture()
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
        "jiuwenswarm.agents.harness.team.get_team_manager", return_value=fake_tm,
    ), patch("openjiuwen.core.runner.Runner", _OldRunner), patch.object(
        team_handlers, "_sessions_dir_for_request", return_value="/tmp/sessions",
    ), patch.object(
        team_handlers, "_effective_config_for_request", return_value={},
    ), patch(
        "jiuwenswarm.server.runtime.team_snapshot_refresh."
        "resolve_dissolve_keep_members",
        return_value={"leader"},  # keep-set present → prune branch entered
    ), patch.object(
        team_handlers, "encode_agent_response_for_wire", side_effect=fake_encode,
    ), patch.object(
        mod, "send_wire_payload", new=AsyncMock(),
    ):
        await team_handlers.handle_team_runtime_dissolve(
            _ctx_for(mod, _make_request({"session_id": "sess_1"}))
        )
    assert captured["resp"].ok is True
