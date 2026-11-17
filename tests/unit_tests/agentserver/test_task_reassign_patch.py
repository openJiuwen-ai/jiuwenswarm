# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""pending/blocked reassignment stays on the task and does not use the claim pool."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.team import task_reassign_patch as patch


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Session:
    def __init__(self, calls: list, rowcount: int) -> None:
        self._calls = calls
        self._rowcount = rowcount

    async def execute(self, statement):
        self._calls.append(statement)
        return _Result(self._rowcount)

    async def commit(self) -> None:
        self._calls.append("commit")


class _Write:
    def __init__(self, calls: list, rowcount: int) -> None:
        self._calls = calls
        self._rowcount = rowcount

    async def __aenter__(self):
        return _Session(self._calls, self._rowcount)

    async def __aexit__(self, *_args) -> None:
        return None


class _Dao:
    def __init__(self, calls: list, rowcount: int = 1) -> None:
        self._calls = calls
        self._sessions = type("S", (), {"write": lambda _self: _Write(calls, rowcount)})()


@pytest.mark.asyncio
async def test_pending_blocked_reassign_keeps_status_when_in_progress_misses():
    from openjiuwen.agent_teams.tools.database.task_dao import TaskDao

    calls: list = []
    saved = TaskDao.reassign_task
    saved_flag = patch._PATCHED

    async def original(_self, task_id, from_assignee, to_assignee):
        calls.append(("original", task_id, from_assignee, to_assignee))
        return False

    try:
        patch._PATCHED = False
        TaskDao.reassign_task = original  # type: ignore[method-assign]
        patch.apply_task_reassign_patch()
        ok = await TaskDao.reassign_task(_Dao(calls), "task-1", "alice", "bob")
    finally:
        TaskDao.reassign_task = saved  # type: ignore[method-assign]
        patch._PATCHED = saved_flag

    assert ok is True
    assert calls[0] == ("original", "task-1", "alice", "bob")
    flat: list = []
    for value in calls[1].compile().params.values():
        flat.extend(value if isinstance(value, (list, tuple)) else [value])
    assert "pending" in flat
    assert "blocked" in flat
    assert calls[-1] == "commit"


@pytest.mark.asyncio
async def test_in_progress_reassign_does_not_touch_resting_rows():
    from openjiuwen.agent_teams.tools.database.task_dao import TaskDao

    calls: list = []
    saved = TaskDao.reassign_task
    saved_flag = patch._PATCHED

    async def original(_self, task_id, from_assignee, to_assignee):
        calls.append("original")
        return True

    try:
        patch._PATCHED = False
        TaskDao.reassign_task = original  # type: ignore[method-assign]
        patch.apply_task_reassign_patch()
        ok = await TaskDao.reassign_task(_Dao(calls), "task-1", "alice", "bob")
    finally:
        TaskDao.reassign_task = saved  # type: ignore[method-assign]
        patch._PATCHED = saved_flag

    assert ok is True
    assert calls == ["original"]
