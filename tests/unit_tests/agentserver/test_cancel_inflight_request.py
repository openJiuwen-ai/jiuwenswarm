# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for connection-scoped inflight cancel with round ownership check."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter with internal state set via setattr."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


def _instance_with_active_round(request_id: str | None) -> MagicMock:
    instance = MagicMock()
    if request_id is None:
        instance.active_round = None
    else:
        instance.active_round = SimpleNamespace(work=SimpleNamespace(request_id=request_id))
    instance.cancel_round = AsyncMock(return_value=True)
    return instance


@pytest.mark.asyncio
async def test_cancels_round_owned_by_the_closing_request() -> None:
    """The active round belongs to the closing request → cancel it."""
    instance = _instance_with_active_round("req-owner")
    adapter = _make_adapter(_instance=instance)
    adapter._cancel_scheduler_running_tasks = MagicMock()  # pylint: disable=protected-access

    cancelled = await adapter.cancel_inflight_request(
        "sess-a", "req-owner", reason="[owner ws closed]"
    )

    assert cancelled is True
    instance.cancel_round.assert_awaited_once_with(reason="[owner ws closed]")
    adapter._cancel_scheduler_running_tasks.assert_called_once()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_skips_round_owned_by_another_request() -> None:
    """Same session, another connection's round is active → must NOT touch it."""
    instance = _instance_with_active_round("req-other-connection")
    adapter = _make_adapter(_instance=instance)
    adapter._cancel_scheduler_running_tasks = MagicMock()  # pylint: disable=protected-access

    cancelled = await adapter.cancel_inflight_request("sess-a", "req-closing")

    assert cancelled is False
    instance.cancel_round.assert_not_awaited()
    adapter._cancel_scheduler_running_tasks.assert_not_called()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_skips_when_round_already_finished() -> None:
    """Closing after normal completion: no active round → no-op."""
    instance = _instance_with_active_round(None)
    adapter = _make_adapter(_instance=instance)
    adapter._cancel_scheduler_running_tasks = MagicMock()  # pylint: disable=protected-access

    cancelled = await adapter.cancel_inflight_request("sess-a", "req-finished")

    assert cancelled is False
    instance.cancel_round.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_instance_missing() -> None:
    adapter = _make_adapter(_instance=None)

    cancelled = await adapter.cancel_inflight_request("sess-a", "req-x")

    assert cancelled is False


@pytest.mark.asyncio
async def test_dispatches_to_cached_session_adapter_on_root() -> None:
    """Root adapter delegates to the session-scoped adapter for the session."""
    cached = MagicMock()
    cached.cancel_inflight_request = AsyncMock(return_value=True)
    adapter = _make_adapter()
    adapter._is_session_scoped_adapter = False  # pylint: disable=protected-access
    adapter._get_cached_session_adapter = lambda session_id: (  # pylint: disable=protected-access
        cached if session_id == "sess-a" else None
    )

    cancelled = await adapter.cancel_inflight_request("sess-a", "req-owner")

    assert cancelled is True
    cached.cancel_inflight_request.assert_awaited_once_with(
        "sess-a", "req-owner", adapter.cancel_inflight_request.__defaults__[0]
    )


@pytest.mark.asyncio
async def test_returns_false_without_cached_session_adapter() -> None:
    """No session-scoped adapter = no inflight stream for the session → no-op."""
    adapter = _make_adapter()
    adapter._is_session_scoped_adapter = False  # pylint: disable=protected-access
    adapter._get_cached_session_adapter = lambda session_id: None  # pylint: disable=protected-access

    cancelled = await adapter.cancel_inflight_request("sess-missing", "req-x")

    assert cancelled is False
