# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the strategy-level auth/credential loop breaker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.common.rails.execution_guard.stalled_access_circuit_rail import (
    StalledAccessCircuitRail,
)


def _ctx(tool_result, tool_args=None, tool_name="bash"):
    return SimpleNamespace(
        session=SimpleNamespace(get_session_id=lambda: "session-1"),
        inputs=SimpleNamespace(
            tool_name=tool_name,
            tool_args=tool_args or {},
            tool_result=tool_result,
        ),
        request_force_finish=MagicMock(),
    )


@pytest.mark.asyncio
async def test_trips_after_repeated_auth_failures() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=3, probe_threshold=99)
    ctx = _ctx({"content": "HTTP 401 AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "error"
    assert "重新登录" in payload["output"]


@pytest.mark.asyncio
async def test_trips_after_repeated_credential_probes() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=2)
    ctx = _ctx(
        {"content": "wrote copy_cookies_share.py"},
        {"content": "import ctypes; crypt32.CryptUnprotectData(data)"},
    )
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "error"
    assert "凭证" in payload["output"]


@pytest.mark.asyncio
async def test_single_auth_failure_does_not_trip() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=3, probe_threshold=99)
    ctx = _ctx({"content": "401 AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_normal_tool_results_do_not_count_as_probes() -> None:
    # Both tool_result and tool_args participate in the scan; "ok" contains
    # no credential fingerprint and therefore must not add to the count.
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=3)
    ctx = _ctx({"content": "ok"}, {})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    ctx.inputs.tool_args = {"content": "CryptUnprotectData"}
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    ctx.inputs.tool_args = {}
    ctx.inputs.tool_result = {"content": "ok"}
    await rail.after_tool_call(ctx)
    ctx.inputs.tool_args = {"content": "Network/Cookies"}
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    ctx.inputs.tool_args = {"content": "Login Data"}
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_called_once()


@pytest.mark.asyncio
async def test_non_relevant_tool_does_not_trip() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=3, probe_threshold=99)
    ctx = _ctx({"content": "HTTP 401 AUTH_SESSION_TERMINATED"}, tool_name="web_search")
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_generic_authentication_required_is_not_a_strong_fingerprint() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=2, probe_threshold=99)
    ctx = _ctx({"content": "Authentication required"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_set_language_switches_to_english() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    ctx = _ctx({"content": "AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)
    rail.set_language("en")

    await rail.after_tool_call(ctx)

    payload = ctx.request_force_finish.call_args[0][0]
    assert "sign in" in payload["output"]


@pytest.mark.asyncio
async def test_scans_tail_of_large_tool_result() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    ctx = _ctx({"content": "x" * (128 * 1024) + " AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


@pytest.mark.asyncio
async def test_exec_tool_participates() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    ctx = _ctx({"content": "AUTH_SESSION_TERMINATED"}, tool_name="exec_command")
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


class _ToolOutputLike:
    success = True
    error = None

    def __init__(self, data: str) -> None:
        self.data = data

    def __repr__(self) -> str:
        raise AssertionError("repr must not be called for object tool output")


@pytest.mark.asyncio
async def test_object_tool_output_uses_fields_not_repr() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    payload = "x" * (4 * 1024 * 1024) + " AUTH_SESSION_TERMINATED"
    ctx = _ctx(_ToolOutputLike(payload))
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


@pytest.mark.asyncio
async def test_tool_call_arguments_fallback() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    ctx = SimpleNamespace(
        session=SimpleNamespace(get_session_id=lambda: "session-1"),
        inputs=SimpleNamespace(
            tool_name="bash",
            tool_args=None,
            tool_result={"content": "ok"},
            tool_call=SimpleNamespace(arguments={"content": "CryptUnprotectData"}),
        ),
        request_force_finish=MagicMock(),
    )
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()
