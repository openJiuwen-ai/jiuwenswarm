# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for channel-scoped team manager registry behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.agent_teams.runtime.pool import RuntimeState

from jiuwenswarm.common.invocation_context import (
    TRACE_CONTEXT_METADATA_KEY,
    TRACE_HEADER_EXPORTER_METADATA_KEY,
)
from jiuwenswarm.agents.harness.team.team_manager import (
    TeamManager,
    TeamRailMountContext,
    MemberInfo,
    RuntimeInfo,
    TeamWorkspaceInfo,
    get_team_manager,
    refresh_team_shared_skill_links_across_managers,
    reset_team_manager,
)


class _TeamManagerHarness(TeamManager):
    def set_active_runtime_for_test(self, session_id: str, team_name: str) -> None:
        self.commit_runtime_ready(session_id, team_name)

    def set_pending_runtime_for_test(self, session_id: str, team_name: str) -> None:
        getattr(self, "_pending_team_names")[session_id] = team_name

    def cache_local_team_agent_for_test(self, session_id: str, team_agent) -> None:
        getattr(self, "_team_agents")[session_id] = team_agent

    def register_stream_task_for_test(self, session_id: str, task: asyncio.Task) -> None:
        getattr(self, "_stream_tasks")[session_id] = task

    def resolve_session_team_name_for_test(self, session_id: str) -> str | None:
        return self._resolve_session_team_name(session_id)

    def stub_resolve_resumable_runner_entry_for_test(self, resolver) -> None:
        self._resolve_resumable_runner_entry = resolver  # type: ignore[method-assign]

    async def resolve_resumable_runner_entry_for_test(self, session_id: str):
        return await self._resolve_resumable_runner_entry(session_id)

    def get_lifecycle_lock_for_test(self, session_id: str) -> asyncio.Lock:
        return self._get_lifecycle_lock(session_id)


class _FakeRail:
    pass


class _FakeSkillEvolutionRail:
    def __init__(self, signal_trigger: bool = True) -> None:
        self.signal_trigger = signal_trigger


class _FakeTeamSkillEvolutionRail:
    def __init__(self, *, signal_trigger: bool = True, review_trigger: bool = True) -> None:
        self.signal_trigger = signal_trigger
        self.review_trigger = review_trigger
        self._pending_approval_snapshots: dict[str, object] = {}
        self._pending_governance: dict[str, object] = {}

    def add_pending_approval_snapshot(self, request_id: str) -> None:
        self._pending_approval_snapshots[request_id] = object()

    def add_pending_governance(self, request_id: str) -> None:
        self._pending_governance[request_id] = object()


class _FakeTeamSkillCreateRail:
    pass


class _FakeAgent:
    def __init__(self) -> None:
        self.unregistered: list[object] = []
        self.added_rails: list[object] = []

    async def unregister_rail(self, rail: object):
        self.unregistered.append(rail)
        return self

    def add_rail(self, rail: object) -> None:
        self.added_rails.append(rail)


def setup_function() -> None:
    reset_team_manager()


def teardown_function() -> None:
    reset_team_manager()


def test_get_team_manager_is_singleton() -> None:
    # TeamManager is a process-wide singleton shared across channels so that
    # bridged follow-up requests (e.g. a /join member replying from feishu
    # while the originating web stream is still alive) can see the
    # originating channel's runtime markers and avoid being misidentified as
    # a first request.
    web_manager = get_team_manager("web")
    feishu_manager = get_team_manager("feishu")
    web_manager_again = get_team_manager("web")

    assert isinstance(web_manager, TeamManager)
    assert isinstance(feishu_manager, TeamManager)
    assert web_manager is web_manager_again
    assert web_manager is feishu_manager  # singleton: same instance regardless of channel

    reset_team_manager()
    after_reset = get_team_manager("web")
    assert after_reset is not web_manager  # reset yields a fresh instance


@pytest.mark.asyncio
async def test_broadcast_event_applies_backpressure_to_full_waiter_queue() -> None:
    manager = TeamManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    manager.add_waiter("sess-backpressure", "req-1", queue)

    await manager.broadcast_event("sess-backpressure", {"seq": 1})
    blocked_broadcast = asyncio.create_task(
        manager.broadcast_event("sess-backpressure", {"seq": 2})
    )
    await asyncio.sleep(0)

    assert blocked_broadcast.done() is False
    assert await queue.get() == {"seq": 1}
    await asyncio.wait_for(blocked_broadcast, timeout=1.0)
    assert await queue.get() == {"seq": 2}


@pytest.mark.asyncio
async def test_broadcast_event_unblocks_when_full_waiter_is_removed() -> None:
    manager = TeamManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    manager.add_waiter("sess-disconnect", "req-1", queue)
    await queue.put({"seq": 1})

    blocked_broadcast = asyncio.create_task(
        manager.broadcast_event("sess-disconnect", {"seq": 2})
    )
    await asyncio.sleep(0)
    assert blocked_broadcast.done() is False

    manager.remove_waiter("sess-disconnect", "req-1")

    await asyncio.wait_for(blocked_broadcast, timeout=1.0)
    assert manager.has_waiters("sess-disconnect") is False
    assert await queue.get() == {"seq": 1}


@pytest.mark.asyncio
async def test_full_waiter_does_not_block_delivery_to_other_waiters() -> None:
    manager = TeamManager()
    slow_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    fast_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    manager.add_waiter("sess-multi", "req-slow", slow_queue)
    manager.add_waiter("sess-multi", "req-fast", fast_queue)
    await slow_queue.put({"seq": 0})

    broadcast = asyncio.create_task(
        manager.broadcast_event("sess-multi", {"seq": 1})
    )

    assert await asyncio.wait_for(fast_queue.get(), timeout=1.0) == {"seq": 1}
    assert broadcast.done() is False

    manager.remove_waiter("sess-multi", "req-slow")
    await asyncio.wait_for(broadcast, timeout=1.0)


@pytest.mark.asyncio
async def test_update_evolution_config_updates_member_skill_evolution_signal_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    rail = _FakeSkillEvolutionRail(signal_trigger=True)
    manager.register_team_member_skill_evolution_rail("sess-1", rail)

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    await manager.update_evolution_config({"evolution": {"signal_trigger": False}})

    assert rail.signal_trigger is False

    await manager.update_evolution_config({"evolution": {"signal_trigger": True}})

    assert rail.signal_trigger is True


