"""Disk-backed lifecycle scenarios; runtime calls are isolated from LLMs."""

import asyncio
import sys
from contextlib import asynccontextmanager
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
async def test_listing_skips_unusable_entries_instead_of_failing(archive):
    service, create, root, _ = archive
    create()
    await service.session("sess_a", "archive", "web")
    # 非法资源 ID（前后空白）：validate_id 拒绝，但目录在两个平台都能创建。
    stray = root / "sessions_archived/ bad_name"
    stray.mkdir()
    lc.atomic_json(stray / "metadata.json", dict(session_id=" bad_name", title="x"))
    # 损坏的 metadata.json：JSON 解析失败。
    corrupt = root / "sessions_archived/sess_corrupt"
    corrupt.mkdir()
    (corrupt / "metadata.json").write_text("{not json")
    # 空目录：会话在扫描期间被移走或外部垃圾，不得进入列表。
    (root / "sessions_archived/sess_gone").mkdir()
    result = service.list_sessions({})
    assert result["total"] == 1
    item = result["sessions"][0]
    assert item["session_id"] == "sess_a"
    assert item["archived"] is True
    assert isinstance(item["archived_at"], float)
    assert item["execution_blocked"] is True
    assert item["lifecycle_operation"] is None


@pytest.mark.asyncio
async def test_listing_is_read_only_and_backfill_persists_archive_time(archive):
    service, create, root, _ = archive
    directory = create()
    # 模拟老版本遗留：手动移入归档区，元数据没有 archived_at。
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)
    mtime = destination.stat().st_mtime
    result = service.list_sessions({})
    assert result["total"] == 1
    assert result["sessions"][0]["archived_at"] == pytest.approx(mtime)
    # 只读：列表请求不得写元数据或生命周期状态。
    assert "archived_at" not in lc.read_json(destination / "metadata.json")
    assert lc.state("session", "sess_a") == {}
    # 启动回填持久化后，列表返回持久化的值。
    service._backfill_archive_times()
    persisted = lc.read_json(destination / "metadata.json")["archived_at"]
    assert persisted == pytest.approx(mtime)
    assert service.list_sessions({})["sessions"][0]["archived_at"] == persisted


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "win32", reason="directory junctions are Windows-only"
)
async def test_listing_skips_junction_escaping_managed_root(archive, tmp_path):
    import _winapi

    service, create, root, _ = archive
    create()
    await service.session("sess_a", "archive", "web")
    # junction 指向受管存储之外：is_symlink 识别不了，必须由
    # resolve 后的父目录比对拦下（与 session_paths 守卫同判定）。
    outside = tmp_path / "outside"
    outside.mkdir()
    lc.atomic_json(
        outside / "metadata.json",
        dict(session_id="sess_escape", title="escape", project_id="default"),
    )
    junction = root / "sessions_archived" / "sess_escape"
    _winapi.CreateJunction(str(outside), str(junction))
    result = service.list_sessions({})
    assert result["total"] == 1
    assert result["sessions"][0]["session_id"] == "sess_a"
    # 启动回填同样不得读取或修复越界目标。
    service._backfill_archive_times()
    assert "archived_at" not in lc.read_json(outside / "metadata.json")


