"""Unit coverage for the host-owned Summary Team launcher."""

from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.paths import organization_workspace_dir

from jiuwenswarm.agents.harness.team.summary_org.launcher import (
    JiuwenSummaryTeamLauncher,
)
from jiuwenswarm.agents.harness.team.summary_org.spec import build_summary_team_spec


@pytest.mark.parametrize("budget", [None, 0, -1, True, 37])
def test_summary_agent_uses_a_finite_configured_budget(budget):
    from jiuwenswarm.agents.harness.team.summary_org.spec import _summary_agent_spec

    source = SimpleNamespace(model=None, language="cn", max_iterations=budget)
    spec = _summary_agent_spec(source, system_prompt="summary")
    assert spec.max_iterations == (37 if budget == 37 else 200)


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


@pytest.mark.asyncio
async def test_summary_team_launch_rollback_reuses_resolved_runtime(monkeypatch):
    """Rollback must stop with launch's runtime without resolving it again."""
    database = object()
    donor_backend = SimpleNamespace(db=database)

    class FakePool:
        """Provide the root Team backend used as the shared-database donor."""

        async def get(self, team_id: str):
            """Return the donor entry only for the claimed root Team."""
            if team_id == "root-team":
                return SimpleNamespace(
                    agent=SimpleNamespace(team_backend=donor_backend)
                )
            return None

    class FakeRuntime:
        """Fail activation and record the rollback stop call."""

        def __init__(self) -> None:
            self.pool = FakePool()
            self.stop_calls: list[dict[str, str]] = []

        async def activate(self, spec, session_id: str):
            """Simulate a launch failure after the runtime has been resolved."""
            raise RuntimeError("activation failed")

        async def stop_team(self, **kwargs) -> None:
            """Record best-effort cleanup of the partially launched Team."""
            self.stop_calls.append(kwargs)

    runtime = FakeRuntime()
    launcher = JiuwenSummaryTeamLauncher()
    runtime_lookups = 0

    def get_runtime():
        """Return the launch runtime and make duplicate resolution observable."""
        nonlocal runtime_lookups
        runtime_lookups += 1
        return runtime

    monkeypatch.setattr(launcher, "_get_runtime", get_runtime)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.summary_org.spec.build_summary_team_spec",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.summary_org.launcher._align_spec_storage",
        lambda spec, db: None,
    )

    with pytest.raises(RuntimeError, match="activation failed"):
        await launcher.launch(
            organization_id="org-1",
            session_id="session-1",
            share_db_from_team_id="root-team",
        )

    assert runtime_lookups == 1
    assert runtime.stop_calls == [
        {"team_name": "org-summary-org-1", "session_id": "session-1"}
    ]


def _summary_prompts(monkeypatch):
    """Build the fixed Summary Team and return its three role prompts."""
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
    members = {member.member_name: member.prompt for member in spec.predefined_members}
    prompts = {
        "leader": spec.agents["leader"].system_prompt,
        "source-integrator": spec.agents["source-integrator"].system_prompt,
        "delivery-drafter": spec.agents["delivery-drafter"].system_prompt,
    }
    assert spec.leader.prompt == prompts["leader"]
    assert members["source-integrator"] == prompts["source-integrator"]
    assert members["delivery-drafter"] == prompts["delivery-drafter"]
    return prompts


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
    assert spec.agents["leader"].enable_sys_operation is True
    assert spec.agents["leader"].workspace is not None
    assert spec.workspace is not None
    assert spec.workspace.enabled is True
    assert spec.workspace.artifact_dirs == ["sources", "outlines", "drafts", "reviews"]
    assert spec.agents["source-integrator"].enable_sys_operation is True
    assert spec.agents["delivery-drafter"].enable_sys_operation is True
    assert {member.member_name for member in spec.predefined_members} == {
        "source-integrator",
        "delivery-drafter",
    }
    assert "source-integrator" in spec.agents
    assert "delivery-drafter" in spec.agents
    assert "source attribution" in spec.agents["source-integrator"].system_prompt
    assert "user-facing draft" in spec.agents["delivery-drafter"].system_prompt
    assert "verify the returned draft" in spec.agents["leader"].system_prompt
    assert "internal Team message" in spec.agents["leader"].system_prompt
    assert "Summary Team workspace" in spec.agents["leader"].system_prompt
    summary_dir = str(organization_workspace_dir("org-1", "session-1") / "summary")
    leader_prompt = spec.agents["leader"].system_prompt
    integrator_prompt = spec.agents["source-integrator"].system_prompt
    drafter_prompt = spec.agents["delivery-drafter"].system_prompt
    assert summary_dir in leader_prompt
    assert summary_dir in drafter_prompt
    assert "Do not send that short text to source-integrator or delivery-drafter" in leader_prompt
    assert "send the complete outline back" in integrator_prompt
    assert "Return the complete draft" in drafter_prompt
    assert "root_task and summary_task" in leader_prompt
    assert "facts must come only from bound sources" in leader_prompt
    assert "stop after successful submission" in leader_prompt
    assert "correct the reported fields and retry without restarting drafting" in leader_prompt
    assert "disagreements that would change the conclusion" in integrator_prompt
    assert "do not call claim_task" in integrator_prompt
    assert "do not call claim_task" in drafter_prompt
    assert "use that text" in integrator_prompt
    assert "use that text" in drafter_prompt
    assert "Do not automatically produce a long report or redo specialist work" in drafter_prompt
    assert "copy it unchanged" in drafter_prompt
    assert "Set result_type to the actual delivered format" in drafter_prompt


