# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The worker recovery loop must also reconcile ordinary member execution cards.

After a worker is killed and restarts, non-terminal execution rows would otherwise
stay spinning in the UI and could not be stopped (#6795).  The startup/periodic
scan now calls ``GroupExecutions(session).reconcile()`` in addition to the
organization-only reconciliation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_recovery_loop_reconciles_member_executions(monkeypatch, tmp_path: Path) -> None:
    from jiuwenswarm.agents.swarm import group_org_recovery as recovery

    session = "session-6795"
    (tmp_path / session).mkdir()
    (tmp_path / session / "group.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )

    stop = asyncio.Event()

    # Stop the loop once it has actually reached the session, rather than after a
    # fixed number of event-loop ticks.  Upstream runs the directory scan in a
    # worker thread (asyncio.to_thread), so a fixed number of sleep(0) calls is
    # not a reliable synchronization point.
    async def _stop_on_session(*args, **kwargs) -> None:
        stop.set()

    monkeypatch.setattr(recovery, "reconcile_group", AsyncMock(side_effect=_stop_on_session))

    reconcile_mock = AsyncMock()
    fake_ge = MagicMock()
    fake_ge.GroupExecutions.return_value.reconcile = reconcile_mock
    monkeypatch.setitem(
        __import__("sys").modules,
        "jiuwenswarm.server.runtime.group_execution",
        fake_ge,
    )

    await recovery.recovery_loop(stop)

    fake_ge.GroupExecutions.assert_called_once_with(session)
    reconcile_mock.assert_awaited_once()
