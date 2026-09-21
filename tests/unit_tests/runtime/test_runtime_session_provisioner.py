from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry


class Manager:
    def __init__(self, events):
        self.events = events

    async def release_subagent_runtime_for_session(self, **_kwargs):
        self.events.append("dispose.subagent")

    async def cleanup_session_runtime(self, **_kwargs):
        self.events.append("dispose.agent")
        return True


class Plan:
    def __init__(self, events):
        self.events = events

    def reset_session(self, _sid):
        self.events.append("commit.plan")


class Lifecycle:
    is_available = True

    def __init__(self, events):
        self.events = events

    async def begin_session_delete(self, _sid):
        self.events.append("fence.lifecycle")

    async def abort_session_delete(self, _sid, **_kwargs):
        self.events.append("abort.lifecycle")

    async def commit_session_delete(self, _sid):
        self.events.append("commit.lifecycle")


class Participant:
    name = "derived"

    def __init__(self, events):
        self.events, self.before_error = events, False

    async def before_delete(self, _target):
        self.events.append("participant.before")
        if self.before_error:
            raise RuntimeError("partial before")

    async def release_resources(self, _target):
        self.events.append("participant.release")

    async def delete_failed(self, _target, *, destructive_started):
        self.events.append(f"participant.failed.{destructive_started}")

    async def delete_committed(self, _target):
        self.events.append("participant.committed")


class TeamController:
    def __init__(self, events):
        self.events = events

    async def quiesce_for_delete(self, _target, *, reason):
        self.events.append("team.quiesce")

    async def dispose_after_resource_release(self, _target, *, reason):
        self.events.append("team.dispose")

    def delete_aborted(self, _target):
        self.events.append("team.aborted")

    def delete_committed(self, _target):
        self.events.append("team.committed")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    events = []
    root = tmp_path / "sessions"
    root.mkdir()
    metadata = {"mode": "agent.work.normal", "channel_id": "web"}

    async def checkpointer():
        events.append("checkpointer")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        checkpointer,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid: dict(metadata),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.remove_session_metadata_cache",
        lambda _sid: events.append("commit.metadata"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.observability.session_delete.begin_trajectory_session_delete",
        lambda _sid: events.append("fence.trajectory"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.observability.session_delete.abort_trajectory_session_delete",
        lambda _sid: events.append("abort.trajectory"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.observability.session_delete.commit_trajectory_session_delete",
        lambda _sid: events.append("commit.trajectory"),
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.release",
        AsyncMock(side_effect=lambda _sid: events.append("runner.release")),
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        AsyncMock(side_effect=lambda **_kwargs: events.append("runner.team") or True),
    )
    participant = Participant(events)
    registry = RuntimeParticipantRegistry()
    registry.replace_session_participants(activity=(), delete=(participant,))
    runtime = AgentRuntime(
        agent_manager=Manager(events),
        initializer=AsyncMock(),
        plan_controller=Plan(events),
        session_delete_lifecycle=Lifecycle(events),
        participant_registry=registry,
        team_execution_controller=TeamController(events),
    )
    return SimpleNamespace(
        runtime=runtime,
        root=root,
        events=events,
        metadata=metadata,
        participant=participant,
    )


@pytest.mark.asyncio
async def test_agent_delete_orders_release_before_dispose_and_runner(env):
    path = env.root / "s1"
    path.mkdir()
    result = await env.runtime.delete_session(channel_id="request", session_id="s1")
    assert result.ok and result.deleted and not result.recovery_required
    assert env.events.index("participant.release") < env.events.index(
        "dispose.subagent"
    )
    assert env.events.index("dispose.agent") < env.events.index("runner.release")
    assert env.events.index("runner.release") < env.events.index("commit.lifecycle")
    assert env.events[-1] == "participant.committed"
    assert not path.exists()


@pytest.mark.asyncio
async def test_partial_before_is_entered_and_release_is_independent(env):
    (env.root / "s2").mkdir()
    env.participant.before_error = True
    result = await env.runtime.delete_session(channel_id="web", session_id="s2")
    assert result.ok
    assert "participant.release" in env.events and "participant.committed" in env.events


@pytest.mark.asyncio
async def test_pre_destructive_failure_restores_fence(env):
    path = env.root / "s3"
    path.mkdir()
    env.runtime._session_provisioner._delete_lifecycle.begin_session_delete = AsyncMock(
        side_effect=RuntimeError("fence failed")
    )
    result = await env.runtime.delete_session(channel_id="web", session_id="s3")
    assert not result.ok and not result.recovery_required and path.exists()
    assert "participant.failed.False" in env.events


@pytest.mark.asyncio
async def test_post_boundary_failure_requires_recovery(env, monkeypatch):
    (env.root / "s4").mkdir()
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.release",
        AsyncMock(side_effect=RuntimeError("runner failed")),
    )
    result = await env.runtime.delete_session(channel_id="web", session_id="s4")
    assert not result.ok and result.recovery_required
    assert (
        "participant.failed.True" in env.events and "abort.lifecycle" not in env.events
    )


@pytest.mark.asyncio
async def test_team_session_orders_controller_participant_and_runner(env, monkeypatch):
    (env.root / "team-session").mkdir()
    env.metadata.update(mode="team.work.normal", team_name="team-a")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(unbind_session=lambda **_kwargs: None),
    )
    result = await env.runtime.delete_session(
        channel_id="web", session_id="team-session"
    )
    assert result.ok and result.is_team and result.deleted
    assert (
        env.events.index("team.quiesce")
        < env.events.index("participant.release")
        < env.events.index("team.dispose")
    )
    assert env.events.count("runner.team") == 1
    assert env.events.index("runner.team") < env.events.index("team.committed")


