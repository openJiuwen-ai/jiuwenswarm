from __future__ import annotations

from typing import Any, Awaitable, Callable

import pytest

from jiuwenswarm.agents.harness.design.rails.designer_execution_rail import (
    REPORT_ONLY_REFUSAL,
    DesignerExecutionRail,
)


class _FakeSpawner:
    def __init__(self, run_status: str) -> None:
        self.report_pending = False
        self.calls: list[tuple[list[str] | None, bool]] = []
        self._run_status = run_status

    async def run_graph(self, *, node_ids: list[str] | None = None, cascade: bool = False) -> dict[str, Any]:
        self.calls.append((node_ids, cascade))
        return {
            "run_id": "run_1",
            "status": self._run_status,
            "node_states": {"n_clip": {"status": self._run_status, "error": "boom"}},
        }


class _FakeAsyncToolRuntime:
    def __init__(self) -> None:
        self.launched: list[Callable[[], Awaitable[Any]]] = []

    def launch(self, task_id: str, coro_factory: Callable[[], Awaitable[Any]], *, tool_name: str, description: str) -> None:
        self.launched.append(coro_factory)


class _FakeLeader:
    def __init__(self) -> None:
        self.async_tool_runtime = _FakeAsyncToolRuntime()


def _rail(spawner: _FakeSpawner, leader: _FakeLeader) -> DesignerExecutionRail:
    rail = DesignerExecutionRail(session_id="design_session")
    rail._spawner = spawner
    rail._agent = leader
    return rail


@pytest.mark.asyncio
async def test_finished_run_reports_once_and_refuses_reruns_until_the_user_replies():
    spawner = _FakeSpawner("failed")
    leader = _FakeLeader()
    tool = _rail(spawner, leader)._build_tool()

    await tool.invoke({})
    summary = await leader.async_tool_runtime.launched[0]()

    assert "只如实" in summary
    assert '"status": "failed"' in summary
    assert spawner.report_pending is True

    assert await tool.invoke({}) == REPORT_ONLY_REFUSAL
    assert len(leader.async_tool_runtime.launched) == 1
    assert spawner.calls == [(None, False)]

    spawner.report_pending = False
    await tool.invoke({"node_id": "n_clip"})
    assert len(leader.async_tool_runtime.launched) == 2
