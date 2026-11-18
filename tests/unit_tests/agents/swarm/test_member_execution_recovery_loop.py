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
    monkeypatch.setattr(recovery, "reconcile_group", AsyncMock())

    reconcile_mock = AsyncMock()
    fake_ge = MagicMock()
    fake_ge.GroupExecutions.return_value.reconcile = reconcile_mock
    monkeypatch.setitem(
        __import__("sys").modules,
        "jiuwenswarm.server.runtime.group_execution",
        fake_ge,
    )

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        # Let the loop finish one full scan, then stop it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop.set()

    asyncio.create_task(_stop_soon())
    await recovery.recovery_loop(stop)

    fake_ge.GroupExecutions.assert_called_once_with(session)
    reconcile_mock.assert_awaited_once()
