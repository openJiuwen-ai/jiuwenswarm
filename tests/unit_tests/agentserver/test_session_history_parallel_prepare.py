# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""History I/O overlaps assembly and drains before its session lock is released."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from tests.unit_tests.test_context_snapshots import stored as snapshot_storage, user

stored = snapshot_storage


def router(monkeypatch, child):
    value = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    value._is_session_scoped_adapter = False
    value._session_adapter_locks = {}
    value._session_adapters = {}
    value._session_instance_config = {}
    value._session_instance_mode = "agent"
    value._session_instance_sub_mode = None
    monkeypatch.setattr(value, "_new_session_scoped_adapter", lambda _sid: child)
    monkeypatch.setattr(value, "_load_skill_retrieval_session_profile", lambda _sid: None)
    monkeypatch.setattr(value, "_session_instance_extra_create_kwargs", lambda: {})
    monkeypatch.setattr(value, "_touch_session_adapter", lambda _sid: None)
    monkeypatch.setattr(value, "_reload_session_adapter_if_stale", AsyncMock())
    return value


def child_adapter():
    return SimpleNamespace(restore_skill_retrieval_session=Mock(),
        configure_initial_execution=Mock(),
        persist_skill_retrieval_session_profile=Mock(), create_instance=AsyncMock(),
        start_interaction=AsyncMock(), cleanup=AsyncMock(), _instance=object())


@pytest.mark.asyncio
async def test_history_read_overlaps_creation_and_restores_after_config_join(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    child = child_adapter()
    parent = router(monkeypatch, child)
    snapshot = object()
    stages = []

    def read(_sid, **_kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return snapshot

    async def create(*_args, **_kwargs):
        assert await asyncio.to_thread(entered.wait, 5)
        assert parent._session_adapter_locks["session-parallel"].locked()
        assert not finished.is_set()
        stages.append("create")
        release.set()

    async def start(**_kwargs):
        stages.append("start")

    async def reload(*_args, **_kwargs):
        stages.append("reload")

    async def restore(**kwargs):
        assert finished.is_set() and stages == ["create", "start", "reload"]
        assert kwargs["history_snapshot"] is snapshot
        assert kwargs["history_before_request_id"] == "one-input"
        assert parent._session_adapter_locks["session-parallel"].locked()
        stages.append("context")

    child.create_instance = create
    child.start_interaction = start
    monkeypatch.setattr(parent, "_reload_session_adapter_if_stale", reload)
    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    assert await parent._get_or_create_session_adapter("session-parallel",
        history_before_request_id="one-input") is child
    assert stages == ["create", "start", "reload", "context"]
    assert not parent._session_adapter_locks["session-parallel"].locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "assembly"])
async def test_failed_or_cancelled_assembly_drains_read_inside_session_lock(monkeypatch, failure):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    assembly_entered = asyncio.Event()
    child = child_adapter()
    parent = router(monkeypatch, child)

    def read(_sid, **_kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return None

    async def create(*_args, **_kwargs):
        assert await asyncio.to_thread(entered.wait, 5)
        assembly_entered.set()
        if failure == "assembly":
            raise RuntimeError("required rail failed")
        await asyncio.Event().wait()

    child.create_instance = create
    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    restore = AsyncMock()
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    task = asyncio.create_task(parent._get_or_create_session_adapter("session-drain",
        history_before_request_id="one-input"))
    await asyncio.wait_for(assembly_entered.wait(), 5)
    if failure == "cancel":
        task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done() and parent._session_adapter_locks["session-drain"].locked()
    if failure == "cancel":
        task.cancel()  # A repeated cancellation cannot bypass the drain.
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await task
    assert finished.is_set() and not parent._session_adapter_locks["session-drain"].locked()
    child.start_interaction.assert_not_awaited()
    restore.assert_not_awaited()
    assert parent._session_adapters == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation", ["none", "error", "no-cutoff"])
async def test_unavailable_pre_read_preserves_original_warmup_call(monkeypatch, preparation):
    child = child_adapter()
    parent = router(monkeypatch, child)

    def read(_sid, **_kwargs):
        if preparation == "no-cutoff":
            pytest.fail("create/switch preparation must keep the original transaction")
        if preparation == "error":
            raise OSError("disk unavailable")
        return None

    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    restore = AsyncMock()
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    boundary = None if preparation == "no-cutoff" else "one-input"
    assert await parent._get_or_create_session_adapter("session-fallback",
        history_before_request_id=boundary) is child
    assert restore.await_args.kwargs == {"deep_agent": child._instance,
        "session_id": "session-fallback", "history_before_request_id": boundary}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prefetch", "install"])
async def test_restore_failure_propagates_and_discards_the_unusable_child(monkeypatch, stage):
    from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

    child = child_adapter()
    parent = router(monkeypatch, child)

    def read(*_args, **_kwargs):
        if stage == "prefetch":
            raise HistoryReplayError("history budget exceeded", code="HISTORY_RESTORE_LIMIT")
        return None

    restore = AsyncMock(side_effect=HistoryReplayError("context install failed"))
    monkeypatch.setattr(session_ops_service, "prefetch_session_history", read)
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    for attempt in range(2):
        with pytest.raises(HistoryReplayError):
            await parent._get_or_create_session_adapter("failed-restore", history_before_request_id="input")
        assert parent._session_adapters == {}
        assert child.cleanup.await_count == attempt + 1
    if stage == "prefetch":
        restore.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "cutoff", "rewrite", "generation", "checkpoint"])
