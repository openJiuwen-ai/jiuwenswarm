# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""stop_team settles the task board before tearing down the runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team import stop_team_settle_patch as patch


class _TaskManager:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def cancel_all_tasks(self, skip_assignees=None):
        self.calls.append(("cancel_all_tasks", skip_assignees))
        return [SimpleNamespace(task_id="t3-html")]


class _Manager:
    def __init__(self, entry) -> None:
        self._entry = entry
        self.stop_calls: list[tuple] = []

    async def _resolve_entry(self, *, team_name: str, session_id: str):
        return self._entry

    async def stop_team(self, *, team_name: str, session_id: str) -> bool:
        self.stop_calls.append((team_name, session_id))
        return True


@pytest.mark.asyncio
async def test_stop_team_cancels_tasks_before_original_stop(monkeypatch):
    from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

    task_manager = _TaskManager()
    entry = SimpleNamespace(agent=SimpleNamespace(task_manager=task_manager))
    manager = _Manager(entry)
    order: list[str] = []

    async def original(self, *, team_name: str, session_id: str) -> bool:
        order.append("original")
        assert task_manager.calls, "settle must run before original stop_team"
        return await _Manager.stop_team(self, team_name=team_name, session_id=session_id)

    saved = TeamRuntimeManager.stop_team
    saved_flag = patch._PATCHED
    try:
        patch._PATCHED = False
        TeamRuntimeManager.stop_team = original  # type: ignore[method-assign]
        patch.apply_stop_team_settle_patch()
        ok = await TeamRuntimeManager.stop_team(
            manager, team_name="team-a", session_id="sess-1"
        )
    finally:
        TeamRuntimeManager.stop_team = saved  # type: ignore[method-assign]
        patch._PATCHED = saved_flag

    assert ok is True
    assert task_manager.calls == [("cancel_all_tasks", None)]
    assert order == ["original"]
    assert manager.stop_calls == [("team-a", "sess-1")]


@pytest.mark.asyncio
async def test_stop_team_continues_when_settle_raises(monkeypatch):
    from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

    class _Boom:
        async def cancel_all_tasks(self, skip_assignees=None):
            raise RuntimeError("db down")

    entry = SimpleNamespace(agent=SimpleNamespace(task_manager=_Boom()))
    manager = _Manager(entry)
    stopped: list[bool] = []

    async def original(self, *, team_name: str, session_id: str) -> bool:
        stopped.append(True)
        return True

    saved = TeamRuntimeManager.stop_team
    saved_flag = patch._PATCHED
    try:
        patch._PATCHED = False
        TeamRuntimeManager.stop_team = original  # type: ignore[method-assign]
        patch.apply_stop_team_settle_patch()
        ok = await TeamRuntimeManager.stop_team(
            manager, team_name="team-a", session_id="sess-1"
        )
    finally:
        TeamRuntimeManager.stop_team = saved  # type: ignore[method-assign]
        patch._PATCHED = saved_flag

    assert ok is True
    assert stopped == [True]


@pytest.mark.asyncio
async def test_apply_stop_team_settle_patch_is_idempotent():
    from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager

    saved = TeamRuntimeManager.stop_team
    saved_flag = patch._PATCHED
    try:
        patch._PATCHED = False
        patch.apply_stop_team_settle_patch()
        first = TeamRuntimeManager.stop_team
        patch.apply_stop_team_settle_patch()
        assert TeamRuntimeManager.stop_team is first
    finally:
        TeamRuntimeManager.stop_team = saved  # type: ignore[method-assign]
        patch._PATCHED = saved_flag