@pytest.mark.asyncio
async def test_update_evolution_config_enabled_false_keeps_team_skill_rail_and_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    rail = _FakeRail()
    agent = _FakeAgent()
    task = asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    manager.register_team_skill_rail("sess-1", rail)
    manager.register_team_live_rail("sess-1", agent, rail)
    manager.register_team_evolution_watcher("sess-1", task)

    await manager.update_evolution_config({"evolution": {"enabled": False, "review_trigger": False}})

    assert manager.get_team_skill_rail("sess-1") is rail
    assert manager.get_team_evolution_watcher("sess-1") is task
    assert agent.unregistered == []
    assert not task.cancelled()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_update_evolution_config_keeps_team_skill_rail_when_review_trigger_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    rail = _FakeTeamSkillEvolutionRail(signal_trigger=True)
    manager.register_team_skill_rail("sess-1", rail)

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    await manager.update_evolution_config({"evolution": {"enabled": True, "review_trigger": False}})

    assert manager.get_team_skill_rail("sess-1") is rail
    assert rail.signal_trigger is True
    assert rail.review_trigger is False


@pytest.mark.asyncio
async def test_update_evolution_config_enabled_false_does_not_override_signal_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    rail = _FakeTeamSkillEvolutionRail(signal_trigger=False)
    manager.register_team_skill_rail("sess-1", rail)

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    await manager.update_evolution_config({"evolution": {"enabled": False, "review_trigger": True}})

    assert manager.get_team_skill_rail("sess-1") is rail
    assert rail.signal_trigger is False
    assert rail.review_trigger is True


@pytest.mark.asyncio
async def test_update_evolution_config_only_updates_existing_rails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    team_rail = _FakeTeamSkillEvolutionRail(
        signal_trigger=False,
        review_trigger=False,
    )
    member_rail = _FakeSkillEvolutionRail(signal_trigger=False)
    manager.register_team_skill_rail("sess-1", team_rail)
    manager.register_team_member_skill_evolution_rail("sess-1", member_rail)

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    monkeypatch.setenv("SKILL_CREATE", "false")
    await manager.update_evolution_config({"evolution": {"auto_scan": True}})

    assert team_rail.signal_trigger is False
    assert team_rail.review_trigger is True
    assert member_rail.signal_trigger is True
    assert manager.get_team_skill_rail("sess-1") is team_rail
    assert manager.get_team_skill_create_rail("sess-1") is None


def test_find_team_skill_rail_for_request_uses_pending_approval_snapshots() -> None:
    manager = TeamManager()
    rail = _FakeTeamSkillEvolutionRail()
    rail.add_pending_approval_snapshot("team_skill_evolve_req1")
    manager.register_team_skill_rail("sess-1", rail)

    assert manager.find_team_skill_rail_for_request("team_skill_evolve_req1") is rail
    assert manager.find_team_skill_rail_for_request("missing") is None


def test_find_team_skill_rail_for_request_uses_pending_governance() -> None:
    manager = TeamManager()
    rail = _FakeTeamSkillEvolutionRail()
    rail.add_pending_governance("evolve_simplify_req1")
    manager.register_team_skill_rail("sess-1", rail)

    assert manager.find_team_skill_rail_for_request("evolve_simplify_req1") is rail
    assert manager.find_team_skill_rail_for_request("missing") is None


def test_refresh_team_shared_skill_links_across_managers_uses_registered_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_skills_dir = tmp_path / "global-skills"
    skill_dir = global_skills_dir / "skill-a"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: skill-a\n---\n", encoding="utf-8")
    team_shared_skills = tmp_path / "team-workspace" / "skills"

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    manager = get_team_manager("web")
    manager.register_team_shared_skill_link_target("sess-1", team_shared_skills)

    assert refresh_team_shared_skill_links_across_managers("sess-1")
    assert (team_shared_skills / "skill-a").resolve() == skill_dir.resolve()


@pytest.mark.asyncio
async def test_update_evolution_config_disables_team_skill_create_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SKILL_CREATE", raising=False)
    manager = TeamManager()
    rail = _FakeRail()
    agent = _FakeAgent()

    manager.register_team_skill_create_rail("sess-1", rail)
    manager.register_team_live_rail("sess-1", agent, rail)

    await manager.update_evolution_config({"evolution": {"skill_create": False}})

    assert manager.get_team_skill_create_rail("sess-1") is None
    assert agent.unregistered == [rail]


@pytest.mark.asyncio
async def test_update_evolution_config_skill_create_enabled_mounts_missing_team_skill_create_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    agent = _FakeAgent()
    context = TeamRailMountContext(
        agent=agent,
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(channel="web"),
        team_workspace=TeamWorkspaceInfo(
            root_dir="/tmp/team",
            skills_dir="/tmp/team/skills",
            team_id="demo-team",
            config={},
        ),
    )
    manager.register_team_rail_context("sess-1", context)

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("SKILL_CREATE", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"evolution": {"skill_create": True}},
    )

    def _fake_build_member_rails(**kwargs):
        if kwargs["team_workspace"].config.get("evolution", {}).get("skill_create"):
            return [_FakeTeamSkillCreateRail()]
        return []

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.build_member_rails",
        _fake_build_member_rails,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.TeamSkillCreateRail",
        _FakeTeamSkillCreateRail,
    )
    await manager.update_evolution_config(
        {"evolution": {"skill_create": True}}
    )

    assert isinstance(manager.get_team_skill_create_rail("sess-1"), _FakeTeamSkillCreateRail)
    assert len(agent.added_rails) == 1


@pytest.mark.asyncio
async def test_register_team_rail_context_keeps_leader_context() -> None:
    manager = TeamManager()
    leader_context = TeamRailMountContext(
        agent=_FakeAgent(),
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(channel="web"),
        team_workspace=TeamWorkspaceInfo(team_id="demo-team"),
    )
    member_context = TeamRailMountContext(
        agent=_FakeAgent(),
        member_info=MemberInfo(role="member"),
        runtime=RuntimeInfo(channel="web"),
        team_workspace=TeamWorkspaceInfo(team_id="demo-team"),
    )

    manager.register_team_rail_context("sess-1", leader_context)
    manager.register_team_rail_context("sess-1", member_context)

    assert manager.get_team_rail_context("sess-1") is leader_context


@pytest.mark.asyncio
async def test_update_evolution_config_skips_rail_rebuild_when_skill_create_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    agent = _FakeAgent()
    context = TeamRailMountContext(
        agent=agent,
        member_info=MemberInfo(role="leader"),
        runtime=RuntimeInfo(channel="web"),
        team_workspace=TeamWorkspaceInfo(
            root_dir="/tmp/team",
            skills_dir="/tmp/team/skills",
            team_id="demo-team",
            config={},
        ),
    )
    manager.register_team_rail_context("sess-1", context)

    monkeypatch.delenv("SKILL_CREATE", raising=False)

    await manager.update_evolution_config(
        {"evolution": {"skill_create": False}}
    )

    assert manager.get_team_skill_create_rail("sess-1") is None
    assert agent.unregistered == []
    assert manager.get_team_rail_context("sess-1") is context