def test_summary_leader_writes_short_text_without_the_two_teammates(monkeypatch):
    """A single short text stays with the leader; teammates are only for a complex deliverable."""
    prompts = _summary_prompts(monkeypatch)
    leader = prompts["leader"]

    assert "single short text deliverable" in leader
    assert "check it once against the request" in leader
    assert "Do not send that short text to source-integrator or delivery-drafter" in leader
    assert "only when the bound sources contain a disagreement" in leader
    assert "or the user asked for a file, code, or a multi-part specialist deliverable" in leader


def test_summary_prompts_read_a_cited_source_file_only(monkeypatch):
    """Bound source text is the default; a file is opened only when that source cites it."""
    prompts = _summary_prompts(monkeypatch)

    for prompt in prompts.values():
        assert "use that text" in prompt
        assert "explicitly cites it" in prompt
        assert "Do not list directories or open files from earlier runs" in prompt


def test_summary_outline_does_not_expand_unrequested_work(monkeypatch):
    """The outline keeps structure, attribution, and conclusion-changing disagreements."""
    prompts = _summary_prompts(monkeypatch)
    outline = (
        "structure the draft needs",
        "source attribution",
        "disagreements that would change the conclusion",
        "Do not add research, risk lists, or verification items the user did not request",
    )

    for name in ("leader", "source-integrator"):
        for phrase in outline:
            assert phrase in prompts[name], name
    assert (
        "Do not add research, risk lists, or verification items the user did not request"
        in prompts["delivery-drafter"]
    )


def test_summary_prompts_use_one_absolute_summary_directory(monkeypatch):
    """Writing a final file and result_uri share the framework summary directory."""
    prompts = _summary_prompts(monkeypatch)
    summary_dir = str(organization_workspace_dir("org-1", "session-1") / "summary")

    assert summary_dir.startswith("/")
    assert summary_dir in prompts["leader"]
    assert summary_dir in prompts["delivery-drafter"]
    assert summary_dir not in prompts["source-integrator"]
    assert "pass that same absolute path as result_uri" in prompts["leader"]
    assert "omit result_uri" in prompts["leader"]
    assert "report that same absolute path" in prompts["delivery-drafter"]
    assert "do not invent a path" in prompts["delivery-drafter"]


def test_summary_messages_do_not_require_claim_task(monkeypatch):
    """A message assignment is complete unless view_task shows an internal task for that member."""
    prompts = _summary_prompts(monkeypatch)

    assert "Do not also create an internal task for work assigned by message" in prompts["leader"]
    for name in ("source-integrator", "delivery-drafter"):
        prompt = prompts[name]
        assert "overrides the autonomous claim loop" in prompt
        assert "assigns this work by message" in prompt
        assert "view_task shows an internal task assigned to you" in prompt
        assert "do not call claim_task" in prompt
        assert "claim_task(status=completed)" not in prompt
        assert "org_claim_task" not in prompt