async def test_overlapping_cold_builds_share_only_the_same_structural_failure(stored, monkeypatch, change):
    from openjiuwen.core.context_engine import ContextEngine
    from jiuwenswarm.server.runtime.session import context_migration as migration
    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
    from jiuwenswarm.server.runtime.session import lifecycle as lc
    from jiuwenswarm.server.runtime.session.recovery_budget import LegacyGroupLimit, _assemblies

    child = child_adapter()
    parent = router(monkeypatch, child)
    engine = ContextEngine()
    child._instance = SimpleNamespace(react_agent=SimpleNamespace(
        context_engine=engine, _config=SimpleNamespace(context_processors=[]),
    ))
    monkeypatch.setattr(session_ops_service, "resolve_live_agent_session", lambda *_args: stored.session)
    monkeypatch.setattr(migration, "_MAX_CONTEXT_BYTES", 512)
    stored.write([user("large", "x" * 1024),
                  {"role": "assistant", "event_type": "context.compact_summary"}, user("current")])
    ready, proceed = asyncio.Event(), asyncio.Event()
    attempts = []
    migrate = migration.migrate_history_context

    async def run_migration(**kwargs):
        attempts.append(kwargs["before_request_id"])
        ready.set()
        await proceed.wait()
        return await migrate(**kwargs)

    monkeypatch.setattr(migration, "migrate_history_context", run_migration)

    follower_cleanup, release_follower = asyncio.Event(), asyncio.Event()

    async def cleanup():
        if change == "none" and asyncio.current_task() is second:
            follower_cleanup.set()
            await release_follower.wait()
        if change == "rewrite":
            stored.write([user("small", "repaired"), user("current")])
        elif change == "generation":
            with lc.resource_lock("session", stored.sid):
                value = lc.state("session", stored.sid)
                value["generation"] = int(value.get("generation", 0)) + 1
                lc.save_locked("session", stored.sid, value)
        elif change == "checkpoint":
            stored.session.update_state({snapshots.SNAPSHOT_KEY: {"file": "new-checkpoint"}})
            monkeypatch.setattr(snapshots, "restore_checkpoint_context", AsyncMock(return_value=True))

    child.cleanup = AsyncMock(side_effect=cleanup)

    async def build(boundary):
        return await parent._get_or_create_session_adapter(stored.sid, history_before_request_id=boundary)

    first = asyncio.create_task(build("current"))
    await asyncio.wait_for(ready.wait(), 3)
    second = asyncio.create_task(build("other" if change == "cutoff" else "current"))
    await asyncio.sleep(0)
    proceed.set()
    if change == "none":
        # The third caller overlaps the follower, after the original build exited.
        await asyncio.wait_for(follower_cleanup.wait(), 3)
        third = asyncio.create_task(build("current"))
        await asyncio.sleep(0)
        release_follower.set()
        with pytest.raises(LegacyGroupLimit) as chained:
            await third
    leader, follower = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(leader, LegacyGroupLimit) and leader.source is not None
    if change in {"rewrite", "checkpoint"}:
        assert follower is child
    else:
        assert isinstance(follower, LegacyGroupLimit)
        assert follower.source is not None
        if change == "none":
            assert chained.value.source is follower.source is leader.source
            assert chained.value is not follower and follower is not leader
        else:
            assert follower.source != leader.source
    assert len(attempts) == (2 if change in {"cutoff", "generation"} else 1)
    assert parent._session_adapter_locks[stored.sid] not in _assemblies
    if change == "none":
        # Even an identical serial request gets a fresh attempt; a repaired file can then recover.
        with pytest.raises(LegacyGroupLimit) as serial:
            await build("current")
        assert serial.value.source is not None and len(attempts) == 2
        stored.write([user("small", "repaired"), user("current")])
        assert await build("current") is child
