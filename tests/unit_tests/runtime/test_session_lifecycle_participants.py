from __future__ import annotations

from dataclasses import dataclass

import pytest

from jiuwenswarm.runtime.session_lifecycle import (
    RuntimeParticipantRegistry,
    SessionDescriptor,
    SessionKind,
    SessionLifecycleTarget,
)
from jiuwenswarm.runtime.session_delete import TeamDeletionGate
from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner import (
    KVCacheApplicationOwner,
    KVCacheApplicationState,
)
from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_session_lifecycle_participant import (
    KVCacheSessionLifecycleParticipant,
)


@dataclass
class _Runtime:
    closed: bool = False


def _target(kind: SessionKind = SessionKind.AGENT) -> SessionLifecycleTarget:
    return SessionLifecycleTarget(
        descriptor=SessionDescriptor(
            session_id="session-a",
            channel_id="web",
            mode="team" if kind is SessionKind.TEAM else "agent.plan",
            work_mode="work",
        ),
        kind=kind,
        team_name="team-a" if kind is SessionKind.TEAM else "",
    )


@pytest.mark.asyncio
async def test_owner_off_on_off_on_keeps_terminal_delete_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enabled = False
    runtime = _Runtime()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "is_kv_cache_affinity_enabled",
        lambda: enabled,
    )
    runtime_lookups: list[str] = []

    def get_runtime() -> _Runtime:
        runtime_lookups.append("get")
        return runtime

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime."
        "get_kv_cache_runtime",
        get_runtime,
    )

    owner = KVCacheApplicationOwner()
    registry = RuntimeParticipantRegistry()
    lease = owner.attach_runtime(registry)
    assert owner.state is KVCacheApplicationState.DISABLED
    assert registry.snapshot_delete() == ()
    assert runtime_lookups == []

    enabled = True
    await owner.activate_from_config()
    installed = registry.snapshot_delete()
    assert owner.state is KVCacheApplicationState.ENABLED
    assert runtime_lookups == ["get"]
    assert len(installed) == 1
    assert registry.snapshot_activity() == installed

    await owner.set_enabled(False)
    assert owner.state is KVCacheApplicationState.TERMINAL_ONLY
    assert registry.snapshot_activity() == ()
    assert registry.snapshot_delete() == installed

    await owner.set_enabled(True)
    assert owner.state is KVCacheApplicationState.ENABLED
    assert registry.snapshot_delete() == installed
    assert registry.snapshot_activity() == installed

    await owner.suspend()
    assert owner.state is KVCacheApplicationState.SUSPENDED
    assert registry.snapshot_activity() == ()
    assert registry.snapshot_delete() == ()

    await owner.set_enabled(True)
    assert owner.state is KVCacheApplicationState.ENABLED
    assert registry.snapshot_activity() == installed
    assert registry.snapshot_delete() == installed

    await lease.release()
    await lease.release()
    assert registry.snapshot_delete() == ()
    assert owner.state is KVCacheApplicationState.ENABLED


@pytest.mark.asyncio
async def test_delete_participant_release_is_independent_of_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Guard:
        def delete(self, **_kwargs) -> None:
            events.append("before")
            raise RuntimeError("partial tombstone")

        def restore_after_failed_delete(self, _session_id: str) -> None:
            events.append("restore")

        def forget(self, _session_id: str) -> None:
            events.append("forget")

    class _Session:
        async def release_kvc(self) -> bool:
            events.append("release")
            return True

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard."
        "get_session_kv_cache_task_guard",
        lambda: _Guard(),
    )
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        lambda **_kwargs: _Session(),
    )
    participant = KVCacheSessionLifecycleParticipant(_Runtime())

    with pytest.raises(RuntimeError, match="partial tombstone"):
        await participant.before_delete(_target())
    await participant.release_resources(_target())
    await participant.delete_failed(_target(), destructive_started=False)
    await participant.delete_failed(_target(), destructive_started=True)
    await participant.delete_committed(_target())

    assert events == ["before", "release", "restore", "forget"]


@pytest.mark.asyncio
async def test_team_deletion_gate_serializes_mutation_and_rejects_bind() -> None:
    gate = TeamDeletionGate()
    lock = gate.mutation_lock("team-a")
    async with lock:
        assert gate.begin_delete_locked("team-a") is True
        with pytest.raises(RuntimeError, match="TEAM_DELETING"):
            gate.assert_not_deleting_locked("team-a")
        assert gate.begin_delete_locked("team-a") is False
        gate.finish_delete_locked("team-a")
        gate.assert_not_deleting_locked("team-a")