@pytest.mark.asyncio
async def test_destroy_team_cleans_registered_evolution_rails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    rail = _FakeRail()
    agent = _FakeAgent()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.release_a2x_reservations_for_session",
        lambda session_id, *, team_agent=None: None,
    )
    manager.register_team_skill_rail("sess-1", rail)
    manager.register_team_member_skill_evolution_rail("sess-1", rail)
    manager.register_team_skill_create_rail("sess-1", rail)
    manager.register_team_live_rail("sess-1", agent, rail)
    manager.commit_runtime_ready("sess-1", "demo-team")

    cleaned = await manager.destroy_team("sess-1")

    assert cleaned is False
    assert manager.get_team_skill_rail("sess-1") is None
    assert manager.get_team_skill_create_rail("sess-1") is None


def test_team_manager_tracks_deferred_evolution_watcher() -> None:
    manager = TeamManager()

    manager.mark_team_evolution_watcher_deferred("sess-1")

    assert manager.consume_team_evolution_watcher_deferred("sess-1") is True
    assert manager.consume_team_evolution_watcher_deferred("sess-1") is False


@pytest.mark.asyncio
async def test_team_manager_keeps_single_session_per_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    destroyed_sessions: list[str] = []
    created_sessions: list[str] = []
    stopped_messagers: list[str] = []

    class _FakeTeamAgent:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.infra = type(
                "FakeInfra",
                (),
                {"messager": self._FakeMessager(session_id)},
            )()

        class _FakeMessager:
            def __init__(self, session_id: str) -> None:
                self.session_id = session_id

            async def stop(self) -> None:
                stopped_messagers.append(self.session_id)

        async def destroy_team(self, force: bool = False) -> bool:
            _ = force
            destroyed_sessions.append(self.session_id)
            return True

    class _FakeWorkspace:
        root_path = None

    def fake_load_team_spec(session_id: str):
        class _Spec:
            team_name = f"team-{session_id}"
            workspace = _FakeWorkspace()

            @staticmethod
            def build() -> _FakeTeamAgent:
                created_sessions.append(session_id)
                return _FakeTeamAgent(session_id)

        return _Spec()

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load_team_spec))
    # Mock _initialize_team_shared_skill_links to avoid file operations
    monkeypatch.setattr(
        TeamManager,
        "_initialize_team_shared_skill_links",
        staticmethod(lambda spec: None),
    )
    # Provider assembly is covered by the swarm suite; stub it so this
    # session-management test runs on the minimal fake spec.
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: None,
    )

    web_manager = get_team_manager("web")
    feishu_manager = get_team_manager("feishu")

    await web_manager.get_or_create_team("web-s1", deep_agent=object(), channel_id="web")
    await feishu_manager.get_or_create_team("fs-s1", deep_agent=object(), channel_id="feishu")
    await web_manager.get_or_create_team("web-s2", deep_agent=object(), channel_id="web")

    assert created_sessions == ["web-s1", "fs-s1", "web-s2"]
    assert destroyed_sessions == ["web-s1"]
    assert stopped_messagers == ["web-s1"]
    assert web_manager.get_team_agent("web-s1") is None
    assert isinstance(web_manager.get_team_agent("web-s2"), _FakeTeamAgent)
    assert isinstance(feishu_manager.get_team_agent("fs-s1"), _FakeTeamAgent)


@pytest.mark.asyncio
async def test_create_team_does_not_run_global_runtime_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeWorkspace:
        root_path = None

    def fake_load_team_spec(_session_id: str):
        class _Spec:
            team_name = "demo-team"
            workspace = _FakeWorkspace()

            @staticmethod
            def build():
                return SimpleNamespace()

        return _Spec()

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load_team_spec))
    # Mock _initialize_team_shared_skill_links to avoid file operations
    monkeypatch.setattr(
        TeamManager,
        "_initialize_team_shared_skill_links",
        staticmethod(lambda spec: None),
    )
    # Provider assembly is covered by the swarm suite; stub it so this
    # session-management test runs on the minimal fake spec.
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: None,
    )
    manager = TeamManager()

    team_agent = await manager.create_team("sess-1", deep_agent=object(), channel_id="web")

    assert team_agent is not None
    assert manager.get_team_agent("sess-1") is team_agent


@pytest.mark.asyncio
async def test_create_team_appends_session_id_to_team_name(monkeypatch: pytest.MonkeyPatch) -> None:
    created_team_names: list[str] = []

    class _FakeWorkspace:
        root_path = None

    class _Spec:
        def __init__(self) -> None:
            self.team_name = "demo_team"
            self.workspace = _FakeWorkspace()

        def build(self):
            created_team_names.append(self.team_name)
            return SimpleNamespace()

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(lambda _session_id: _Spec()))
    monkeypatch.setattr(
        TeamManager,
        "_initialize_team_shared_skill_links",
        staticmethod(lambda spec: None),
    )
    # Provider assembly is covered by the swarm suite; stub it so this
    # session-management test runs on the minimal fake spec.
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: None,
    )
    manager = TeamManager()

    team_agent = await manager.create_team("oc_abc123", deep_agent=object(), channel_id="feishu")

    assert team_agent is not None
    assert created_team_names == ["demo_team_oc_abc123"]


@pytest.mark.asyncio
async def test_create_team_appends_session_id_to_web_team_name(monkeypatch: pytest.MonkeyPatch) -> None:
    created_team_names: list[str] = []

    class _FakeWorkspace:
        root_path = None

    class _Spec:
        def __init__(self) -> None:
            self.team_name = "demo_team"
            self.workspace = _FakeWorkspace()

        def build(self):
            created_team_names.append(self.team_name)
            return SimpleNamespace()

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(lambda _session_id: _Spec()))
    monkeypatch.setattr(
        TeamManager,
        "_initialize_team_shared_skill_links",
        staticmethod(lambda spec: None),
    )
    # Provider assembly is covered by the swarm suite; stub it so this
    # session-management test runs on the minimal fake spec.
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: None,
    )
    manager = TeamManager()

    team_agent = await manager.create_team("oc_abc123", deep_agent=object(), channel_id="web")

    assert team_agent is not None
    assert created_team_names == ["demo_team_oc_abc123"]


@pytest.mark.asyncio
async def test_prepare_session_switch_stops_other_active_and_pending_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed"}}},
    )
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-active", "team-active")
    manager.set_pending_runtime_for_test("sess-pending", "team-pending")

    stopped: list[tuple[str, str]] = []

    async def fake_stop(self, session_id: str, reason: str = "") -> bool:
        stopped.append((session_id, reason))
        return True

    monkeypatch.setattr(
        TeamManager,
        "stop_session_runtime",
        fake_stop,
    )

    await manager.prepare_session_switch("sess-target", reason="session switch: ")

    assert stopped == [
        ("sess-active", "session switch: "),
        ("sess-pending", "session switch: "),
    ]