@pytest.mark.asyncio
async def test_team_session_cancellation_after_quiesce_reopens_admission(
    env,
):
    (env.root / "team-cancelled-after-quiesce").mkdir()
    env.metadata.update(mode="team.work.normal", team_name="team-a")
    env.participant.release_resources = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await env.runtime.delete_session(
            channel_id="web",
            session_id="team-cancelled-after-quiesce",
        )

    assert "team.quiesce" in env.events
    assert "team.dispose" not in env.events
    assert "team.aborted" in env.events


@pytest.mark.asyncio
async def test_concurrent_delete_is_idempotent(env):
    (env.root / "same").mkdir()
    first, second = await asyncio.gather(
        env.runtime.delete_session(channel_id="web", session_id="same"),
        env.runtime.delete_session(channel_id="web", session_id="same"),
    )
    assert first.ok and second.ok and first.deleted and second.deleted
    assert env.events.count("runner.release") == 1


@pytest.mark.asyncio
async def test_completed_delete_result_expires_back_to_not_found(env, monkeypatch):
    import jiuwenswarm.runtime.session_provisioner as provisioner_module

    now = 100.0
    monkeypatch.setattr(provisioner_module.time, "monotonic", lambda: now)
    (env.root / "expiring").mkdir()

    deleted = await env.runtime.delete_session(
        channel_id="web",
        session_id="expiring",
    )
    assert (
        await env.runtime.delete_session(
            channel_id="web",
            session_id="expiring",
        )
    ) == deleted

    now += provisioner_module._DELETE_RESULT_CACHE_TTL_SECONDS + 1
    expired = await env.runtime.delete_session(
        channel_id="web",
        session_id="expiring",
    )
    assert expired.ok is False
    assert expired.error_code == "NOT_FOUND"


def test_completed_delete_result_cache_is_bounded():
    import jiuwenswarm.runtime.session_provisioner as provisioner_module

    cache = provisioner_module._RecentDeleteResults[object]()
    for index in range(provisioner_module._DELETE_RESULT_CACHE_MAX_ENTRIES + 10):
        cache[str(index)] = object()

    assert len(cache) == provisioner_module._DELETE_RESULT_CACHE_MAX_ENTRIES
    assert cache.get("0") is None


@pytest.mark.asyncio
async def test_delete_team_releases_every_session_then_deletes_runner_once(
    env, monkeypatch
):
    for session_id in ("a", "b"):
        (env.root / session_id).mkdir()
    env.metadata.update(mode="team.work.normal", team_name="team-all")

    class BindingStore:
        deleted = False

        def get(self, _name):
            return None if self.deleted else SimpleNamespace(session_ids=("a", "b"))

        def unbind_session(self, **_kwargs):
            pass

        def delete(self, _name):
            self.deleted = True
            env.events.append("team.binding.delete")

    class EntityStore:
        deleted = False

        def exists(self, _name):
            return not self.deleted

        def delete_team_directory(self, _name):
            self.deleted = True
            env.events.append("team.entity.delete")

    binding_store, entity_store = BindingStore(), EntityStore()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )
    result = await env.runtime.delete_team(team_name="team-all", channel_id="web")
    assert result.ok and result.deleted and result.session_ids == ("a", "b")
    assert env.events.count("participant.release") == 2
    assert env.events.count("runner.team") == 1
    assert env.events.index("participant.release") < env.events.index("runner.team")
    assert env.events.count("team.committed") == 2
    assert env.events[-2:] == ["team.entity.delete", "team.binding.delete"]

    repeated = await env.runtime.delete_team(team_name="team-all", channel_id="web")
    assert repeated == result
    assert env.events.count("runner.team") == 1


@pytest.mark.asyncio
async def test_team_delete_cancellation_rolls_back_current_participant(
    env,
    monkeypatch,
):
    (env.root / "cancelled").mkdir()
    env.metadata.update(mode="team.work.normal", team_name="team-cancelled")
    env.participant.before_delete = AsyncMock(side_effect=asyncio.CancelledError)
    env.participant.delete_failed = AsyncMock()

    class BindingStore:
        def get(self, _name):
            return SimpleNamespace(session_ids=("cancelled",))

    class EntityStore:
        def exists(self, _name):
            return True

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: BindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: EntityStore(),
    )

    with pytest.raises(asyncio.CancelledError):
        await env.runtime.delete_team(team_name="team-cancelled", channel_id="web")

    env.participant.delete_failed.assert_awaited_once()
    _, kwargs = env.participant.delete_failed.await_args
    assert kwargs == {"destructive_started": False}
