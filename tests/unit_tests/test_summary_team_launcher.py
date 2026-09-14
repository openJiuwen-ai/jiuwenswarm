"""Unit coverage for the host-owned Summary Team launcher."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.summary_org.launcher import (
    JiuwenSummaryTeamLauncher,
)
from jiuwenswarm.agents.harness.team.summary_org.spec import build_summary_team_spec


@pytest.mark.asyncio
async def test_summary_team_launcher_pauses_after_build(monkeypatch):
    """A newly created Summary Team must be paused before organization scheduling begins."""
    database = object()

    class FakePool:
        """Return the root-team database donor and no existing Summary Team."""

        async def get(self, team_id: str):
            """Resolve the requested runtime entry."""

            if team_id == "root-team":
                return SimpleNamespace(
                    agent=SimpleNamespace(team_backend=donor_backend)
                )
            return None

    class FakeBackend:
        """Provide the database-bearing backend required by the launcher."""

        def __init__(self) -> None:
            self.db = database
            self.task_manager = SimpleNamespace(db=database)
            self.message_manager = SimpleNamespace(db=database)

        async def build_team(self, **kwargs) -> None:
            """Simulate successful Summary Team construction."""

    donor_backend = FakeBackend()
    summary_backend = FakeBackend()
    summary_agent = SimpleNamespace(
        team_backend=summary_backend, member_name="summary-leader"
    )

    class FakeRuntime:
        """Capture activation and the required transition to paused state."""

        def __init__(self) -> None:
            self.pool = FakePool()
            self.pause_calls: list[dict[str, str]] = []

        async def activate(self, spec, session_id: str):
            """Return the freshly activated Summary Team agent."""

            return SimpleNamespace(agent=summary_agent)

        async def pause(self, **kwargs) -> bool:
            """Record the scheduling-ready transition."""

            self.pause_calls.append(kwargs)
            return True

    runtime = FakeRuntime()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.summary_org.spec.build_summary_team_spec",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.summary_org.launcher._align_spec_storage",
        lambda spec, db: None,
    )

    result = await JiuwenSummaryTeamLauncher(runtime_manager=runtime).launch(
        organization_id="org-1",
        session_id="session-1",
        share_db_from_team_id="root-team",
    )

    assert result.team_id == "org-summary-org-1"
    assert runtime.pause_calls == [
        {"team_name": "org-summary-org-1", "session_id": "session-1"}
    ]


def test_summary_team_spec_uses_inprocess_transport(monkeypatch):
    """The fixed internal teammates need a live in-process message transport."""
    configured = SimpleNamespace(
        agents={
            "leader": SimpleNamespace(model=None, language="zh"),
            "teammate": SimpleNamespace(model=None, language="zh"),
        },
        model_pool=[],
        model_router=None,
        model_intelli_router=None,
        model_pool_strategy="round_robin",
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.TeamManager._load_team_spec",
        lambda session_id: configured,
    )

    spec = build_summary_team_spec(
        team_id="org-summary-org-1",
        organization_id="org-1",
        session_id="session-1",
    )

    assert spec.spawn_mode == "inprocess"
    assert spec.transport is not None
    assert spec.transport.type == "inprocess"