@pytest.mark.asyncio
async def test_prepare_session_switch_keeps_other_local_sessions_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"team": {"runtime": {"mode": "local"}}},
    )
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-active", "team-active")
    manager.set_pending_runtime_for_test("sess-pending", "team-pending")

    async def fail_stop(
        _self,
        _session_id: str,
        reason: str = "",
    ) -> bool:
        raise AssertionError(f"local session switch must not stop a runtime: {reason}")

    monkeypatch.setattr(TeamManager, "stop_session_runtime", fail_stop)

    await manager.prepare_session_switch("sess-target", reason="session switch: ")

    assert manager.get_active_team_name("sess-active") == "team-active"
    assert manager.is_runtime_pending("sess-pending") is True


@pytest.mark.asyncio
async def test_local_lifecycle_operations_run_concurrently_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"team": {"runtime": {"mode": "local"}}},
    )
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "team-1")
    manager.set_active_runtime_for_test("sess-2", "team-2")
    entered_sessions: set[str] = set()
    both_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def fake_cleanup(
        session_id: str,
        *,
        finalize_workflows: bool = True,
    ) -> None:
        _ = finalize_workflows
        entered_sessions.add(session_id)
        if len(entered_sessions) == 2:
            both_entered.set()
        await release_cleanup.wait()

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        _ = team_name, session_id
        return True

    monkeypatch.setattr(manager, "_cleanup_runtime_locals", fake_cleanup)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    first = asyncio.create_task(manager.stop_session_runtime("sess-1"))
    second = asyncio.create_task(manager.stop_session_runtime("sess-2"))
    await asyncio.wait_for(both_entered.wait(), timeout=1.0)
    release_cleanup.set()

    assert await asyncio.gather(first, second) == [True, True]
    assert entered_sessions == {"sess-1", "sess-2"}


@pytest.mark.asyncio
async def test_local_lifecycle_operations_are_serialized_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"team": {"runtime": {"mode": "local"}}},
    )
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "team-1")
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_calls = 0

    async def fake_cleanup(
        session_id: str,
        *,
        finalize_workflows: bool = True,
    ) -> None:
        nonlocal cleanup_calls
        _ = session_id, finalize_workflows
        cleanup_calls += 1
        cleanup_entered.set()
        await release_cleanup.wait()

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        _ = team_name, session_id
        return True

    monkeypatch.setattr(manager, "_cleanup_runtime_locals", fake_cleanup)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    first = asyncio.create_task(manager.stop_session_runtime("sess-1"))
    await asyncio.wait_for(cleanup_entered.wait(), timeout=1.0)
    second = asyncio.create_task(manager.stop_session_runtime("sess-1"))
    await asyncio.sleep(0)

    assert second.done() is False
    release_cleanup.set()
    assert await asyncio.gather(first, second) == [True, False]
    assert cleanup_calls == 1


@pytest.mark.asyncio
async def test_finalize_runtime_cleanup_releases_session_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    session_id = "sess-terminal-cleanup"
    manager.commit_runtime_ready(session_id, "team-terminal")
    manager.mark_seen_team_events(session_id)
    manager.mark_workflow_completed(session_id)
    manager.setdefault_cron_completion(session_id, {"round_id": 1})
    getattr(manager, "_pending_team_evolution_watcher_sessions").add(session_id)

    async def fake_cleanup(
        _session_id: str,
        *,
        finalize_workflows: bool = True,
    ) -> None:
        _ = _session_id, finalize_workflows
        manager._clear_team_rail_registries(session_id)

    monkeypatch.setattr(manager, "_cleanup_runtime_locals", fake_cleanup)

    await manager._finalize_runtime_cleanup(session_id, "test")

    assert manager.is_runtime_active(session_id) is False
    assert manager.is_session_initialized(session_id) is False
    assert manager.has_seen_team_events(session_id) is False
    assert manager.is_workflow_completed(session_id) is False
    assert manager.get_cron_completion(session_id) is None
    assert session_id not in getattr(
        manager,
        "_pending_team_evolution_watcher_sessions",
    )


@pytest.mark.asyncio
async def test_cancel_all_stream_tasks_uses_per_session_lifecycle_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"team": {"runtime": {"mode": "local"}}},
    )
    manager = _TeamManagerHarness()
    first_cancelled = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def wait_until_cancelled(cancelled: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    first_task = asyncio.create_task(wait_until_cancelled(first_cancelled))
    second_task = asyncio.create_task(wait_until_cancelled(second_cancelled))
    await asyncio.sleep(0)
    manager.register_stream_task("sess-1", first_task)
    manager.register_stream_task("sess-2", second_task)

    first_session_lock = manager.get_lifecycle_lock_for_test("sess-1")
    async with first_session_lock:
        cancel_all = asyncio.create_task(manager.cancel_all_stream_tasks())
        await asyncio.wait_for(second_cancelled.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert first_cancelled.is_set() is False
        assert cancel_all.done() is False

    await asyncio.wait_for(cancel_all, timeout=1.0)

    assert first_cancelled.is_set() is True
    assert manager.has_stream_task("sess-1") is False
    assert manager.has_stream_task("sess-2") is False


@pytest.mark.asyncio
async def test_distributed_runtime_activations_switch_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed"}}},
    )
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-old", "team-old")
    old_stop_entered = asyncio.Event()
    release_old_stop = asyncio.Event()
    stopped_sessions: list[str] = []

    async def fake_stop(
        self,
        session_id: str,
        reason: str = "",
    ) -> bool:
        _ = reason
        stopped_sessions.append(session_id)
        if session_id == "sess-old":
            old_stop_entered.set()
            await release_old_stop.wait()
        self.clear_active_runtime(session_id)
        self.clear_pending_runtime(session_id)
        return True

    monkeypatch.setattr(TeamManager, "stop_session_runtime", fake_stop)

    first = asyncio.create_task(manager.prepare_runtime_activation("sess-1", "team-1"))
    await asyncio.wait_for(old_stop_entered.wait(), timeout=1.0)
    second = asyncio.create_task(manager.prepare_runtime_activation("sess-2", "team-2"))
    release_old_stop.set()
    await asyncio.gather(first, second)

    assert stopped_sessions == ["sess-old", "sess-1"]
    assert manager.is_runtime_pending("sess-1") is False
    assert manager.is_runtime_pending("sess-2") is True


@pytest.mark.asyncio
async def test_delete_session_runtime_deletes_single_team_session_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")

    stopped: list[tuple[str, str]] = []
    deleted_teams: list[dict] = []

    async def fake_stop(self, session_id: str, reason: str = "") -> bool:
        stopped.append((session_id, reason))
        return True

    async def fake_delete_agent_team(*, team_name: str, session_ids: list[str], force: bool) -> bool:
        deleted_teams.append(
            {"team_name": team_name, "session_ids": session_ids, "force": force}
        )
        return True

    monkeypatch.setattr(TeamManager, "stop_session_runtime", fake_stop)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        fake_delete_agent_team,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id: {"team_name": "demo-team"},
    )

    deleted = await manager.delete_session_runtime("sess-1", reason="session.delete: ")

    assert deleted is True
    assert stopped == [("sess-1", "session.delete: ")]
    assert deleted_teams == [
        {"team_name": "demo-team", "session_ids": ["sess-1"], "force": True}
    ]


