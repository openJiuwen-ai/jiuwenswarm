# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recovery uses durable intent, not old task counters or a replayed query."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime import team_recovery as recovery
from jiuwenswarm.server.runtime.session import lifecycle


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "get_agent_root_dir", lambda: tmp_path / "agent")
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: tmp_path / "agent" / "sessions")
    monkeypatch.setattr(recovery, "deployment_scope", lambda: "installation-a")
    root = tmp_path / "agent" / "sessions" / "session-a"
    root.mkdir(parents=True)
    (root / "metadata.json").write_text('{"team_name":"team-a"}', encoding="utf-8")
    return {"channel_id": "web", "team_name": "team-a", "mode": "team.code.normal"}


def orphan(session_id="session-a"):
    with lifecycle.resource_lock("session", session_id):
        value = lifecycle.state("session", session_id)
        value["team_recovery"]["owner"] = {"pid": 2147483647, "started_at": 1}
        value.pop("runtime_owner", None)
        lifecycle.save_locked("session", session_id, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime,application,expected", [
    ("persistent", False, 1), ("temporary", False, 0), ("persistent", True, 0),
])
async def test_only_normal_persistent_teams_arm_automatic_recovery(monkeypatch, lifetime, application, expected):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers as helpers
    from jiuwenswarm.agents.harness.team import team_manager

    monkeypatch.setattr(team_manager, "sync_team_observability", lambda: None)
    monkeypatch.setattr(helpers, "get_session_metadata", lambda *a, **k: {"mode": "team.code.normal"})
    monkeypatch.setattr(helpers, "increment_session_round_count", lambda sid: 1)
    begin = Mock(return_value="test-token")
    monkeypatch.setattr(recovery, "begin", begin)
    tasks = []
    monkeypatch.setattr(helpers, "_register_team_stream_task", lambda manager, sid, task, **k: tasks.append(task))

    async def consume(*args, **kwargs):
        pass

    monkeypatch.setattr(helpers, "_consume_stream_with_query", consume)
    manager = SimpleNamespace(prepare_runtime_activation=AsyncMock())
    spec = SimpleNamespace(team_name="original", lifecycle=lifetime, metadata={"app_role_team": application})
    await helpers._start_team_stream_round(
        channel_id="web", session_id="original", request_id="real-user", team_manager=manager,
        team_name="original", team_spec=spec, query="original task", attach_waiter=False,
    )
    await asyncio.gather(*tasks)
    assert begin.call_count == expected


@pytest.mark.asyncio
async def test_orphan_is_automatically_taken_over_without_a_user_request(isolated):
    token = recovery.begin("session-a", isolated)
    orphan()
    received = []

    async def restore(session_id, record):
        received.append((session_id, record))
        recovery.observe(session_id, record["token"], {"event_type": "team.recovered"})

    service = recovery.TeamRecoveryService(restore)
    await service.recover_once()
    await service.recover_once()
    assert len(received) == 1
    assert received[0][1]["token"] != token
    assert recovery.snapshot("session-a")["state"] == "restored"
    assert "query" not in json.dumps(received)


