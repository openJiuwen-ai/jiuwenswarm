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
    operation = lc.begin("project", project.project_id, "delete")
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
            return True, dict(session_id=params["session_id"], project_id="default")
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
        stop_session_for_archive=AsyncMock(),
        delete_session=AsyncMock(return_value=SimpleNamespace(ok=True)),
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
async def test_delete_stop_failure_preserves_fence_and_allows_same_operation_retry(
    archive,
):
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

    sm._enqueue_write("sess_a", {**lc.raw_metadata("sess_a"), "title": "final title"})
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


def test_runtime_running_check_includes_team_without_stopping(monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from jiuwenswarm.agents.harness.team import team_manager

    snapshot = SimpleNamespace(
        executions=[SimpleNamespace(state=SessionExecutionState.RUNNING)]
    )
    runtime = SimpleNamespace(
        _session_coordinator=SimpleNamespace(snapshot_session=lambda sid: snapshot)
    )
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
async def test_gateway_direct_delete_isolates_cron_owner(archive, monkeypatch):
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
        assert lc.projection("project", project.project_id)["execution_blocked"]
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
    import shutil

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    assert (
        not {"project.archive", "project.unarchive", "project.archived.list"}
        & methods.keys()
    )
    await methods["project.delete"](
        object(), "req", {"project_id": project.project_id}, None, "alice"
    )
    assert channel.send_response.call_args.kwargs["ok"]
    assert events == [("disable", "ours"), ("delete", "ours")]
    assert jobs["theirs"].enabled
    assert "ours" not in jobs
    scheduler.stop_project_runs.assert_awaited_once_with(project.project_id, "alice")
    assert project_store.get_project_by_id(project.project_id, cache_bust=True) is None
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


@pytest.mark.asyncio
async def test_project_batch_archive_partial_and_delete_only_archived(archive):
    import shutil

    service, create, root, runtime = archive
    project = project_store.create_project("batch", str(root / "work"))
    for sid in ("sess_idle", "sess_busy", "cron_run", "heartbeat_run", "sess_archived"):
        path = create(sid)
        meta = lc.raw_metadata(sid)
        # Verify legacy directory inference as well as explicit ownership.
        meta.pop("project_id", None)
        meta["project_dir"] = project.project_dir
        lc.atomic_json(path / "metadata.json", meta)
    await service.session("sess_archived", "archive", "web")
    runtime.is_session_running.side_effect = lambda sid: sid == "sess_busy"
    result = await service.project_batch(project.project_id, "archive", "web")
    assert result["succeeded_count"] == result["failed_count"] == 1
    assert {item["session_id"] for item in result["results"]} == {
        "sess_idle",
        "sess_busy",
    }
    assert (
        next(item for item in result["results"] if not item["ok"])["code"]
        == "SESSION_BUSY"
    )
    assert lc.state("session", "sess_busy") == {}
    assert lc.state("project", project.project_id) == {}
    runtime.stop_session_for_archive.assert_not_awaited()
    assert service.list_sessions({"project_id": project.project_id})["total"] == 2
    lc.guard(project_id=project.project_id)

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    result = await service.project_batch(project.project_id, "delete_archived", "web")
    assert result["succeeded_count"] == 2 and result["failed_count"] == 0
    assert set(service.project_sessions(project.project_id)) == {
        "sess_busy",
        "cron_run",
        "heartbeat_run",
    }
    assert (await service.project_batch(project.project_id, "delete_archived", "web"))[
        "results"
    ] == []
    # Direct session deletion remains available even when active and running.
    assert (await service.session("sess_busy", "delete", "web"))["ok"]


def test_migrate_project_archives_preserves_session_and_delete_fences(archive):
    service, create, root, _ = archive
    active = project_store.create_project("same", str(root / "active"))
    legacy = project_store.create_project("same", str(root / "legacy"))
    deleting = project_store.create_project("deleting", str(root / "deleting"))
    records = lc.read_json(root / "projects.json")
    for record in records["projects"]:
        if record["project_id"] == legacy.project_id:
            record.update(hidden=True, archived_at=123)
    lc.atomic_json(root / "projects.json", records)
    lc.begin("project", legacy.project_id, "archive")
    lc.fence_writes("project", legacy.project_id)
    lc.begin("project", deleting.project_id, "delete")
    create()
    lc.begin("session", "sess_a", "archive")
    lc.complete("session", "sess_a", archived=True)
    (root / "lifecycle/project_delete_v2.json").unlink()
    cron = root / "home/cron_jobs.json"
    lc.atomic_json(cron, {"jobs": [{"enabled": False}, {"enabled": True}]})
    before = cron.read_bytes()
    lc.migrate_project_archives()
    assert not lc.projection("project", legacy.project_id)["execution_blocked"]
    assert lc.projection("project", deleting.project_id)["execution_blocked"]
    assert lc.projection("session", "sess_a")["execution_blocked"]
    projects = project_store.list_projects(cache_bust=True)
    assert len(projects) == 3
    assert len({p.name for p in projects}) == 3
    assert project_store.get_project_by_id(active.project_id).name == "same"
    assert all(
        "hidden" not in p.to_dict() and "archived_at" not in p.to_dict()
        for p in projects
    )
    assert cron.read_bytes() == before
    assert not any(
        e["event"].startswith("project.") and e["kind"] != "delete"
        for e in lc.event_snapshots()
    )
    snapshot = (root / "projects.json").read_bytes()
    lc.migrate_project_archives()
    assert (root / "projects.json").read_bytes() == snapshot


@pytest.mark.asyncio
async def test_archive_metadata_failure_rolls_back_directory(archive, monkeypatch):
    service, create, root, _ = archive
    directory = create()
    original = lc.atomic_json

    def fail_metadata(path, value):
        if path == root / "sessions_archived/sess_a/metadata.json":
            raise OSError("disk failure")
        return original(path, value)

    monkeypatch.setattr(lc, "atomic_json", fail_metadata)
    with pytest.raises(lc.LifecycleError, match="disk failure"):
        await service.session("sess_a", "archive", "web")
    assert directory.exists()
    assert not (root / "sessions_archived/sess_a").exists()
    stamp = lc.state("session", "sess_a")["operation"]["archived_at"]
    monkeypatch.setattr(lc, "atomic_json", original)
    assert (await service.session("sess_a", "archive", "web"))["archived_at"] == stamp


@pytest.mark.asyncio
async def test_batch_default_empty_and_deleting_project(archive):
    service, _, root, _ = archive
    assert (await service.project_batch("default", "archive", "web"))["results"] == []
    with pytest.raises(lc.LifecycleError) as error:
        await service.project("default", "delete", "web", {})
    assert error.value.code == "FORBIDDEN"
    project = project_store.create_project("blocked", str(root / "work"))
    await service.project(
        project.project_id, "delete", "web", {"_lifecycle_stage": "prepare"}
    )
    with pytest.raises(lc.LifecycleError) as error:
        await service.project_batch(project.project_id, "archive", "web")
    assert error.value.code == "OPERATION_IN_PROGRESS"
    # 无阶段调用直接拒绝，不再遗留 pending operation 与执行栅栏。
    project = project_store.create_project("stageless", str(root / "work2"))
    with pytest.raises(lc.LifecycleError) as error:
        await service.project(project.project_id, "delete", "web", {})
    assert error.value.code == "BAD_REQUEST"
    await service.project_batch(project.project_id, "archive", "web")
    assert not lc.state("project", project.project_id).get("operation")