@pytest.mark.asyncio
async def test_delete_session_runtime_uses_metadata_not_active_team_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "active-team")

    deleted_teams: list[dict] = []

    async def fake_stop(self, session_id: str, reason: str = "") -> bool:
        return True

    async def fake_delete_agent_team(*, team_name: str, session_ids: list[str], force: bool) -> bool:
        deleted_teams.append(
            {"team_name": team_name, "session_ids": session_ids, "force": force}
        )
        return True

    monkeypatch.setattr(TeamManager, "stop_session_runtime", fake_stop)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        fake_delete_agent_team,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id: {"team_name": "metadata-team"},
    )

    deleted = await manager.delete_session_runtime("sess-1", reason="session.delete: ")

    assert deleted is True
    assert deleted_teams == [
        {"team_name": "metadata-team", "session_ids": ["sess-1"], "force": True}
    ]


@pytest.mark.asyncio
async def test_stop_session_runtime_stops_runner_owned_team_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")
    manager.set_active_runtime_for_test("sess-2", "other-team")
    getattr(manager, "_initialized_sessions").add("sess-1")
    manager.mark_seen_team_events("sess-1")
    manager.mark_workflow_completed("sess-1")
    manager.mark_team_evolution_watcher_deferred("sess-1")

    stop_calls: list[tuple[str, str]] = []

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        stop_calls.append((team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    stopped = await manager.stop_session_runtime("sess-1", reason="switch runtime: ")

    assert stopped is True
    assert stop_calls == [("demo-team", "sess-1")]
    assert manager.is_runtime_active("sess-1") is False
    assert manager.get_active_team_name("sess-2") == "other-team"
    assert manager.is_session_initialized("sess-1") is False
    assert manager.has_seen_team_events("sess-1") is False
    assert manager.is_workflow_completed("sess-1") is False
    assert manager.consume_team_evolution_watcher_deferred("sess-1") is False


@pytest.mark.asyncio
async def test_stop_session_runtime_clears_stale_markers_without_live_runtime() -> None:
    manager = _TeamManagerHarness()
    session_id = "sess-stale-markers"
    getattr(manager, "_initialized_sessions").add(session_id)
    manager.mark_seen_team_events(session_id)
    manager.mark_workflow_completed(session_id)
    manager.mark_team_evolution_watcher_deferred(session_id)
    manager.setdefault_cron_completion(session_id, {"round_id": 1})

    stopped = await manager.stop_session_runtime(session_id)

    assert stopped is False
    assert manager.is_session_initialized(session_id) is False
    assert manager.has_seen_team_events(session_id) is False
    assert manager.is_workflow_completed(session_id) is False
    assert manager.consume_team_evolution_watcher_deferred(session_id) is False
    assert manager.get_cron_completion(session_id) is None


@pytest.mark.asyncio
async def test_pause_session_runtime_pauses_runner_owned_team_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")
    getattr(manager, "_initialized_sessions").add("sess-1")
    manager.mark_seen_team_events("sess-1")
    manager.mark_workflow_completed("sess-1")
    manager.mark_team_evolution_watcher_deferred("sess-1")

    pause_calls: list[tuple[str, str]] = []

    async def fake_pause_agent_team(*, team_name: str, session_id: str) -> bool:
        pause_calls.append((team_name, session_id))
        return True

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        raise AssertionError("pause should not stop the Runner-owned team runtime")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.pause_agent_team",
        fake_pause_agent_team,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    paused = await manager.pause_session_runtime("sess-1", reason="interrupt(intent=pause): ")

    assert paused is True
    assert pause_calls == [("demo-team", "sess-1")]
    assert manager.is_runtime_active("sess-1") is False
    assert manager.is_session_initialized("sess-1") is True
    assert manager.has_seen_team_events("sess-1") is True
    assert manager.is_workflow_completed("sess-1") is True
    assert manager.consume_team_evolution_watcher_deferred("sess-1") is True


@pytest.mark.asyncio
async def test_pause_session_runtime_waits_for_stream_task_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")
    stream_can_exit = asyncio.Event()
    stream_exited = asyncio.Event()

    async def stream_task_body() -> None:
        await stream_can_exit.wait()
        stream_exited.set()

    stream_task = asyncio.create_task(stream_task_body())
    manager.register_stream_task_for_test("sess-1", stream_task)

    async def fake_pause_agent_team(*, team_name: str, session_id: str) -> bool:
        assert (team_name, session_id) == ("demo-team", "sess-1")
        stream_can_exit.set()
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.pause_agent_team",
        fake_pause_agent_team,
    )

    paused = await manager.pause_session_runtime("sess-1", reason="interrupt(intent=pause): ")

    assert paused is True
    assert stream_exited.is_set()
    assert stream_task.done()
    assert not stream_task.cancelled()
    assert manager.has_stream_task("sess-1") is False


@pytest.mark.asyncio
async def test_pause_session_runtime_warns_and_cancels_stream_task_after_grace_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")
    stream_cancelled = asyncio.Event()

    async def stream_task_body() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stream_cancelled.set()
            raise

    stream_task = asyncio.create_task(stream_task_body())
    manager.register_stream_task_for_test("sess-1", stream_task)

    async def fake_pause_agent_team(*, team_name: str, session_id: str) -> bool:
        assert (team_name, session_id) == ("demo-team", "sess-1")
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.pause_agent_team",
        fake_pause_agent_team,
    )
    original_wait_for_stream_task_exit = manager._wait_for_stream_task_exit

    async def wait_for_stream_task_exit_with_short_timeout(session_id: str) -> bool:
        return await original_wait_for_stream_task_exit(session_id, timeout_sec=0.01)

    monkeypatch.setattr(
        manager,
        "_wait_for_stream_task_exit",
        wait_for_stream_task_exit_with_short_timeout,
    )
    warning_messages: list[str] = []

    def fake_warning(message: str, *args) -> None:
        warning_messages.append(message % args if args else message)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.logger.warning",
        fake_warning,
    )

    paused = await manager.pause_session_runtime("sess-1", reason="interrupt(intent=pause): ")

    assert paused is True
    assert stream_cancelled.is_set()
    assert stream_task.cancelled()
    assert manager.has_stream_task("sess-1") is False
    assert any("stream task did not exit within grace timeout" in message for message in warning_messages)


