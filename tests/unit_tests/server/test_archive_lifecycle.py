"""Disk-backed lifecycle scenarios; runtime calls are isolated from LLMs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.session import (
    lifecycle as lc,
    project_store,
    session_metadata as sm,
)
from jiuwenswarm.server.runtime.session.session_archive import SessionArchiveService


def test_gateway_recovery_owners_survive_empty_cron_store(tmp_path):
    from jiuwenswarm.gateway.cron.lifecycle_owners import LifecycleOwners

    path = tmp_path / "cron_jobs.json"
    first = LifecycleOwners(path)
    second = LifecycleOwners(path)
    first.remember("alice")
    second.remember("bob")
    assert LifecycleOwners(path).read() == {"", "alice", "bob"}
    assert not path.exists()


@pytest.mark.asyncio
async def test_scheduler_restart_routes_recovery_without_remaining_jobs(
    tmp_path, monkeypatch
):
    from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
    from jiuwenswarm.gateway.cron.store import CronJobStore
    from jiuwenswarm.gateway.routing import e2a_proxy

    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    original = CronSchedulerService(
        store=store, agent_client=None, message_handler=None
    )
    original.remember_lifecycle_owner("alice")
    restarted = CronSchedulerService(
        store=store, agent_client=None, message_handler=None
    )
    owners = []

    async def fetch(**kwargs):
        owners.append(kwargs["user_id"])
        return True, {"projects": []}

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", fetch)
    await restarted.reconcile_project_lifecycles()
    assert "alice" in owners
    assert await store.list_jobs() == []


def test_stale_project_checkpoint_cannot_change_progress(archive):
    service, _, root, _ = archive
    project = project_store.create_project("checkpoint", str(root / "work"))
    operation = lc.begin("project", project.project_id, "archive")
    lc.claim_operation("project", project.project_id, "first")
    old = dict(
        operation_id=operation["operation_id"], generation=operation["generation"]
    )
    current = lc.claim_operation("project", project.project_id, "second")
    with pytest.raises(lc.LifecycleError):
        lc.checkpoint_project(
            project.project_id, {**old, "completed_cron_job_ids": ["stale"]}
        )
    lc.checkpoint_project(
        project.project_id,
        {
            "operation_id": current["operation_id"],
            "generation": current["generation"],
            "completed_cron_job_ids": ["current"],
        },
    )
    assert lc.state("project", project.project_id)["operation"]["completed_items"][
        "cron"
    ] == ["current"]


@pytest.mark.asyncio
async def test_project_restore_rejects_pre_archive_writer(archive):
    service, create, root, _ = archive
    project = project_store.create_project("writer", str(root / "work"))
    directory = create()
    metadata = lc.raw_metadata("sess_a")
    metadata["project_id"] = project.project_id
    lc.atomic_json(directory / "metadata.json", metadata)
    token = await service.project(project.project_id, "archive", "web", {})
    await service.project(
        project.project_id, "archive", "web", {**token, "_lifecycle_stage": "finish"}
    )
    await service.project(project.project_id, "unarchive", "web", {})
    with pytest.raises(lc.LifecycleError):
        lc.write_guard("sess_a", generation=0)
    lc.write_guard("sess_a", generation=lc.state("session", "sess_a")["generation"])


@pytest.mark.asyncio
async def test_interrupted_unarchive_retry_releases_fence(archive):
    service, create, root, _ = archive
    project = project_store.create_project("resume", str(root / "work"))
    directory = create()
    metadata = lc.raw_metadata("sess_a")
    metadata["project_id"] = project.project_id
    lc.atomic_json(directory / "metadata.json", metadata)
    token = await service.project(
        project.project_id, "archive", "web", {"_lifecycle_stage": "prepare"}
    )
    await service.project(
        project.project_id, "archive", "web", {**token, "_lifecycle_stage": "finish"}
    )
    # Simulate a crash after restore_project() but before the lifecycle commit:
    # the project is visible again while its execution fence is still up.
    lc.begin("project", project.project_id, "unarchive")
    project_store.restore_project(project.project_id)
    assert not project_store.get_project_by_id(
        project.project_id, cache_bust=True
    ).hidden
    assert lc.projection("project", project.project_id)["execution_blocked"]
    # Retry must finish the interrupted operation, not return idempotency
    # while leaving the fence in place forever.
    result = await service.project(project.project_id, "unarchive", "web", {})
    assert result["restored"] is False
    state = lc.state("project", project.project_id)
    assert state["operation"]["status"] == "completed"
    assert not lc.projection("project", project.project_id)["execution_blocked"]
    lc.write_guard("sess_a")


@pytest.mark.asyncio
async def test_gateway_session_events_carry_project_id(archive, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.web import lifecycle_handlers

    service, create, root, _ = archive
    create()

    async def fetch(**kwargs):
        params, method = kwargs["params"], kwargs["req_method"].value
        if method == "session.archive":
            results = [
                await service.session(sid, "archive", "web")
                for sid in params["session_ids"]
            ]
            return True, dict(
                succeeded_count=len(results), failed_count=0, results=results
            )
        if method == "session.delete":
            return True, dict(
                session_id=params["session_id"], project_id="default"
            )
        return True, {}

    monkeypatch.setattr(lifecycle_handlers, "fetch_agent_unary", fetch)
    methods = {}
    events = []
    channel = SimpleNamespace(
        channel_id="web",
        clients=[],
        register_method=lambda name, fn: methods.update({name: fn}),
        send_response=AsyncMock(),
        send_event=AsyncMock(
            side_effect=lambda ws, event, payload: events.append((event, payload))
        ),
    )
    lifecycle_handlers.register_lifecycle_handlers(
        channel, lambda: object(), lambda: None
    )
    await methods["session.archive"](
        object(), "req", {"session_ids": ["sess_a"]}, None, "alice"
    )
    # session.archived 直接事件必须符合 §5.10.11 契约：project_id 必返。
    assert len(events) == 1
    event, payload = events[0]
    assert event == "session.archived"
    assert payload["session_id"] == "sess_a"
    assert payload["project_id"] == "default"
    assert payload["archived"] is True
    assert isinstance(payload["archived_at"], float)
    assert payload["stop_pending"] is False
    await methods["session.delete"](
        object(), "req", {"session_id": "sess_a"}, None, "alice"
    )
    event, payload = events[-1]
    assert event == "session.deleted"
    assert payload["session_id"] == "sess_a"
    assert payload["project_id"] == "default"


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    active = root / "sessions"
    active.mkdir(parents=True)
    for module in (lc, project_store):
        monkeypatch.setattr(module, "get_agent_root_dir", lambda: root)
    for module in (lc, sm):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: active)
    import jiuwenswarm.server.runtime.session.session_archive as module

    monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: active)
    project_store.invalidate_cache()
    sm._METADATA_CACHE.clear()
    runtime = SimpleNamespace(
        is_session_running=Mock(return_value=False),
        stop_session_for_archive=AsyncMock(), delete_session=AsyncMock(return_value=SimpleNamespace(ok=True))
    )
    service = SessionArchiveService(runtime)

    def create(sid="sess_a", **meta):
        directory = active / sid
        directory.mkdir()
        lc.atomic_json(
            directory / "metadata.json",
            dict(
                session_id=sid,
                channel_id="web",
                title=sid,
                work_mode="work",
                project_id="default",
                **meta,
            ),
        )
        (directory / "history.json").write_text('[{"content":"keep me"}]')
        return directory

    yield service, create, root, runtime
    project_store.invalidate_cache()
    sm._METADATA_CACHE.clear()


@pytest.mark.asyncio
async def test_idle_archive_restore_preserves_data_and_event_time(archive):
    service, create, root, runtime = archive
    create()
    first = await service.session("sess_a", "archive", "web")
    runtime.stop_session_for_archive.assert_not_awaited()
    assert not (root / "sessions/sess_a").exists()
    assert "keep me" in (root / "sessions_archived/sess_a/history.json").read_text()
    assert (await service.session("sess_a", "archive", "web"))["archived_at"] == first[
        "archived_at"
    ]
    assert service.list_sessions({})["total"] == 1
    with pytest.raises(lc.LifecycleError, match="archived|blocks"):
        lc.guard("sess_a")
    await service.session("sess_a", "unarchive", "web")
    lc.guard("sess_a")
    assert "archived_at" not in lc.raw_metadata("sess_a")
    runtime.stop_session_for_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_moved_directory_recovers_original_timestamp(archive):
    service, create, root, _ = archive
    directory = create()
    operation = lc.begin("session", "sess_a", "archive")
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)
    result = await service.session("sess_a", "archive", "web")
    assert result["archived_at"] == operation["archived_at"]
    assert lc.raw_metadata("sess_a")["archived_at"] == operation["archived_at"]


@pytest.mark.asyncio
async def test_delete_stop_failure_preserves_fence_and_allows_same_operation_retry(archive):
    service, create, root, runtime = archive
    create()
    runtime.stop_session_for_archive.side_effect = lc.LifecycleError(
        "STOP_TIMEOUT", "not stopped"
    )
    with pytest.raises(lc.LifecycleError):
        await service.session("sess_a", "delete", "web")
    original = lc.state("session", "sess_a")["operation"]["operation_id"]
    assert (root / "sessions/sess_a").exists()
    assert lc.projection("session", "sess_a")["execution_blocked"]
    with pytest.raises(lc.LifecycleError):
        await service.session("sess_a", "unarchive", "web")
    runtime.stop_session_for_archive.side_effect = None
    await service.session("sess_a", "delete", "web")
    assert lc.state("session", "sess_a")["operation"]["operation_id"] == original


@pytest.mark.asyncio
async def test_stale_metadata_cannot_recreate_active_directory(archive):
    service, create, root, _ = archive
    create()
    options = sm._MetadataWriteOptions(lifecycle_generation=0)
    await service.session("sess_a", "archive", "web")
    with pytest.raises(lc.LifecycleError):
        sm._write_metadata_sync("sess_a", {"title": "late"}, options)
    assert not (root / "sessions/sess_a").exists()
    await service.session("sess_a", "unarchive", "web")
    with pytest.raises(lc.LifecycleError):
        sm._write_metadata_sync("sess_a", {"title": "late"}, options)
    assert lc.raw_metadata("sess_a")["title"] == "sess_a"


@pytest.mark.asyncio
async def test_archive_flushes_accepted_writes_without_stopping(archive, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history as history

    service, create, root, runtime = archive
    create()
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: root / "sessions")

    sm._enqueue_write(
        "sess_a", {**lc.raw_metadata("sess_a"), "title": "final title"}
    )
    history._enqueue_history_item(
        "sess_a", {"role": "assistant", "content": "final accepted record"}
    )
    await service.session("sess_a", "archive", "web")
    runtime.stop_session_for_archive.assert_not_awaited()
    assert lc.raw_metadata("sess_a")["title"] == "final title"
    archived = root / "sessions_archived/sess_a"
    files = list(archived.glob("history.*"))
    assert any("final accepted record" in path.read_text() for path in files)
    assert not (root / "sessions/sess_a").exists()


@pytest.mark.asyncio
async def test_busy_session_archive_has_no_side_effects(archive):
    service, create, root, runtime = archive
    directory = create()
    before = (directory / "metadata.json").read_bytes()
    runtime.is_session_running.return_value = True
    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_a", "archive", "web")
    assert error.value.code == "SESSION_BUSY"
    assert lc.state("session", "sess_a") == {}
    assert (directory / "metadata.json").read_bytes() == before
    assert not (root / "sessions_archived/sess_a").exists()
    runtime.stop_session_for_archive.assert_not_awaited()
    lc.guard("sess_a")
    runtime.is_session_running.return_value = False
    await service.session("sess_a", "archive", "web")


@pytest.mark.asyncio
async def test_busy_project_checks_legacy_and_cron_sessions_without_fencing(archive):
    service, create, root, runtime = archive
    project = project_store.create_project("busy", str(root / "work"))
    for sid in ("sess_legacy", "cron_running"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta.pop("project_id")
        meta["project_dir"] = project.project_dir
        lc.atomic_json(directory / "metadata.json", meta)
    runtime.is_session_running.side_effect = lambda sid: sid == "cron_running"
    with pytest.raises(lc.LifecycleError) as error:
        await service.project(project.project_id, "archive", "web", {})
    assert error.value.code == "PROJECT_BUSY"
    assert error.value.details["running_session_ids"] == ["cron_running"]
    assert set(service.project_sessions(project.project_id)) == {"sess_legacy", "cron_running"}
    assert lc.state("project", project.project_id) == {}
    assert not project_store.get_project_by_id(project.project_id, cache_bust=True).hidden
    runtime.stop_session_for_archive.assert_not_awaited()
    runtime.is_session_running.side_effect = None
    token = await service.project(project.project_id, "archive", "web", {})
    await service.project(project.project_id, "archive", "web", {**token, "_lifecycle_stage": "finish"})
    assert set(service.project_sessions(project.project_id)) == {"sess_legacy", "cron_running"}
    assert sm.collect_all_sessions_metadata() == []


def test_runtime_running_check_includes_team_without_stopping(monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from jiuwenswarm.agents.harness.team import team_manager

    snapshot = SimpleNamespace(executions=[SimpleNamespace(state=SessionExecutionState.RUNNING)])
    runtime = SimpleNamespace(_session_coordinator=SimpleNamespace(snapshot_session=lambda sid: snapshot))
    monkeypatch.setattr(team_manager, "_team_manager", None)
    assert AgentRuntime.is_session_running(runtime, "sess_a")
    snapshot.executions[0].state = SessionExecutionState.SUCCEEDED
    assert not AgentRuntime.is_session_running(runtime, "sess_a")
    manager = SimpleNamespace(
        has_stream_task=lambda sid: False,
        is_runtime_active=lambda sid: True,
        is_runtime_pending=lambda sid: False,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    assert AgentRuntime.is_session_running(runtime, "sess_a")


@pytest.mark.asyncio
async def test_project_hides_without_explicit_session_archive(archive):
    service, create, root, runtime = archive
    project = project_store.create_project("example", str(root / "user-work"))
    directory = create()
    metadata = lc.raw_metadata("sess_a")
    metadata["project_id"] = project.project_id
    lc.atomic_json(directory / "metadata.json", metadata)
    token = await service.project(
        project.project_id, "archive", "web", {"_lifecycle_stage": "prepare"}
    )
    assert not lc.projection("project", project.project_id)["execution_blocked"]
    # The accepted concurrency boundary: work starting after the check is not
    # cancelled, nor is the prepare stage an execution admission fence.
    runtime.is_session_running.return_value = True
    result = await service.project(
        project.project_id, "archive", "web", {**token, "_lifecycle_stage": "finish"}
    )
    assert result["affected_sessions"] == 1
    runtime.stop_session_for_archive.assert_not_awaited()
    assert service.list_sessions({})["total"] == 0
    assert sm.get_all_sessions_metadata()[1] == 0
    assert sm.collect_all_sessions_metadata() == []
    await service.project(project.project_id, "unarchive", "web", {})
    assert sm.get_all_sessions_metadata()[1] == 1


def test_conflicts_and_batch_validation(archive):
    _, create, root, _ = archive
    create()
    (root / "sessions_archived/sess_a").mkdir(parents=True)
    with pytest.raises(lc.LifecycleError) as error:
        lc.resolve_session("sess_a")
    assert error.value.code == "SESSION_ID_CONFLICT"
    for value in ("../x", "x/y", "x\\y", "NUL", "x:", ""):
        with pytest.raises(lc.LifecycleError):
            lc.validate_id(value)
    assert lc.parse_ids(
        {"session_id": "b", "session_ids": ["a", "b"]}, delete=True
    ) == ["b", "a"]
    for params in ({"session_ids": []}, {"session_ids": "x"}, {"session_ids": [False]}):
        with pytest.raises(lc.LifecycleError):
            lc.parse_ids(params)


@pytest.mark.asyncio
async def test_gateway_archive_disables_cron_without_stopping_or_fencing(
    archive, monkeypatch
):
    from jiuwenswarm.gateway.cron.controller import CronController
    from jiuwenswarm.gateway.channel_manager.web import lifecycle_handlers

    service, create, root, runtime = archive
    project = project_store.create_project("cascade", str(root / "user-work"))
    project_dir = root / "user-work"
    project_dir.mkdir()
    (project_dir / "keep.txt").write_text("user data")
    directory = create()
    meta = lc.raw_metadata("sess_a")
    meta["project_id"] = project.project_id
    lc.atomic_json(directory / "metadata.json", meta)
    events = []
    jobs = {
        "ours": SimpleNamespace(
            id="ours", project_id=project.project_id, user_id="alice", enabled=True
        ),
        "theirs": SimpleNamespace(
            id="theirs", project_id=project.project_id, user_id="bob", enabled=True
        ),
    }

    async def update_job(job_id, patch):
        assert not lc.projection("project", project.project_id)["execution_blocked"]
        jobs[job_id].enabled = patch["enabled"]
        events.append(("disable", job_id))

    async def delete_job(job_id, **kwargs):
        assert not kwargs.get("force")
        events.append(("delete", job_id))
        jobs.pop(job_id, None)
        return True

    store = SimpleNamespace(
        list_jobs=AsyncMock(side_effect=lambda: list(jobs.values())),
        update_job=update_job,
        delete_job=delete_job,
    )
    scheduler = SimpleNamespace(
        reload=AsyncMock(),
        stop_project_runs=AsyncMock(),
        _lifecycle_owners=set(),
        remember_lifecycle_owner=lambda owner: None,
        has_running_project_sessions=Mock(return_value=False),
    )
    controller = CronController(store=store, scheduler=scheduler)

    async def fetch(**kwargs):
        params, method = kwargs["params"], kwargs["req_method"].value
        pid = params["project_id"]
        if method == "project.lifecycle":
            operation = lc.state("project", pid).get("operation")
            if "planned_cron_job_ids" in params:
                lc.update(
                    "project", pid, planned_cron_job_ids=params["planned_cron_job_ids"]
                )
            if "completed_cron_job_ids" in params:
                lc.update(
                    "project",
                    pid,
                    completed_items={"cron": params["completed_cron_job_ids"]},
                )
            return True, dict(operation=operation, **lc.projection("project", pid))
        return True, await service.project(pid, method.split(".")[1], "web", params)

    monkeypatch.setattr(lifecycle_handlers, "fetch_agent_unary", fetch)
    methods = {}
    channel = SimpleNamespace(
        channel_id="web",
        clients=[],
        register_method=lambda name, fn: methods.update({name: fn}),
        send_response=AsyncMock(),
        send_event=AsyncMock(),
    )
    lifecycle_handlers.register_lifecycle_handlers(
        channel, lambda: object(), lambda: controller
    )
    scheduler.has_running_project_sessions.return_value = True
    await methods["project.archive"](
        object(), "busy", {"project_id": project.project_id}, None, "alice"
    )
    assert channel.send_response.call_args.kwargs["code"] == "PROJECT_BUSY"
    assert events == []
    assert lc.state("project", project.project_id) == {}
    scheduler.has_running_project_sessions.return_value = False
    await methods["project.archive"](
        object(), "req", {"project_id": project.project_id}, None, "alice"
    )
    assert events == [("disable", "ours")]
    assert jobs["theirs"].enabled
    assert not jobs["ours"].enabled
    assert project_store.get_project_by_id(project.project_id, cache_bust=True).hidden
    scheduler.stop_project_runs.assert_not_awaited()
    await methods["project.unarchive"](
        object(), "req", {"project_id": project.project_id}, None, "alice"
    )
    assert not jobs["ours"].enabled
    assert (project_dir / "keep.txt").read_text() == "user data"
    await asyncio.sleep(0)  # allow the no-client watcher to exit


@pytest.mark.asyncio
async def test_project_delete_retries_preserve_counts_and_user_directory(archive):
    import shutil

    service, create, root, runtime = archive
    user_dir = root / "user-work"
    user_dir.mkdir()
    (user_dir / "keep.txt").write_text("keep")
    project = project_store.create_project("deletion", str(user_dir))
    for sid in ("sess_a", "sess_b"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta["project_id"] = project.project_id
        lc.atomic_json(directory / "metadata.json", meta)
    await service.session("sess_b", "archive", "web")
    token = await service.project(
        project.project_id, "archive", "web", {"_lifecycle_stage": "prepare"}
    )
    await service.project(
        project.project_id, "archive", "web", {**token, "_lifecycle_stage": "finish"}
    )
    failures = {"sess_b"}

    async def delete_session(*, channel_id, session_id):
        if session_id in failures:
            return SimpleNamespace(
                ok=False, error_code="DELETE_FAILED", error_message="retry"
            )
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    token = await service.project(
        project.project_id, "delete", "web", {"_lifecycle_stage": "prepare"}
    )
    with pytest.raises(lc.LifecycleError) as error:
        await service.project(
            project.project_id,
            "delete",
            "web",
            {**token, "_lifecycle_stage": "finish", "deleted_cron_jobs": 2},
        )
    assert error.value.code == "PARTIAL_PROJECT_DELETE_FAILED"
    assert project_store.get_project_by_id(project.project_id, cache_bust=True)
    failures.clear()
    result = await service.project(
        project.project_id,
        "delete",
        "web",
        {**token, "_lifecycle_stage": "finish", "deleted_cron_jobs": 2},
    )
    assert result["deleted_sessions"] == 2
    assert result["deleted_cron_jobs"] == 2
    assert await service.project(project.project_id, "delete", "web", {}) == result
    assert (user_dir / "keep.txt").read_text() == "keep"