@pytest.mark.asyncio
async def test_listing_survives_permission_error_on_one_entry(archive, monkeypatch):
    import pathlib

    service, create, root, _ = archive
    create()
    await service.session("sess_a", "archive", "web")
    locked = root / "sessions_archived" / "sess_locked"
    locked.mkdir()
    (locked / "metadata.json").write_text("{}")
    original = pathlib.Path.is_dir

    def denying_is_dir(self):
        if self.name == "sess_locked":
            raise PermissionError("denied")
        return original(self)

    monkeypatch.setattr(pathlib.Path, "is_dir", denying_is_dir)
    # 单个条目的权限错误不得让整个列表失败。
    result = service.list_sessions({})
    assert result["total"] == 1
    assert result["sessions"][0]["session_id"] == "sess_a"


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
    with pytest.raises(lc.LifecycleError) as delete_error:
        await service.session("sess_a", "delete", "web")
    assert delete_error.value.code == "SESSION_BUSY"
    runtime.stop_session_for_archive.assert_not_awaited()
    runtime.delete_session.assert_not_awaited()
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
    # 等待本轮 round 的准备阶段（spec 组装/运行时激活）也算运行中。
    manager = SimpleNamespace(
        has_inflight_request=lambda sid: True,
        is_round_active=lambda sid: False,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    assert AgentRuntime.is_session_running(runtime, "sess_a")
    # round 终止后即使持久 stream 与运行时仍在（idle 常驻），也不得算运行中。
    manager = SimpleNamespace(
        has_inflight_request=lambda sid: False,
        is_round_active=lambda sid: True,
    )
    assert AgentRuntime.is_session_running(runtime, "sess_a")
    manager = SimpleNamespace(
        has_inflight_request=lambda sid: False,
        is_round_active=lambda sid: False,
        has_stream_task=lambda sid: True,
        is_runtime_active=lambda sid: True,
        is_runtime_pending=lambda sid: True,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    assert not AgentRuntime.is_session_running(runtime, "sess_a")


@pytest.mark.asyncio
async def test_chat_preparation_blocks_archive_until_all_requests_finish(archive, monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.agents.harness.team import team_manager

    service, create, root, runtime = archive
    create()
    monkeypatch.setattr(team_manager, "_team_manager", None)
    runtime._pending_chat_requests = {}
    runtime._session_coordinator = SimpleNamespace(snapshot_session=lambda sid: None)
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(runtime, sid)

    AgentRuntime.begin_chat_request(runtime, "sess_a", "first")
    AgentRuntime.begin_chat_request(runtime, "sess_a", "second")
    with pytest.raises(lc.LifecycleError, match="running") as error:
        await service.session("sess_a", "archive", "web")
    assert error.value.code == "SESSION_BUSY"
    AgentRuntime.end_chat_request(runtime, "sess_a", "first")
    assert runtime.is_session_running("sess_a")
    AgentRuntime.end_chat_request(runtime, "sess_a", "second")
    assert not runtime.is_session_running("sess_a")
    await service.session("sess_a", "archive", "web")
    assert (root / "sessions_archived/sess_a").exists()


@pytest.mark.asyncio
async def test_chat_admission_marks_session_busy_before_team_binding(archive, monkeypatch):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.server import agent_ws_server as module

    service, create, _, runtime = archive
    create()
    runtime._pending_chat_requests = {}
    runtime._session_coordinator = SimpleNamespace(snapshot_session=lambda sid: None)
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(runtime, sid)
    runtime.begin_chat_request = lambda sid, rid: AgentRuntime.begin_chat_request(runtime, sid, rid)
    runtime.end_chat_request = lambda sid, rid: AgentRuntime.end_chat_request(runtime, sid, rid)
    entered = asyncio.Event()
    release = asyncio.Event()
    request = SimpleNamespace(
        request_id="early-team-chat",
        session_id="sess_a",
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team.work.normal", "content": "hello"},
        metadata={},
        is_stream=True,
    )
    monkeypatch.setattr(module.E2AEnvelope, "from_dict", lambda data: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(module, "_payload_to_request", lambda data: request)
    server = module.AgentWebSocketServer.__new__(module.AgentWebSocketServer)
    server._execution_runtime = lambda: runtime
    server._handle_gateway_cron_callback = AsyncMock(return_value=False)
    server._handle_lifecycle_request = AsyncMock(return_value=False)
    server._dispatch_gateway_adapter_request = AsyncMock(return_value=False)
    server._trigger_before_chat_request_hook = AsyncMock()
    server._try_record_implicit_feedback = AsyncMock()

    async def wait_for_binding(_request):
        entered.set()
        await release.wait()

    server._ensure_auto_team_binding_for_chat = wait_for_binding
    server._handle_stream = AsyncMock()
    task = asyncio.create_task(server._handle_message(object(), "{}", asyncio.Lock()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(lc.LifecycleError) as error:
            await service.session("sess_a", "archive", "web")
        assert error.value.code == "SESSION_BUSY"
    finally:
        release.set()
        await task
    assert not runtime.is_session_running("sess_a")


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
    assert set(error.value.details["completed_conversation_session_ids"]) == {"sess_a"}
    assert project_store.get_project_by_id(project.project_id, cache_bust=True)
    failures.clear()
    result = await service.project(
        project.project_id,
        "delete",
        "web",
        {**token, "_lifecycle_stage": "finish", "deleted_cron_jobs": 2},
    )
    assert result["deleted_sessions"] == 2
    assert result["deleted_conversation_sessions"] == 2
    assert result["deleted_cron_jobs"] == 2
    assert await service.project(project.project_id, "delete", "web", {}) == result
    assert (user_dir / "keep.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_project_delete_skips_running_ordinary_session_and_keeps_project(archive):
    import shutil

    service, create, root, runtime = archive
    project = project_store.create_project("keep-running", str(root / "work"))
    for sid in ("sess_busy", "sess_idle", "cron_run"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta["project_id"] = project.project_id
        if sid == "cron_run":
            meta["cron_id"] = "job_a"
        lc.atomic_json(directory / "metadata.json", meta)
    runtime.is_session_running.side_effect = lambda sid: sid == "sess_busy"

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    token = await service.project(
        project.project_id, "delete", "web", {"_lifecycle_stage": "prepare"}
    )
    result = await service.project(
        project.project_id, "delete", "web", {**token, "_lifecycle_stage": "finish"}
    )
    assert result["deleted"] is False
    assert result["deleted_sessions"] == 2
    assert result["deleted_conversation_sessions"] == 1
    assert result["skipped_running_session_ids"] == ["sess_busy"]
    assert service.project_sessions(project.project_id) == ["sess_busy"]
    assert project_store.get_project_by_id(project.project_id, cache_bust=True)
    assert not lc.projection("project", project.project_id)["execution_blocked"]
    assert "sess_busy" not in [call.kwargs["session_id"] for call in runtime.stop_session_for_archive.await_args_list]


@pytest.mark.asyncio
async def test_project_delete_reuses_inventory_metadata(archive, monkeypatch):
    import shutil

    service, create, root, runtime = archive
    project = project_store.create_project("reuse", str(root / "work"))
    for sid in ("sess_1", "sess_2", "sess_3"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta["project_id"] = project.project_id
        lc.atomic_json(directory / "metadata.json", meta)

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session

    original = lc.raw_metadata
    calls = []

    def counting_raw_metadata(session_id):
        calls.append(session_id)
        return original(session_id)

    monkeypatch.setattr(lc, "raw_metadata", counting_raw_metadata)
    token = await service.project(
        project.project_id, "delete", "web", {"_lifecycle_stage": "prepare"}
    )
    calls.clear()
    result = await service.project(
        project.project_id,
        "delete",
        "web",
        {**token, "_lifecycle_stage": "finish"},
    )
    assert result["deleted"] is True
    # Exactly one metadata read per session (the inventory scan); the delete
    # loop and _session must consume the snapshot instead of re-reading.
    assert sorted(calls) == ["sess_1", "sess_2", "sess_3"]


@pytest.mark.asyncio
async def test_delete_cron_sessions_removes_running_and_idle_children(archive):
    import shutil

    service, create, root, runtime = archive
    for sid in ("cron_running", "cron_idle"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta["cron_id"] = "job_a"
        lc.atomic_json(directory / "metadata.json", meta)
    runtime.is_session_running.return_value = True

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    result = await service.delete_cron_sessions("job_a", "web")
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    assert service.cron_sessions("job_a") == []


@pytest.mark.asyncio
async def test_delete_cron_sessions_locks_each_project_once(archive, monkeypatch):
    service, create, _, _ = archive
    create("cron_job_a")  # matched by naming convention
    create("cron_child", cron_id="job_a")  # matched by metadata

    locked = []
    original = service.lock

    @asynccontextmanager
    async def counting_lock(kind, resource_id):
        if kind == "project":
            locked.append(resource_id)
        async with original(kind, resource_id):
            yield

    monkeypatch.setattr(service, "lock", counting_lock)
    result = await service.delete_cron_sessions("job_a", "web")
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    # One project execution lock for the whole group, not one per session.
    assert locked == ["default"]


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
    # A running ordinary session must be stopped before direct deletion.
    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_busy", "delete", "web")
    assert error.value.code == "SESSION_BUSY"
    runtime.is_session_running.side_effect = None
    runtime.is_session_running.return_value = False
    assert (await service.session("sess_busy", "delete", "web"))["ok"]


def test_project_inventory_builds_legacy_lookup_once_per_scan(archive, monkeypatch):
    service, create, root, _ = archive
    project = project_store.create_project("legacy-batch", str(root / "work"))
    for sid in ("legacy_1", "legacy_2", "legacy_3"):
        path = create(sid)
        meta = lc.raw_metadata(sid)
        meta.pop("project_id", None)
        meta["project_dir"] = project.project_dir
        lc.atomic_json(path / "metadata.json", meta)

    original = lc.build_project_lookup
    calls = 0

    def build_project_lookup():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(lc, "build_project_lookup", build_project_lookup)
    assert set(service.project_sessions(project.project_id)) == {
        "legacy_1",
        "legacy_2",
        "legacy_3",
    }
    assert calls == 1


@pytest.mark.asyncio
async def test_project_batch_reindexes_pins_once_only_when_required(
    archive, monkeypatch
):
    service, create, root, _ = archive
    project = project_store.create_project("pin-batch", str(root / "work"))
    for sid, pinned in (("unpinned_a", False), ("unpinned_b", False)):
        path = create(sid)
        meta = lc.raw_metadata(sid)
        meta.update(project_id=project.project_id, pinned=pinned)
        lc.atomic_json(path / "metadata.json", meta)

    reindex = Mock()
    monkeypatch.setattr(service, "reindex_pins", reindex)
    result = await service.project_batch(project.project_id, "archive", "web")
    assert result["succeeded_count"] == 2
    reindex.assert_not_called()
    assert all("_pins_reindex_required" not in item for item in result["results"])

    for sid in ("pinned_a", "pinned_b"):
        path = create(sid)
        meta = lc.raw_metadata(sid)
        meta.update(project_id=project.project_id, pinned=True, pin_order=1)
        lc.atomic_json(path / "metadata.json", meta)
    result = await service.project_batch(project.project_id, "archive", "web")
    assert result["succeeded_count"] == 2
    reindex.assert_called_once_with()
    assert all("_pins_reindex_required" not in item for item in result["results"])


@pytest.mark.asyncio
async def test_single_session_archive_reindexes_only_pinned_sessions(
    archive, monkeypatch
):
    service, create, _, _ = archive
    create("plain")
    reindex = Mock()
    monkeypatch.setattr(service, "reindex_pins", reindex)
    await service.session("plain", "archive", "web")
    # Archiving an unpinned session cannot change the pinned ordering.
    reindex.assert_not_called()

    create("pinned", pinned=True, pin_order=1)
    await service.session("pinned", "archive", "web")
    reindex.assert_called_once_with()


@pytest.mark.asyncio
async def test_archive_retry_preserves_pin_reindex_requirement(archive, monkeypatch):
    service, create, _, runtime = archive
    create("first", pinned=True, pin_order=1)
    create("second", pinned=True, pin_order=2)
    monkeypatch.setattr(
        service, "reindex_pins", Mock(side_effect=OSError("temporary reindex failure"))
    )

    with pytest.raises(lc.LifecycleError, match="temporary reindex failure"):
        await service.session("first", "archive", "web")

    assert lc.raw_metadata("first")["pinned"] is False
    assert lc.raw_metadata("second")["pin_order"] == 2
    operation = lc.state("session", "first")["operation"]
    assert operation["status"] == "failed"
    assert operation["pin_reindex_required"] is True

    # A fresh service must recover the requirement from disk, not memory.
    restarted = SessionArchiveService(runtime)
    result = await restarted.session("first", "archive", "web")
    assert result["ok"] is True
    assert lc.raw_metadata("second")["pin_order"] == 1
    operation = lc.state("session", "first")["operation"]
    assert operation["status"] == "completed"


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


@pytest.mark.asyncio
async def test_parked_team_stream_archive_proceeds_without_touching_stream(
    archive, monkeypatch
):
    from jiuwenswarm.agents.harness.team import team_manager

    service, create, root, runtime = archive
    create()
    stop_session_runtime = AsyncMock()
    manager = SimpleNamespace(
        has_stream_task=lambda sid: True,
        is_round_ended_request=lambda sid, rid: True,
        stop_session_runtime=stop_session_runtime,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    # The runtime would report the session busy (parked handler pending);
    # only the parked exemption lets the archive through.
    runtime.is_session_running = Mock(return_value=True)
    runtime.has_parked_team_streams = Mock(return_value=True)

    original_begin = lc.begin
    begin_calls = []

    def begin(*args, **kwargs):
        begin_calls.append(kwargs.get("block_execution", True))
        return original_begin(*args, **kwargs)

    monkeypatch.setattr(lc, "begin", begin)
    payload = await service.session("sess_a", "archive", "web")

    assert payload["ok"] is True
    # Only the busy check changes: archive keeps its unfenced lifecycle
    # generation, so a failed move cannot leave the session blocked.
    assert begin_calls == [False]
    assert (root / "sessions_archived/sess_a/history.json").exists()
    assert not (root / "sessions/sess_a").exists()
    # The parked leader stream is released by its own lifecycle (disconnect,
    # runtime teardown), never as a side effect of archiving.
    stop_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_parked_team_stream_still_blocks_delete(archive, monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager

    service, create, _, runtime = archive
    create()
    stop_session_runtime = AsyncMock()
    monkeypatch.setattr(
        team_manager,
        "_team_manager",
        SimpleNamespace(
            has_stream_task=lambda sid: True,
            is_round_ended_request=lambda sid, rid: True,
            stop_session_runtime=stop_session_runtime,
        ),
    )
    runtime.is_session_running = Mock(return_value=True)
    runtime.has_parked_team_streams = Mock(return_value=True)
    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_a", "delete", "web")
    assert error.value.code == "SESSION_BUSY"
    stop_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_round_marks_request_ended_until_stream_pops():
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    manager._stream_tasks["sess_a"] = asyncio.get_running_loop().create_future()
    manager.begin_round("sess_a", "req_1")
    assert not manager.is_round_ended_request("sess_a", "req_1")
    assert await manager.release_round("sess_a", "req_1")
    assert manager.is_round_ended_request("sess_a", "req_1")
    # Stream end releases every handler parked on it; the marker dies with
    # the stream instead of surviving into the next stream generation.
    assert manager.pop_stream_task("sess_a") is not None
    assert not manager.is_round_ended_request("sess_a", "req_1")


@pytest.mark.asyncio
async def test_stream_cancel_clears_ended_round_markers():
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    stream_task = asyncio.get_running_loop().create_future()
    manager._stream_tasks["sess_a"] = stream_task
    manager.begin_round("sess_a", "req_1")
    assert await manager.release_round("sess_a", "req_1")
    assert manager.is_round_ended_request("sess_a", "req_1")
    # Disconnect/shutdown cancellation is a third stream-pop site: markers
    # must not outlive their stream into the next generation, or a reused
    # request id would read as parked while it is still live.
    await manager._cancel_stream_task("sess_a", "disconnect")
    assert "sess_a" not in manager._stream_tasks
    assert not manager.is_round_ended_request("sess_a", "req_1")


@pytest.mark.asyncio
async def test_release_round_without_stream_does_not_leave_parked_marker():
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    manager.begin_round("sess_a", "req_1")
    assert await manager.release_round("sess_a", "req_1")
    assert not manager.is_round_ended_request("sess_a", "req_1")


def test_has_parked_team_streams_requires_all_requests_round_ended(monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.agents.harness.team import team_manager

    assert not AgentRuntime.has_parked_team_streams(
        SimpleNamespace(_pending_chat_requests={}), "sess_a"
    )
    runtime = SimpleNamespace(_pending_chat_requests={"sess_a": {"req_1", "req_2"}})
    ended = {"req_1"}
    manager = SimpleNamespace(
        has_stream_task=lambda sid: True,
        has_inflight_request=lambda sid: False,
        is_round_active=lambda sid: False,
        is_round_ended_request=lambda sid, rid: rid in ended,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    # A request without a released round (preparing or mid-round) is live.
    assert not AgentRuntime.has_parked_team_streams(runtime, "sess_a")
    ended.add("req_2")
    assert AgentRuntime.has_parked_team_streams(runtime, "sess_a")
    # Non-Web requests such as heartbeat/cron do not appear in the Runtime's
    # pending WebSocket request set, but they must still keep archive busy.
    manager.has_inflight_request = lambda sid: True
    assert not AgentRuntime.has_parked_team_streams(runtime, "sess_a")
    manager.has_inflight_request = lambda sid: False
    manager.is_round_active = lambda sid: True
    assert not AgentRuntime.has_parked_team_streams(runtime, "sess_a")
    manager.is_round_active = lambda sid: False
    # Stream already gone: the handlers are exiting, not parked.
    manager.has_stream_task = lambda sid: False
    assert not AgentRuntime.has_parked_team_streams(runtime, "sess_a")
    monkeypatch.setattr(team_manager, "_team_manager", None)
    assert not AgentRuntime.has_parked_team_streams(runtime, "sess_a")