@pytest.mark.asyncio
async def test_interact_uses_runner_only_for_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")

    class _LocalTeamAgent:
        async def interact(self, _user_input: str) -> None:
            raise AssertionError("single-machine interact should not use local TeamAgent")

    interact_calls: list[tuple[str, str, str]] = []

    async def fake_interact_agent_team(user_input: str, *, team_name: str, session_id: str) -> bool:
        interact_calls.append((user_input, team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.interact_agent_team",
        fake_interact_agent_team,
    )

    success, reason = await manager.interact("sess-1", "hello team")

    assert success is True
    assert reason is None
    assert interact_calls == [("hello team", "demo-team", "sess-1")]


@pytest.mark.asyncio
async def test_interact_refreshes_active_team_model_pool_trace_before_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.xiaoyi_invocation import get_xiaoyi_trace_header_exporters

    assert get_xiaoyi_trace_header_exporters()
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")
    pool_entry = SimpleNamespace(metadata={"client": {"custom_headers": {"keep": "yes"}}})
    evolution_model_config = {
        "model_client_config": {"custom_headers": {"evolution": "yes"}},
        "model_config_obj": {"temperature": 0.3},
        "model_name": "trace-test-model",
    }
    evolution_rail = SimpleNamespace(
        params={"evolution_model_config": evolution_model_config}
    )
    agent_spec = SimpleNamespace(model=None, rails=[evolution_rail], subagents=None)
    spec = SimpleNamespace(
        model_pool=[pool_entry],
        model_router=None,
        agents={"leader": agent_spec},
    )
    updated_pools: list[list[object]] = []
    team_agent = SimpleNamespace(
        _configurator=SimpleNamespace(ctx=SimpleNamespace(team_spec=spec)),
        update_model_pool=lambda pool: updated_pools.append(pool),
    )
    previous_evolution_model = SimpleNamespace(
        model_client_config=SimpleNamespace(custom_headers={"evolution": "yes"}),
        model_config=SimpleNamespace(temperature=0.3),
    )
    refreshed_evolution_models: list[tuple[object, str]] = []
    live_evolution_rail = SimpleNamespace(
        evolver=SimpleNamespace(llm=previous_evolution_model, model="trace-test-model"),
        update_llm=lambda model, name: refreshed_evolution_models.append((model, name)),
    )
    manager.register_team_skill_rail("sess-1", live_evolution_rail)

    class _Pool:
        async def get(self, team_name: str):
            assert team_name == "demo-team"
            return SimpleNamespace(agent=team_agent)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager._runner_team_runtime_manager",
        lambda _runner: SimpleNamespace(pool=_Pool()),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Model",
        lambda *, model_client_config, model_config: SimpleNamespace(
            model_client_config=model_client_config,
            model_config=model_config,
        ),
        raising=False,
    )

    async def fake_interact_agent_team(user_input: str, *, team_name: str, session_id: str) -> bool:
        assert pool_entry.metadata["client"]["custom_headers"] == {
            "keep": "yes",
            "x-hag-trace-id": "root&19&abc&0",
            "x-session-id": "root",
            "x-interaction-id": "19",
        }
        assert evolution_model_config["model_client_config"]["custom_headers"] == {
            "evolution": "yes",
            "x-hag-trace-id": "root&19&abc&0",
            "x-session-id": "root",
            "x-interaction-id": "19",
        }
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.interact_agent_team",
        fake_interact_agent_team,
    )
    remote_updates: list[tuple[object, str, dict[str, object] | None]] = []

    async def fake_notify_remote_trace(team_agent, session_id, *, request_metadata):
        remote_updates.append((team_agent, session_id, request_metadata))
        return 1

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.notify_remote_members_trace_context_update",
        fake_notify_remote_trace,
    )

    result = await manager.interact(
        "sess-1",
        "follow up",
        request_metadata={
            TRACE_HEADER_EXPORTER_METADATA_KEY: "xiaoyi",
            TRACE_CONTEXT_METADATA_KEY: {
                "version": 1,
                "trace_id": "root&19&abc&0",
                "conversation_id": "root",
                "interaction_id": "19",
            }
        },
    )

    assert result == (True, None)
    assert updated_pools == [[pool_entry]]
    assert len(refreshed_evolution_models) == 1
    refreshed_model, refreshed_model_name = refreshed_evolution_models[0]
    assert refreshed_model_name == "trace-test-model"
    assert refreshed_model.model_client_config.custom_headers == {
        "evolution": "yes",
        "x-hag-trace-id": "root&19&abc&0",
        "x-session-id": "root",
        "x-interaction-id": "19",
    }
    assert remote_updates == [
        (
            team_agent,
            "sess-1",
            {
                TRACE_HEADER_EXPORTER_METADATA_KEY: "xiaoyi",
                TRACE_CONTEXT_METADATA_KEY: {
                    "version": 1,
                    "trace_id": "root&19&abc&0",
                    "conversation_id": "root",
                    "interaction_id": "19",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_interact_routes_multiple_local_sessions_to_their_own_teams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team-sess-1")
    manager.set_active_runtime_for_test("sess-2", "demo-team-sess-2")
    interact_calls: list[tuple[str, str, str]] = []

    async def fake_interact_agent_team(
        user_input: str,
        *,
        team_name: str,
        session_id: str,
    ) -> bool:
        interact_calls.append((user_input, team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.interact_agent_team",
        fake_interact_agent_team,
    )

    first_result = await manager.interact("sess-1", "first")
    second_result = await manager.interact("sess-2", "second")

    assert first_result == (True, None)
    assert second_result == (True, None)
    assert interact_calls == [
        ("first", "demo-team-sess-1", "sess-1"),
        ("second", "demo-team-sess-2", "sess-2"),
    ]


@pytest.mark.asyncio
async def test_interact_returns_false_for_non_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-active", "demo-team")

    interact_calls: list[tuple[str, str, str]] = []

    async def fake_interact_agent_team(user_input: str, *, team_name: str, session_id: str) -> bool:
        interact_calls.append((user_input, team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.interact_agent_team",
        fake_interact_agent_team,
    )

    success, reason = await manager.interact("sess-other", "hello team")

    assert success is False
    assert reason == "not_active"
    assert interact_calls == []


@pytest.mark.asyncio
async def test_interact_restores_resumable_runtime_before_runner_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.clear_active_runtime("sess-1")

    async def fake_resolve_resumable_runner_entry(session_id: str):
        assert session_id == "sess-1"
        return "demo-team", SimpleNamespace(
            current_session_id="sess-1",
            state="paused",
        )

    interact_calls: list[tuple[str, str, str]] = []

    async def fake_interact_agent_team(user_input: str, *, team_name: str, session_id: str) -> bool:
        interact_calls.append((user_input, team_name, session_id))
        return True

    manager.stub_resolve_resumable_runner_entry_for_test(fake_resolve_resumable_runner_entry)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.interact_agent_team",
        fake_interact_agent_team,
    )

    success, reason = await manager.interact("sess-1", "plan.approve")

    assert success is True
    assert reason is None
    assert manager.get_active_team_name("sess-1") == "demo-team"
    assert interact_calls == [("plan.approve", "demo-team", "sess-1")]


@pytest.mark.asyncio
async def test_resolve_resumable_runner_entry_ignores_stale_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-stale", "stale-team")

    resumable_entry = SimpleNamespace(
        current_session_id="sess-current",
        state=RuntimeState.PAUSED,
    )

    class _FakePool:
        @staticmethod
        async def get(team_name: str):
            assert team_name == "demo-team"
            return resumable_entry

    fake_runner = SimpleNamespace(_team_runtime_manager=SimpleNamespace(pool=_FakePool()))

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda session_id: {"team_name": "demo-team"} if session_id == "sess-current" else {},
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.runner.GLOBAL_RUNNER",
        fake_runner,
    )

    resolved = await manager.resolve_resumable_runner_entry_for_test("sess-current")

    assert resolved == ("demo-team", resumable_entry)


@pytest.mark.asyncio
async def test_interact_restores_resumable_runtime_even_with_stale_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-stale", "stale-team")

    resumable_entry = SimpleNamespace(
        current_session_id="sess-1",
        state=RuntimeState.PAUSED,
    )

    class _FakePool:
        @staticmethod
        async def get(team_name: str):
            assert team_name == "demo-team"
            return resumable_entry

    fake_runner = SimpleNamespace(_team_runtime_manager=SimpleNamespace(pool=_FakePool()))
    interact_calls: list[tuple[str, str, str]] = []

    async def fake_interact_agent_team(user_input: str, *, team_name: str, session_id: str) -> bool:
        interact_calls.append((user_input, team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda session_id: {"team_name": "demo-team"} if session_id == "sess-1" else {},
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.runner.GLOBAL_RUNNER",
        fake_runner,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.interact_agent_team",
        fake_interact_agent_team,
    )

    success, reason = await manager.interact("sess-1", "plan.approve")

    assert success is True
    assert reason is None
    assert manager.get_active_team_name("sess-1") == "demo-team"
    assert interact_calls == [("plan.approve", "demo-team", "sess-1")]


@pytest.mark.asyncio
async def test_wait_for_resumable_runtime_polls_until_runtime_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    restore_calls: list[str] = []

    async def fake_restore(session_id: str) -> bool:
        restore_calls.append(session_id)
        if len(restore_calls) == 2:
            manager.commit_runtime_ready(session_id, "demo-team")
            return True
        return False

    async def fake_sleep(_seconds: float) -> None:
        return None

    manager.restore_resumable_runtime = fake_restore
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.asyncio.sleep",
        fake_sleep,
    )

    restored = await manager.wait_for_resumable_runtime(
        "sess-1",
        timeout_sec=0.1,
        poll_interval_sec=0.01,
    )

    assert restored is True
    assert restore_calls == ["sess-1", "sess-1"]
    assert manager.get_active_team_name("sess-1") == "demo-team"


@pytest.mark.asyncio
async def test_stop_session_runtime_ignores_local_team_cache_in_single_machine_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "demo-team")

    class _LocalTeamAgent:
        async def destroy_team(self, force: bool = False) -> bool:
            _ = force
            raise AssertionError("single-machine stop should not destroy local TeamAgent cache")

    stop_calls: list[tuple[str, str]] = []

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        stop_calls.append((team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    manager.cache_local_team_agent_for_test("sess-1", _LocalTeamAgent())

    stopped = await manager.stop_session_runtime("sess-1", reason="switch runtime: ")

    assert stopped is True
    assert stop_calls == [("demo-team", "sess-1")]


@pytest.mark.asyncio
async def test_stop_session_runtime_uses_metadata_team_name_for_non_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.register_stream_task("sess-1", asyncio.create_task(asyncio.sleep(0)))

    stop_calls: list[tuple[str, str]] = []

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        stop_calls.append((team_name, session_id))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id: {"team_name": "meta-team"},
    )

    stopped = await manager.stop_session_runtime("sess-1", reason="switch runtime: ")

    assert stopped is True
    assert stop_calls == [("meta-team", "sess-1")]


@pytest.mark.asyncio
async def test_delete_session_runtime_uses_metadata_team_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()

    stop_calls: list[tuple[str, str]] = []
    deleted_teams: list[dict] = []

    async def fake_stop(self, session_id: str, reason: str = "") -> bool:
        stop_calls.append((session_id, reason))
        return True

    async def fake_delete_agent_team(*, team_name: str, session_ids: list[str], force: bool) -> bool:
        deleted_teams.append(
            {"team_name": team_name, "session_ids": session_ids, "force": force}
        )
        return True

    monkeypatch.setattr(TeamManager, "stop_session_runtime", fake_stop)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        fake_delete_agent_team,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id: {"team_name": "meta-team"},
    )

    deleted = await manager.delete_session_runtime("sess-1", reason="session.delete: ")

    assert deleted is True
    assert stop_calls == [("sess-1", "session.delete: ")]
    assert deleted_teams == [
        {"team_name": "meta-team", "session_ids": ["sess-1"], "force": True}
    ]


@pytest.mark.asyncio
async def test_delete_session_runtime_falls_back_to_release_without_team_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    manager.set_active_runtime_for_test("sess-1", "active-team")

    stop_calls: list[tuple[str, str]] = []
    released: list[str] = []

    async def fake_stop(self, session_id: str, reason: str = "") -> bool:
        stop_calls.append((session_id, reason))
        return True

    async def fake_release(session_id: str) -> None:
        released.append(session_id)

    async def fake_delete_agent_team(*, team_name: str, session_ids: list[str], force: bool) -> bool:
        raise AssertionError("delete_agent_team should not use active team_name when metadata is missing")

    monkeypatch.setattr(TeamManager, "stop_session_runtime", fake_stop)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.release",
        fake_release,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.delete_agent_team",
        fake_delete_agent_team,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id: {},
    )

    deleted = await manager.delete_session_runtime("sess-1", reason="session.delete: ")

    assert deleted is True
    assert stop_calls == [("sess-1", "session.delete: ")]
    assert released == ["sess-1"]


def test_resolve_session_team_name_returns_none_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id: {},
    )

    team_name = manager.resolve_session_team_name_for_test("sess-missing")

    assert team_name is None


@pytest.mark.asyncio
async def test_get_swarm_enriched_team_spec_uses_bound_stable_team_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    load_calls: list[dict] = []
    enrich_calls: list[dict] = []

    class _Spec:
        team_name = "template_team"

    async def fake_ensure_postgresql(self, config_base: dict) -> None:
        _ = self, config_base

    def fake_load_team_spec(session_id: str, **kwargs):
        load_calls.append({"session_id": session_id, **kwargs})
        return _Spec()

    def fake_enrich(spec, **kwargs) -> None:
        enrich_calls.append({"team_name": spec.team_name, **kwargs})

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {
            "team_name": "custom_team",
            "team_template_id": "beta_template",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(get=lambda _team_name: None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.ensure_team_entity",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(TeamManager, "_ensure_postgresql_for_leader", fake_ensure_postgresql)
    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load_team_spec))
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        fake_enrich,
    )

    spec = await manager.get_swarm_enriched_team_spec(
        "sess-bound",
        mode="team",
        request_metadata={"mode": "team"},
    )

    assert spec.team_name == "custom_team"
    assert load_calls == [
        {
            "session_id": "sess-bound",
            "template_id": "beta_template",
            "strict_template": True,
        }
    ]
    assert enrich_calls[0]["team_name"] == "custom_team"