@pytest.mark.asyncio
async def test_two_recovery_scanners_cannot_acquire_the_same_team(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    restore = AsyncMock()
    await asyncio.gather(*[recovery.TeamRecoveryService(restore).recover_once() for _ in range(2)])
    assert restore.await_count == 1


@pytest.mark.asyncio
async def test_cancelling_recovery_service_is_not_recorded_as_an_admission_failure(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    restore = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await recovery.TeamRecoveryService(restore).recover_once()
    assert recovery.snapshot("session-a")["state"] == "recovering"
    assert "error" not in recovery.snapshot("session-a")


@pytest.mark.parametrize("state", ["paused", "cancelled"])
def test_explicit_stop_fences_an_already_claimed_recovery(isolated, state):
    recovery.begin("session-a", isolated)
    orphan()
    record = recovery.claim("session-a")
    recovery.stop("session-a", state)
    with pytest.raises(RuntimeError, match="stopped"):
        recovery.begin("session-a", isolated, token=record["token"])
    recovery.observe("session-a", record["token"], {"event_type": "chat.tool_call"})
    recovery.failed("session-a", record["token"], RuntimeError("late failure"))
    assert recovery.snapshot("session-a")["state"] == state
    assert recovery.claim("session-a") is None


@pytest.mark.parametrize("kind,expected", [
    ("team.idle", "idle"), ("team.completed", "completed"),
    ("chat.ask_user_question", "waiting_for_input"), ("chat.error", "failed"),
])
def test_terminal_or_waiting_teams_do_not_automatically_resume(isolated, kind, expected):
    token = recovery.begin("session-a", isolated)
    recovery.observe("session-a", token, {"event_type": kind})
    orphan()
    assert recovery.claim("session-a") is None
    assert recovery.snapshot("session-a")["state"] == expected


def test_old_stream_cannot_change_a_new_submission(isolated):
    old = recovery.begin("session-a", isolated)
    new = recovery.begin("session-a", isolated)
    recovery.observe("session-a", old, {"event_type": "team.completed"})
    assert recovery.snapshot("session-a")["state"] == "running"
    assert old != new


def test_stale_stop_token_cannot_cancel_a_new_submission(isolated):
    old = recovery.begin("session-a", isolated)
    current = recovery.begin("session-a", isolated)
    before = lifecycle.state("session", "session-a")
    assert recovery.stop("session-a", "cancelled", token=old) is False
    assert lifecycle.state("session", "session-a") == before
    assert recovery.stop("session-a", "paused", token=current) is True


def test_old_process_cannot_stop_the_new_runtime_owner(isolated, monkeypatch):
    recovery.begin("session-a", isolated)
    value = lifecycle.state("session", "session-a")
    value["runtime_owner"] = {"pid": lifecycle.current_runtime_owner()["pid"] + 1, "started_at": 1}
    lifecycle.atomic_json(lifecycle.resource_path("session", "session-a"), value)
    monkeypatch.setattr(lifecycle, "runtime_owner_alive", lambda owner: True)
    before = lifecycle.state("session", "session-a")
    with pytest.raises(lifecycle.LifecycleError, match="another live AgentServer"):
        recovery.stop("session-a", "cancelled")
    assert lifecycle.state("session", "session-a") == before


@pytest.mark.parametrize("deleted", [False, True])
def test_stop_does_not_rewrite_a_lifecycle_barrier(isolated, deleted):
    recovery.begin("session-a", isolated)
    lifecycle.begin("session", "session-a", "delete" if deleted else "archive")
    lifecycle.fence_writes("session", "session-a")
    before = lifecycle.state("session", "session-a")
    assert recovery.stop("session-a", "paused") is False
    assert lifecycle.state("session", "session-a") == before
    assert recovery.candidates() == []


def test_lifecycle_drain_withdraws_recovery_before_isolating_writes(isolated):
    recovery.begin("session-a", isolated)
    operation = lifecycle.begin("session", "session-a", "archive")
    assert recovery.stop("session-a", "paused") is True
    value = lifecycle.state("session", "session-a")
    assert value["blocked"] is True
    assert value["operation"]["operation_id"] == operation["operation_id"]
    assert value["team_recovery"]["desired"] is False


def test_pending_orphan_can_be_stopped_without_a_live_team(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    assert recovery.stop("session-a", "cancelled") is True
    assert recovery.claim("session-a") is None


def test_live_owner_and_other_installation_are_not_stolen(isolated, monkeypatch):
    recovery.begin("session-a", isolated)
    assert recovery.claim("session-a") is None
    orphan()
    monkeypatch.setattr(recovery, "deployment_scope", lambda: "installation-b")
    assert recovery.claim("session-a") is None
    assert recovery.candidates() == []


def test_missing_or_archived_session_is_not_recreated(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    with lifecycle.resource_lock("session", "session-a"):
        value = lifecycle.state("session", "session-a")
        value.update(blocked=True, deleted=True)
        lifecycle.save_locked("session", "session-a", value)
    with pytest.raises(lifecycle.LifecycleError):
        recovery.claim("session-a")


@pytest.mark.asyncio
async def test_failed_admission_has_backoff_and_persistent_limit(isolated, monkeypatch):
    recovery.begin("session-a", isolated)
    orphan()
    clock = [1000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    restore = AsyncMock(side_effect=RuntimeError("checkpoint unreadable"))
    service = recovery.TeamRecoveryService(restore)
    for _ in range(3):
        await service.recover_once()
        await service.recover_once()
        clock[0] += 100
    assert restore.await_count == 3
    assert recovery.snapshot("session-a")["state"] == "recovery_failed"
    await recovery.TeamRecoveryService(restore).recover_once()
    assert restore.await_count == 3


def test_stable_execution_resets_consecutive_failures(isolated, monkeypatch):
    recovery.begin("session-a", isolated)
    orphan()
    record = recovery.claim("session-a")
    monkeypatch.setattr(recovery.time, "time", lambda: record["started_at"] + 601)
    recovery.observe("session-a", record["token"], {"event_type": "chat.tool_call"})
    assert recovery.snapshot("session-a")["attempts"] == 0


def test_recovered_idle_team_gets_at_most_one_wakeup_and_stops_win(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    record = recovery.claim("session-a")
    recovery.observe("session-a", record["token"], {"event_type": "team.recovered"})
    recovery.observe("session-a", record["token"], {"event_type": "team.idle"})
    assert recovery.prepare_wakeup("session-a", record["token"]) is True
    assert recovery.prepare_wakeup("session-a", record["token"]) is False
    recovery.stop("session-a", "paused")
    assert recovery.prepare_wakeup("session-a", record["token"]) is False


def test_runtime_ready_does_not_clear_a_restored_permission_wait(isolated):
    token = recovery.begin("session-a", isolated)
    recovery.observe("session-a", token, {"event_type": "chat.ask_user_question"})
    recovery.observe("session-a", token, {"event_type": "team.recovered"})
    recovery.observe("session-a", token, {"event_type": "team.idle"})
    assert recovery.snapshot("session-a")["state"] == "waiting_for_input"
    assert recovery.prepare_wakeup("session-a", token) is False


@pytest.mark.asyncio
async def test_sdk_cold_start_has_no_user_query_and_keeps_original_identity(monkeypatch, isolated):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers as helpers
    from openjiuwen.agent_teams.runtime import metadata
    from openjiuwen.core.session.checkpointer import CheckpointerFactory

    recovery.begin("session-a", isolated)
    orphan()
    record = recovery.claim("session-a")
    spec = SimpleNamespace(team_name="team-a")
    manager = SimpleNamespace(
        get_startup_lock=lambda sid: asyncio.Lock(), session_has_runtime=AsyncMock(return_value=False),
        get_swarm_enriched_team_spec=AsyncMock(return_value=spec),
        get_team_agent=lambda sid: SimpleNamespace(has_pending_interrupt=lambda: False, is_agent_running=lambda: True),
        get_stream_task=lambda sid: None,
    )
    monkeypatch.setattr(helpers, "get_team_manager", lambda channel: manager)
    monkeypatch.setattr(helpers, "get_session_metadata", lambda *a, **k: {"team_name": "team-a"})
    monkeypatch.setattr(helpers, "create_agent_team_session", lambda **k: SimpleNamespace(pre_run=AsyncMock()))
    monkeypatch.setattr(CheckpointerFactory, "get_checkpointer", lambda: SimpleNamespace(session_exists=AsyncMock(return_value=True)))
    monkeypatch.setattr(metadata, "read_team_namespace", lambda *a: {"leader": "leader"})
    monkeypatch.setattr(helpers, "_resolve_session_swarmflow_config", lambda *a, **k: None)
    calls = []

    async def start(**kwargs):
        calls.append(kwargs)
        recovery.observe("session-a", record["token"], {"event_type": "team.recovered"})

    monkeypatch.setattr(helpers, "_start_team_stream_round", start)
    await helpers.recover_interrupted_team("session-a", record)
    assert len(calls) == 1
    assert calls[0]["resume_only"] is True
    assert calls[0]["query"] is None
    assert calls[0]["team_name"] == "team-a"
    assert calls[0]["attach_waiter"] is False


@pytest.mark.asyncio
async def test_missing_checkpoint_never_creates_a_fresh_team(monkeypatch, isolated):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers as helpers
    from openjiuwen.core.session.checkpointer import CheckpointerFactory

    recovery.begin("session-a", isolated)
    orphan()
    record = recovery.claim("session-a")
    manager = SimpleNamespace(get_startup_lock=lambda sid: asyncio.Lock(), session_has_runtime=AsyncMock(return_value=False))
    monkeypatch.setattr(helpers, "get_team_manager", lambda channel: manager)
    monkeypatch.setattr(helpers, "get_session_metadata", lambda *a, **k: {"team_name": "team-a"})
    monkeypatch.setattr(helpers, "create_agent_team_session", lambda **k: SimpleNamespace(pre_run=AsyncMock()))
    monkeypatch.setattr(CheckpointerFactory, "get_checkpointer", lambda: SimpleNamespace(session_exists=AsyncMock(return_value=False)))
    start = AsyncMock()
    monkeypatch.setattr(helpers, "_start_team_stream_round", start)
    with pytest.raises(RuntimeError, match="checkpoint is missing"):
        await helpers.recover_interrupted_team("session-a", record)
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_stream_is_owned_by_coordinator_and_user_cancel_stops_it(isolated):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
    from jiuwenswarm.runtime.session.model import SessionWorkKind

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._closed = False
    runtime._started = True
    runtime._session_coordinator = RuntimeSessionCoordinator()
    runtime._team_recovery_consumers = set()
    runtime._session_runtime_locks = {}
    runtime._session_run_end_tasks = {}
    runtime.resolve_manager = AsyncMock(return_value=SimpleNamespace())
    stopped = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def restore(generation):
        from jiuwenswarm.runtime.context import get_current_runtime

        assert get_current_runtime() is runtime
        assert generation > 0
        return asyncio.create_task(work())

    try:
        await runtime.recover_team_execution("session-a", "web", "automatic-recovery", restore)
        snapshot = runtime._session_coordinator.snapshot_session("session-a")
        active = [entry for entry in snapshot.executions if not entry.state.terminal]
        assert len(active) == 1 and active[0].work_kind is SessionWorkKind.TEAM_BACKGROUND
        await runtime._session_coordinator.cancel_execution("session-a")
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        await runtime._session_coordinator.close()
        await asyncio.gather(*runtime._team_recovery_consumers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["empty", "error", "cancelled"])
async def test_recovery_admission_settles_when_coordinator_never_yields(ending):
    from jiuwenswarm.runtime.service import AgentRuntime, RuntimeStateError

    async def stream(*args, **kwargs):
        if ending == "error":
            raise RuntimeError("admission refused")
        if ending == "cancelled":
            raise asyncio.CancelledError()
        for event in ():
            yield event

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._closed = False
    runtime._started = True
    runtime._register_session = AsyncMock()
    runtime.resolve_manager = AsyncMock(return_value=SimpleNamespace())
    runtime._session_coordinator = SimpleNamespace(run_team_stream=stream)
    runtime._team_recovery_consumers = set()
    restore = AsyncMock()
    expected = {"empty": RuntimeStateError, "error": RuntimeError, "cancelled": asyncio.CancelledError}
    with pytest.raises(expected[ending]):
        await asyncio.wait_for(
            runtime.recover_team_execution("session-a", "web", "recover", restore), timeout=1,
        )
    await asyncio.sleep(0)
    assert not runtime._team_recovery_consumers
    restore.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_pending_recovery_admission_joins_the_consumer():
    from jiuwenswarm.runtime.service import AgentRuntime

    entered = asyncio.Event()
    closed = asyncio.Event()

    async def stream(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield None
        finally:
            closed.set()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._closed = False
    runtime._started = True
    runtime._register_session = AsyncMock()
    runtime.resolve_manager = AsyncMock(return_value=SimpleNamespace())
    runtime._session_coordinator = SimpleNamespace(run_team_stream=stream)
    runtime._team_recovery_consumers = set()
    task = asyncio.create_task(runtime.recover_team_execution("session-a", "web", "recover", AsyncMock()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not runtime._team_recovery_consumers


def test_task_references_keep_human_scope_without_audit_or_automation(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import session_history as history

    path = tmp_path / "history.jsonl"
    rows = [
        {"role": "user", "content": "Original scope"},
        {"role": "assistant", "content": "Audit chatter"},
        {"role": "user", "content": "Timer", "metadata": {"automation": {"kind": "heartbeat"}}},
        {"role": "user", "content": "Later constraint"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setattr(history, "get_read_history_path", lambda sid: path)
    assert json.loads(recovery.recovery_instructions("original")) == ["Original scope", "Later constraint"]
    path.write_text(json.dumps({"role": "user", "content": "x" * 65537}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="budget"):
        recovery.recovery_instructions("original")
    path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable"):
        recovery.recovery_instructions("original")


def test_recovery_public_failure_does_not_copy_exception_secrets(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    record = recovery.claim("session-a")
    recovery.failed("session-a", record["token"], ValueError("api_key=private-test-secret"))
    assert "private-test-secret" not in json.dumps(recovery.snapshot("session-a"))


@pytest.mark.asyncio
async def test_recovery_admissions_are_bounded_and_one_failure_does_not_block_others(isolated, monkeypatch):
    names = [f"session-{i}" for i in range(4)]
    for name in names:
        recovery.begin(name, isolated)
        orphan(name)
    active = 0
    maximum = 0
    finished = []

    async def restore(sid, record):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(.02)
            if sid == names[0]:
                raise RuntimeError("broken checkpoint")
            finished.append(sid)
        finally:
            active -= 1

    await recovery.TeamRecoveryService(restore).recover_once()
    assert maximum == 2 and len(finished) == 3
    assert recovery.snapshot(names[0])["state"] == "retrying"


@pytest.mark.asyncio
async def test_recovery_state_reaches_page_without_a_chat_waiter(monkeypatch):
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from jiuwenswarm.server.runtime import group_execution

    manager = TeamManager()
    event = {"event_type": "team.recovery", "recovery": {"state": "recovering"}}
    monkeypatch.setattr(group_execution, "project_group_stream_event", AsyncMock(side_effect=lambda sid, item: item))
    manager._push_event_without_waiter = AsyncMock()
    await manager.broadcast_event("original", event, channel_id="web")
    manager._push_event_without_waiter.assert_awaited_once_with("original", event, channel_id="web")


@pytest.mark.asyncio
async def test_repeated_worker_deaths_exhaust_budget_with_a_visible_failure(isolated):
    recovery.begin("session-a", isolated)
    orphan()
    with lifecycle.resource_lock("session", "session-a"):
        value = lifecycle.state("session", "session-a")
        value["team_recovery"]["attempts"] = 3
        lifecycle.save_locked("session", "session-a", value)
    restore = AsyncMock()
    visible = []

    async def publish(sid, record):
        visible.append(recovery.snapshot(sid))

    await recovery.TeamRecoveryService(restore, publish).recover_once()
    restore.assert_not_awaited()
    assert visible[0]["state"] == "recovery_failed"