@pytest.mark.asyncio
async def test_get_swarm_enriched_team_spec_prefetches_group_package(
        monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """绑定专家团的会话在 async 边界预取包目录（缓存优先 + fetch 兜底），
    结果经 agent_group_package_dir 透传进 enrich——装配点不再裸读缓存。
    """
    from jiuwenswarm.server.runtime.expert import expert_store as es

    manager = _TeamManagerHarness()
    enrich_calls: list[dict] = []
    resolve_calls: list[str] = []

    class _Spec:
        team_name = "template_team"

    async def fake_ensure_postgresql(self, config_base: dict) -> None:
        _ = self, config_base

    async def fake_resolve(expert_id: str):
        resolve_calls.append(expert_id)
        return tmp_path / "prefetched" / expert_id

    def fake_enrich(spec, **kwargs) -> None:
        enrich_calls.append({"team_name": spec.team_name, **kwargs})

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {
            "team_name": "custom_team",
            "team_template_id": "expert_group",
            "expert_id": "sample-expert-group",
            "expert_type": "team",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(get=lambda _team_name: None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.ensure_team_entity",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(TeamManager, "_ensure_postgresql_for_leader", fake_ensure_postgresql)
    monkeypatch.setattr(
        TeamManager, "_load_team_spec", staticmethod(lambda _session_id, **_kw: _Spec())
    )
    monkeypatch.setattr(es, "resolve_expert_package_dir", fake_resolve)
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        fake_enrich,
    )

    await manager.get_swarm_enriched_team_spec(
        "sess-group",
        mode="team",
        request_metadata={"mode": "team"},
    )

    assert resolve_calls == ["sample-expert-group"]
    assert enrich_calls[0]["agent_group_name"] == "sample-expert-group"
    assert (
        enrich_calls[0]["agent_group_package_dir"]
        == tmp_path / "prefetched" / "sample-expert-group"
    )


@pytest.mark.asyncio
async def test_get_swarm_enriched_team_spec_no_group_skips_prefetch(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未绑定专家团（expert_type 缺省 agent）时不预取、不透传 package_dir。"""
    from jiuwenswarm.server.runtime.expert import expert_store as es

    manager = _TeamManagerHarness()
    enrich_calls: list[dict] = []

    class _Spec:
        team_name = "template_team"

    async def fake_ensure_postgresql(self, config_base: dict) -> None:
        _ = self, config_base

    async def fake_resolve(expert_id: str):
        raise AssertionError("未绑定专家团时不应预取")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {
            "team_name": "custom_team",
            "team_template_id": "beta_template",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(get=lambda _team_name: None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.ensure_team_entity",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(TeamManager, "_ensure_postgresql_for_leader", fake_ensure_postgresql)
    monkeypatch.setattr(
        TeamManager, "_load_team_spec", staticmethod(lambda _session_id, **_kw: _Spec())
    )
    monkeypatch.setattr(es, "resolve_expert_package_dir", fake_resolve)
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: enrich_calls.append(kwargs),
    )

    await manager.get_swarm_enriched_team_spec(
        "sess-plain",
        mode="team",
        request_metadata={"mode": "team"},
    )

    assert enrich_calls[0]["agent_group_name"] is None
    assert enrich_calls[0]["agent_group_package_dir"] is None


@pytest.mark.asyncio
async def test_get_swarm_enriched_team_spec_uses_bound_team_entity_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()
    load_calls: list[dict] = []

    class _Spec:
        team_name = "template_team"

    async def fake_ensure_postgresql(self, config_base: dict) -> None:
        _ = self, config_base

    def fake_load_team_spec(session_id: str, **kwargs):
        load_calls.append({"session_id": session_id, **kwargs})
        return _Spec()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {
            "team_name": "custom_team",
            "team_template_id": "deleted_template",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(get=lambda _team_name: None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.ensure_team_entity",
        lambda **_kwargs: SimpleNamespace(
            template_id="deleted_template",
            template_snapshot={
                "team_name": "template_team",
                "leader": {"member_name": "snapshot_leader"},
            },
        ),
    )
    monkeypatch.setattr(TeamManager, "_ensure_postgresql_for_leader", fake_ensure_postgresql)
    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load_team_spec))
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: None,
    )

    spec = await manager.get_swarm_enriched_team_spec(
        "sess-bound",
        mode="team",
        request_metadata={"mode": "team"},
    )

    assert spec.team_name == "custom_team"
    assert load_calls == [
        {
            "session_id": "sess-bound",
            "template_id": "deleted_template",
            "strict_template": False,
            "template_snapshot": {
                "team_name": "template_team",
                "leader": {"member_name": "snapshot_leader"},
            },
        }
    ]


@pytest.mark.asyncio
async def test_get_swarm_enriched_team_spec_keeps_legacy_session_scoped_team_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TeamManagerHarness()

    class _Spec:
        team_name = "template_team"

    async def fake_ensure_postgresql(self, config_base: dict) -> None:
        _ = self, config_base

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {},
    )
    monkeypatch.setattr(TeamManager, "_ensure_postgresql_for_leader", fake_ensure_postgresql)
    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(lambda _session_id: _Spec()))
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: None,
    )

    spec = await manager.get_swarm_enriched_team_spec(
        "sess-legacy",
        mode="team",
        request_metadata={"mode": "team"},
    )

    assert spec.team_name == "template_team_sess-legacy"


def test_register_workflow_handler() -> None:
    tm = TeamManager()
    fake_handler = type("FakeWorkflowHandler", (), {"session_id": "sess_1"})()
    tm.register_workflow_handler("sess_1", fake_handler)
    assert tm.get_workflow_handler("sess_1") is fake_handler


def test_pop_workflow_handler() -> None:
    tm = TeamManager()
    fake_handler = type("FakeWorkflowHandler", (), {"session_id": "sess_1"})()
    tm.register_workflow_handler("sess_1", fake_handler)
    popped = tm.pop_workflow_handler("sess_1")
    assert popped is fake_handler
    assert tm.get_workflow_handler("sess_1") is None


def test_get_workflow_handler_returns_none_for_unknown() -> None:
    tm = TeamManager()
    assert tm.get_workflow_handler("unknown_sess") is None
