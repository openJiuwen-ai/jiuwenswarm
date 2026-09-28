"""Disk-backed lifecycle scenarios; runtime calls are isolated from LLMs."""

import asyncio
import sys
import threading
import time
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
async def test_delete_cron_sessions_refuses_running_child_and_removes_idle(archive):
    import shutil

    service, create, root, runtime = archive
    for sid in ("cron_running", "cron_idle"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta["cron_id"] = "job_a"
        lc.atomic_json(directory / "metadata.json", meta)
    # delete_job stops the cron runs (stop_job_runs) before deleting their
    # sessions; a child still running here means the run leaked.  Refuse it
    # with SESSION_BUSY rather than force-deleting a live stream underneath.
    runtime.is_session_running.side_effect = lambda sid: sid == "cron_running"

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    result = await service.delete_cron_sessions("job_a", "web")
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    failures = [item for item in result["results"] if not item["ok"]]
    assert [item["session_id"] for item in failures] == ["cron_running"]
    assert failures[0]["code"] == "SESSION_BUSY"
    # The idle child is gone; the running one and its data survive.
    assert service.cron_sessions("job_a") == ["cron_running"]
    assert lc.session_paths("cron_running")[0].exists()


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


@pytest.mark.asyncio
async def test_project_batch_rejects_archive_for_hidden_project(archive):
    """隐藏项目不再接受批量归档;delete_archived 保持放行(归档页依赖)。"""
    service, create, root, runtime = archive
    project = project_store.create_project("hidden-batch", str(root / "work"))
    path = create("sess_hidden_proj")
    meta = lc.raw_metadata("sess_hidden_proj")
    meta["project_id"] = project.project_id
    lc.atomic_json(path / "metadata.json", meta)
    await service.session("sess_hidden_proj", "archive", "web")
    project_store.hide_project(project.project_id)

    with pytest.raises(lc.LifecycleError) as error:
        await service.project_batch(project.project_id, "archive", "web")
    assert error.value.code == "NOT_FOUND"

    # 归档页是已移除项目归档会话的展示位,清空入口必须继续可用。
    result = await service.project_batch(project.project_id, "delete_archived", "web")
    assert result["succeeded_count"] == 1 and result["failed_count"] == 0


@pytest.mark.asyncio
async def test_archived_sessions_of_removed_project_stay_listed(archive):
    """项目被移除后,已归档会话仍留在归档列表(带项目名与移除标记)。"""
    service, create, root, _ = archive
    project = project_store.create_project("removed", str(root / "work"))

    def create_owned(sid, **extra):
        # fixture 默认写 project_id="default",这里改成归属待移除项目
        path = create(sid)
        meta = lc.raw_metadata(sid)
        meta.update(
            project_id=project.project_id,
            project_dir=project.project_dir,
            **extra,
        )
        lc.atomic_json(path / "metadata.json", meta)

    create_owned("sess_arch")
    create_owned("sess_live")
    await service.session("sess_arch", "archive", "web")
    assert service.list_sessions({})["total"] == 1

    # 项目还在:归档项不标记为已移除
    assert service.list_sessions({})["sessions"][0]["project_hidden"] is False

    assert project_store.hide_project(project.project_id) is not None
    project_store.invalidate_cache()

    listed = service.list_sessions({"project_id": project.project_id})
    assert listed["total"] == 1
    item = listed["sessions"][0]
    assert item["session_id"] == "sess_arch"
    # 归档项按真实项目归属展示,便于用户在归档页辨认来源
    assert item["project_id"] == project.project_id
    assert item["project_name"] == "removed"
    # 归档页据此给出"项目已移除"的说明
    assert item["project_hidden"] is True
    # 活跃会话留在原目录但已隐藏,不应出现在归档区
    assert "sess_live" not in {entry["session_id"] for entry in listed["sessions"]}
    assert (root / "sessions" / "sess_live").exists()
    assert not (root / "sessions_archived" / "sess_live").exists()


@pytest.mark.asyncio
async def test_unarchive_restores_removed_project_and_pin(archive):
    """撤销归档连带恢复被移除的项目,并恢复会话的置顶状态。"""
    service, create, root, _ = archive
    project = project_store.create_project("bring-back", str(root / "work"))

    path = create("sess_a")
    meta = lc.raw_metadata("sess_a")
    meta.update(
        project_id=project.project_id,
        project_dir=project.project_dir,
        pinned=True,
        # 故意留一个不紧凑的序号:取消归档后必须被重排写回,才能证明重排没有
        # 因为该会话仍在执行栅栏内而被跳过。
        pin_order=5,
    )
    lc.atomic_json(path / "metadata.json", meta)

    await service.session("sess_a", "archive", "web")
    # 归档不清置顶状态,撤销归档时靠它把会话放回置顶区
    assert lc.raw_metadata("sess_a")["pinned"] is True

    assert project_store.hide_project(project.project_id) is not None
    project_store.invalidate_cache()
    assert service.list_sessions({})["sessions"][0]["project_hidden"] is True

    await service.session("sess_a", "unarchive", "web")

    restored = project_store.get_project_by_id(project.project_id, cache_bust=True)
    assert restored is not None and restored.hidden is False
    meta = lc.raw_metadata("sess_a")
    assert meta.get("archived") is not True
    assert meta["pinned"] is True
    assert meta["pin_order"] == 1
    assert (root / "sessions" / "sess_a").exists()
    assert not (root / "sessions_archived" / "sess_a").exists()


@pytest.mark.asyncio
async def test_unarchive_reports_name_conflict_and_keeps_archive(archive):
    """项目名被占用时不恢复项目,也不把会话搬出归档区。"""
    service, create, root, _ = archive
    project = project_store.create_project("taken", str(root / "work"))
    path = create("sess_a")
    meta = lc.raw_metadata("sess_a")
    meta.update(project_id=project.project_id, project_dir=project.project_dir)
    lc.atomic_json(path / "metadata.json", meta)
    await service.session("sess_a", "archive", "web")
    assert project_store.hide_project(project.project_id) is not None

    # 隐藏期间同名被占用(create_project_checked 会拒绝同名,这里直接改注册表
    # 模拟旧数据/外部编辑造成的重名)。
    def add_same_name(projects):
        other = dict(projects[0])
        other.update(
            project_id="proj_other",
            project_dir=str(root / "other"),
            hidden=False,
        )
        projects.append(other)

    project_store._mutate(add_same_name)
    project_store.invalidate_cache()

    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_a", "unarchive", "web")

    assert error.value.code == "PROJECT_NAME_CONFLICT"
    # 会话仍在归档区,项目仍隐藏;冲突需用户先改名,不自动重放
    assert (root / "sessions_archived" / "sess_a").exists()
    assert not (root / "sessions" / "sess_a").exists()
    assert project_store.get_project_by_id(project.project_id, cache_bust=True).hidden is True
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "failed"
    assert operation["retryable"] is False


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

    # 置顶状态跨归档保留：取消归档时要靠它把会话放回置顶区
    assert lc.raw_metadata("first")["pinned"] is True
    assert lc.raw_metadata("first")["pin_order"] == 1
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
    # Fence new execution while preserving the parked response stream.
    assert begin_calls == [True]
    assert (root / "sessions_archived/sess_a/history.json").exists()
    assert not (root / "sessions/sess_a").exists()
    # The parked leader stream is released by its own lifecycle (disconnect,
    # runtime teardown), never as a side effect of archiving.
    stop_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_parked_team_stream_delete_proceeds_and_stops_runtime(archive):
    service, create, _, runtime = archive
    create()
    # The runtime would report the session busy (parked handler pending);
    # the parked exemption lets the delete through, and unlike archive the
    # delete's stop path tears the team runtime (and the persistent stream
    # parked on it) down before the directory goes away.
    runtime.is_session_running = Mock(return_value=True)
    runtime.has_parked_team_streams = Mock(return_value=True)

    payload = await service.session("sess_a", "delete", "web")

    assert payload["ok"] is True
    runtime.stop_session_for_archive.assert_awaited_once()
    runtime.delete_session.assert_awaited_once()
    # A stream that is not fully parked — a request still preparing or
    # mid-round — keeps the delete busy.
    create("sess_b")
    runtime.has_parked_team_streams = Mock(return_value=False)
    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_b", "delete", "web")
    assert error.value.code == "SESSION_BUSY"


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


async def _cancel_mid_move(service, sid, action):
    """Cancel a lifecycle action while its directory move is in flight."""
    reached = threading.Event()
    release = threading.Event()
    real = SessionArchiveService.__dict__["_move_session_directory"]

    def stalled(*args, **kwargs):
        reached.set()
        release.wait(10)

    SessionArchiveService._move_session_directory = staticmethod(stalled)
    task = asyncio.create_task(service.session(sid, action, "web"))
    try:
        assert await asyncio.to_thread(reached.wait, 3), "move never reached"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        SessionArchiveService._move_session_directory = real


async def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail("condition not reached in time")


@pytest.mark.asyncio
async def test_cancelled_restore_marks_failed_and_recovers_promptly(archive):
    service, create, root, _ = archive
    create()
    await service.session("sess_a", "archive", "web")
    service.start_recovery()
    await _cancel_mid_move(service, "sess_a", "unarchive")

    # The fence is labelled failed (not stuck at running) and stays up ...
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "failed"
    assert lc.state("session", "sess_a")["blocked"] is True
    # ... but the poke plus backoff-aware polling make recovery retry within
    # seconds instead of after the 10s idle poll.
    await _wait_until(
        lambda: lc.state("session", "sess_a")["operation"]["status"] == "completed"
    )
    assert (root / "sessions/sess_a").exists()
    lc.guard("sess_a")
    await service.close()


@pytest.mark.asyncio
async def test_interrupted_archive_is_finalized_and_keeps_session_active(archive):
    service, create, root, _ = archive
    create()
    service.start_recovery()
    await _cancel_mid_move(service, "sess_a", "archive")

    assert lc.state("session", "sess_a")["operation"]["status"] == "failed"
    assert (root / "sessions/sess_a").exists()
    # Recovery finalizes the abandoned archive op by directory position:
    # still active -> completed without relocating anything.
    await _wait_until(
        lambda: lc.state("session", "sess_a")["operation"]["status"] == "completed"
    )
    assert (root / "sessions/sess_a").exists()
    assert not (root / "sessions_archived/sess_a").exists()
    lc.guard("sess_a")
    # The former kind-mismatch deadlock is gone: the opposite action is
    # admissible again.
    assert (await service.session("sess_a", "unarchive", "web"))["restored"] is False
    await service.close()


@pytest.mark.asyncio
async def test_abandoned_archive_with_moved_directory_finalizes_archived(archive):
    service, create, root, _ = archive
    directory = create()
    # Simulate a crashed archive: the move finished, the operation never
    # completed, and the owner released its lease.
    lc.begin("session", "sess_a", "archive", block_execution=False)
    lc.claim_operation("session", "sess_a", "dead-owner")
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)
    lc.renew_operation("session", "sess_a", "dead-owner", release=True)
    service.start_recovery()
    await _wait_until(
        lambda: lc.state("session", "sess_a")["operation"]["status"] == "completed"
    )
    assert lc.state("session", "sess_a")["blocked"] is True
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code == "SESSION_ARCHIVED"
    # ... and unarchive is admissible again instead of kind-mismatch-deadlocked.
    assert (await service.session("sess_a", "unarchive", "web"))["ok"] is True
    assert (root / "sessions/sess_a").exists()
    await service.close()


@pytest.mark.asyncio
async def test_finalize_never_completes_a_live_retry_of_the_same_operation(archive):
    service, create, root, _ = archive
    create()
    # Cancelled archive: failed, lease released by the unwinding owner.
    lc.begin("session", "sess_a", "archive", block_execution=False)
    lc.claim_operation("session", "sess_a", "old-owner")
    lc.update(
        "session",
        "sess_a",
        status="failed",
        errors=["operation cancelled"],
        retryable=True,
    )
    lc.renew_operation("session", "sess_a", "old-owner", release=True)
    snapshot = lc.state("session", "sess_a")["operation"]

    # A manual retry reuses the same operation_id and refreshes the lease;
    # until its first status update the record still reads status=failed.
    lc.begin("session", "sess_a", "archive", block_execution=False)
    lc.claim_operation("session", "sess_a", "new-owner")
    assert (
        lc.state("session", "sess_a")["operation"]["lease_expires_at"] > time.time()
    )

    service._finalize_abandoned_archive(snapshot)

    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] != "completed"
    assert operation["owner_id"] == "new-owner"

    # Once the retry truly exits and releases its lease, the same stale
    # snapshot is finalized by the directory's position.
    lc.renew_operation("session", "sess_a", "new-owner", release=True)
    service._finalize_abandoned_archive(snapshot)
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "completed"
    assert lc.state("session", "sess_a")["blocked"] is False
    assert (root / "sessions/sess_a").exists()
    lc.guard("sess_a")


@pytest.mark.asyncio
async def test_abandoned_archive_finalization_repairs_pin_reindex(archive, monkeypatch):
    service, create, root, _ = archive
    directory = create(pinned=True)
    lc.begin("session", "sess_a", "archive", block_execution=False)
    lc.claim_operation("session", "sess_a", "old-owner")
    # Cancelled after the move: the requirement persisted before the move
    # must still be honored by the recovery finalization.
    lc.update(
        "session",
        "sess_a",
        pin_reindex_required=True,
        status="failed",
        errors=["operation cancelled"],
        retryable=True,
    )
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)
    lc.renew_operation("session", "sess_a", "old-owner", release=True)

    calls = []
    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(lambda: calls.append(1))
    )
    service._finalize_abandoned_archive(lc.state("session", "sess_a")["operation"])
    assert calls == [1]
    assert lc.state("session", "sess_a")["operation"]["status"] == "completed"
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code == "SESSION_ARCHIVED"


@pytest.mark.asyncio
async def test_pin_reindex_failure_keeps_operation_retryable(archive, monkeypatch):
    service, create, root, _ = archive
    directory = create(pinned=True)
    lc.begin("session", "sess_a", "archive", block_execution=False)
    lc.claim_operation("session", "sess_a", "old-owner")
    lc.update(
        "session",
        "sess_a",
        pin_reindex_required=True,
        status="failed",
        errors=["operation cancelled"],
        retryable=True,
    )
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)
    lc.renew_operation("session", "sess_a", "old-owner", release=True)

    def failing_reindex():
        raise OSError("disk full")

    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(failing_reindex)
    )
    service._finalize_abandoned_archive(lc.state("session", "sess_a")["operation"])
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "failed"
    assert operation["retryable"] is True

    # A manual archive retry then runs the full flow and repairs the index.
    repaired = []
    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(lambda: repaired.append(1))
    )
    result = await service.session("sess_a", "archive", "web")
    assert result["archived"] is True
    assert repaired == [1]
    assert lc.state("session", "sess_a")["operation"]["status"] == "completed"


@pytest.mark.asyncio
async def test_busy_after_flow_end_reports_finishing_hint(archive):
    service, create, _, runtime = archive
    create()
    # swarmflow.stop/自然完成后：round 仍在收尾（busy），但已无用户可停止的
    # 执行——报错改为"稍后重试"并携带 finishing 标记供前端区分文案。
    runtime.is_session_running = Mock(return_value=True)
    runtime.has_parked_team_streams = Mock(return_value=False)
    runtime.is_team_round_finishing = Mock(return_value=True)

    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_a", "archive", "web")

    assert error.value.code == "SESSION_BUSY"
    assert error.value.details["finishing"] is True
    assert "收尾" in str(error.value)
    assert "stop it before" not in str(error.value)


@pytest.mark.asyncio
async def test_busy_running_round_keeps_stop_hint(archive):
    service, create, _, runtime = archive
    create()
    # flow 仍在执行（或 round 与 flow 无关）：保持原有"先停止"引导。
    runtime.is_session_running = Mock(return_value=True)
    runtime.has_parked_team_streams = Mock(return_value=False)
    runtime.is_team_round_finishing = Mock(return_value=False)

    with pytest.raises(lc.LifecycleError) as error:
        await service.session("sess_a", "archive", "web")

    assert error.value.code == "SESSION_BUSY"
    assert "finishing" not in error.value.details
    assert "stop it before" in str(error.value)


@pytest.mark.asyncio
async def test_team_round_finishing_after_flow_probe(monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    # 无 waiter 的 flow 终态事件会走 gateway 推送兜底，单测里没有 server 实例。
    monkeypatch.setattr(manager, "_push_workflow_event_without_waiter", AsyncMock())

    def run_state(status):
        return SimpleNamespace(is_terminal=status in ("completed", "failed", "stopped"))

    handler = SimpleNamespace(get_run_states=lambda: {})
    manager._workflow_handlers["sess_a"] = handler

    async def broadcast_flow_terminal(status="stopped"):
        await manager.broadcast_event("sess_a", {
            "event_type": "workflow.updated",
            "workflow": {"id": "wf_1", "status": status},
        })

    # 无活跃 round：终态事件不置位，探测不成立。
    await broadcast_flow_terminal()
    assert not team_manager.team_round_finishing_after_flow("sess_a")


    # 新回合带着上一回合遗留的终态 run：run 状态跨回合保留，但本回合
    # 未见证过 flow 终态，不得误判为收尾（回归：P2 误判场景）。
    manager.begin_round("sess_a", "req_1")
    handler.get_run_states = lambda: {"wf_1": run_state("stopped")}
    assert not team_manager.team_round_finishing_after_flow("sess_a")

    # 本回合见证 flow 终态 + 全部 run 已终态：收尾窗口。
    await broadcast_flow_terminal()
    assert team_manager.team_round_finishing_after_flow("sess_a")

    # 见证过终态但仍有 run 在跑：不判收尾。
    handler.get_run_states = lambda: {"wf_1": run_state("running")}
    assert not team_manager.team_round_finishing_after_flow("sess_a")

    # 回合释放后标记随回合消亡：再开新回合，遗留终态 run 不再误判。
    await manager.release_round("sess_a", "req_1")
    manager.begin_round("sess_a", "req_2")
    handler.get_run_states = lambda: {
        "wf_1": run_state("stopped"),
        "wf_2": run_state("completed"),
    }
    assert not team_manager.team_round_finishing_after_flow("sess_a")

    # 新回合里新的 flow 走到终态：判收尾。
    await broadcast_flow_terminal("completed")
    assert team_manager.team_round_finishing_after_flow("sess_a")

    # paused 的 flow 没有结束，用户还有可恢复的东西：不判收尾。
    handler.get_run_states = lambda: {"wf_1": run_state("paused")}
    assert not team_manager.team_round_finishing_after_flow("sess_a")


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    {"event_type": "chat.tool_call", "tool_call": {"name": "shell"}},
    {"event_type": "chat.ask_user_question"},
    {"event_type": "team.task", "event": {
        "type": "team.task.created", "task_id": "task_1", "status": "pending",
    }},
])
async def test_flow_finishing_excludes_subsequent_work(monkeypatch, event):
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    monkeypatch.setattr(manager, "_push_workflow_event_without_waiter", AsyncMock())
    manager._workflow_handlers["sess_a"] = SimpleNamespace(
        get_run_states=lambda: {"wf_1": SimpleNamespace(is_terminal=True)},
    )
    terminal = {"event_type": "workflow.updated", "workflow": {
        "id": "wf_1", "status": "completed",
    }}
    manager.begin_round("sess_a", "req_1", defer_terminal_release=True)
    await manager.broadcast_event("sess_a", terminal)
    await manager.broadcast_event("sess_a", {
        "event_type": "chat.delta", "content": "Reporting the result",
    })
    assert team_manager.team_round_finishing_after_flow("sess_a")
    await manager.broadcast_event("sess_a", event)
    assert not team_manager.team_round_finishing_after_flow("sess_a")
    # Repeated terminal updates must not mask the later execution.
    await manager.broadcast_event("sess_a", terminal)
    assert not team_manager.team_round_finishing_after_flow("sess_a")
    await manager.release_round("sess_a", "req_1")
    manager.begin_round("sess_a", "req_2", defer_terminal_release=True)
    await manager.broadcast_event("sess_a", terminal)
    assert team_manager.team_round_finishing_after_flow("sess_a")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
async def test_flow_finishing_waits_for_existing_team_task(monkeypatch, status):
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    monkeypatch.setattr(manager, "_push_workflow_event_without_waiter", AsyncMock())
    manager._workflow_handlers["sess_a"] = SimpleNamespace(
        get_run_states=lambda: {"wf_1": SimpleNamespace(is_terminal=True)},
    )
    manager.begin_round("sess_a", "req_1", defer_terminal_release=True)
    await manager.broadcast_event("sess_a", {"event_type": "team.task", "event": {
        "type": "team.task.created", "task_id": "task_1", "status": "pending",
    }})
    await manager.broadcast_event("sess_a", {
        "event_type": "workflow.updated", "workflow": {"id": "wf_1", "status": "stopped"},
    })
    assert not team_manager.team_round_finishing_after_flow("sess_a")
    await manager.broadcast_event("sess_a", {"event_type": "team.task", "event": {
        "type": "team.task.updated", "task_id": "task_1", "status": status,
    }})
    assert team_manager.team_round_finishing_after_flow("sess_a")


@pytest.mark.asyncio
async def test_archive_fences_admission_while_flushing_accepted_writes(archive, monkeypatch):
    service, create, root, _ = archive
    create()
    generation = lc.state("session", "sess_a").get("generation", 0)
    entered, release = threading.Event(), threading.Event()

    def flush(timeout):
        entered.set()
        return release.wait(5)

    monkeypatch.setattr(sm, "flush_pending_writes", flush)
    task = asyncio.create_task(service.session("sess_a", "archive", "web"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(lc.LifecycleError) as error:
            lc.guard("sess_a")
        assert error.value.code == "OPERATION_IN_PROGRESS"
        # Accepted writers from before the fence must still drain successfully.
        lc.write_guard("sess_a", generation)
        assert (root / "sessions/sess_a").exists()
    finally:
        release.set()
        await task
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code == "SESSION_ARCHIVED"


@pytest.mark.asyncio
async def test_failed_fenced_archive_recovery_restores_admission(archive, monkeypatch):
    service, create, _, _ = archive
    create()
    monkeypatch.setattr(sm, "flush_pending_writes", lambda timeout: False)
    with pytest.raises(lc.LifecycleError):
        await service.session("sess_a", "archive", "web")
    assert lc.state("session", "sess_a")["blocked"]
    await asyncio.to_thread(
        service._finalize_abandoned_archive,
        lc.state("session", "sess_a")["operation"],
    )
    lc.guard("sess_a")
    assert not lc.state("session", "sess_a")["blocked"]


@pytest.mark.asyncio
async def test_delete_stops_heartbeat_instead_of_reporting_busy(archive, monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import SessionWorkKind
    from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission

    service, create, _, runtime = archive
    create("heartbeat_run")
    admission = SessionRunAdmission()
    cancelled_runs: list[str] = []
    cancelled_executions: list[str] = []
    execution = SimpleNamespace(
        execution_id="exec-heartbeat",
        work_kind=SessionWorkKind.HEARTBEAT,
        state=SimpleNamespace(terminal=False),
    )

    async def cancel_run(run_id):
        # Releasing the admission marker alone must not be what settles the
        # session: the coordinator still owns a live heartbeat execution.
        cancelled_runs.append(run_id)
        await admission.end_heartbeat("heartbeat_run", run_id)
        return True

    async def cancel_execution(session_id, *, execution_id=None, **kwargs):
        cancelled_executions.append(execution_id)
        execution.state.terminal = True
        return SimpleNamespace(matched=1, cancelled=1, timed_out=())

    admission.set_heartbeat_preemptor(cancel_run)
    probe = SimpleNamespace(
        _admission_controller=admission,
        _pending_chat_requests={},
        _session_coordinator=SimpleNamespace(
            snapshot_session=lambda sid: SimpleNamespace(executions=(execution,)),
            cancel_execution=cancel_execution,
        ),
    )
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(probe, sid)
    runtime.has_parked_team_streams = lambda sid: AgentRuntime.has_parked_team_streams(probe, sid)
    runtime.stop_heartbeat_runs = lambda sid: AgentRuntime.stop_heartbeat_runs(probe, sid)
    assert await admission.try_begin_heartbeat("heartbeat_run", "run-1")
    assert admission.is_heartbeat_active("heartbeat_run")
    assert runtime.is_session_running("heartbeat_run")
    # A live heartbeat must not turn delete into a busy rejection: the action
    # stops the run and its execution, then deletes a settled session.
    assert (await service.session("heartbeat_run", "delete", "web"))["ok"]
    assert cancelled_runs == ["run-1"]
    assert cancelled_executions == ["exec-heartbeat"]
    assert not admission.is_heartbeat_active("heartbeat_run")
    assert not runtime.is_session_running("heartbeat_run")
    runtime.delete_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_reports_busy_when_heartbeat_refuses_to_stop(archive, monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import SessionWorkKind
    from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission

    service, create, _, runtime = archive
    create("heartbeat_run")
    admission = SessionRunAdmission()
    execution = SimpleNamespace(
        execution_id="exec-heartbeat",
        work_kind=SessionWorkKind.HEARTBEAT,
        state=SimpleNamespace(terminal=False),
    )

    async def refuse(run_id):
        return False

    admission.set_heartbeat_preemptor(refuse)
    probe = SimpleNamespace(
        _admission_controller=admission,
        _pending_chat_requests={},
        _session_coordinator=SimpleNamespace(
            snapshot_session=lambda sid: SimpleNamespace(executions=(execution,)),
            cancel_execution=AsyncMock(),
        ),
    )
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(probe, sid)
    runtime.has_parked_team_streams = lambda sid: AgentRuntime.has_parked_team_streams(probe, sid)
    runtime.stop_heartbeat_runs = lambda sid: AgentRuntime.stop_heartbeat_runs(probe, sid)
    assert await admission.try_begin_heartbeat("heartbeat_run", "run-1")
    # Stopping failed: the run is still live, so the ordinary busy check must
    # still keep the session out of deletion.
    with pytest.raises(lc.LifecycleError) as error:
        await service.session("heartbeat_run", "delete", "web")
    assert error.value.code == "SESSION_BUSY"
    assert admission.is_heartbeat_active("heartbeat_run")
    runtime.delete_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_releases_subagents_instead_of_reporting_busy(archive):
    from unittest.mock import Mock

    service, create, _, runtime = archive
    create("subagent_run")
    released: list[str] = []

    async def release(session_id, *, channel_id="", reason="session_deleted"):
        released.append(session_id)
        # 释放后会话落定：常驻 subagent 不再让 is_session_running 判真。
        runtime.is_session_running = Mock(return_value=False)
        return True

    runtime.is_session_running = Mock(return_value=True)
    runtime.stop_subagent_runtimes = release
    # A resident subagent must not turn archive into a busy rejection: the
    # action releases it, then reads a settled session.
    assert (await service.session("subagent_run", "archive", "web"))["ok"]
    assert released == ["subagent_run"]


@pytest.mark.asyncio
async def test_busy_details_mark_subagent_finishing(archive):
    service, create, _, runtime = archive
    create("subagent_finishing")

    async def release(session_id, *, channel_id="", reason="session_deleted"):
        return True

    runtime.is_session_running = Mock(return_value=True)
    runtime.stop_subagent_runtimes = release
    runtime.is_subagent_finishing = lambda sid, *, channel_id="": True
    with pytest.raises(lc.LifecycleError) as error:
        await service.session("subagent_finishing", "delete", "web")
    assert error.value.code == "SESSION_BUSY"
    # 已被要求停止、仍在收尾：前端走"稍后重试"文案，而不是让用户去停止一个
    # 已经停过的会话。
    assert error.value.details["finishing"] is True
    assert error.value.details["subagent_finishing"] is True
    assert "请稍后重试" in str(error.value)
    runtime.delete_session.assert_not_awaited()


def _subagent_agent(owns, released):
    """Build a channel Agent whose adapter records subagent releases."""
    adapter = SimpleNamespace(apply_sandbox_runtime_patch=True)

    async def release_runtime(session_id, *, reason="session_deleted"):
        released.append((session_id, reason))

    adapter.release_subagent_runtime_for_session = release_runtime
    adapter._session_has_live_subagents = lambda sid: sid in owns
    return SimpleNamespace(
        _adapter=adapter,
        has_session_runtime=lambda sid: sid in owns,
    )


@pytest.mark.asyncio
async def test_subagent_release_targets_the_agent_owning_the_session():
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    manager = AgentManager()
    other_released: list[tuple[str, str]] = []
    owner_released: list[tuple[str, str]] = []
    # A channel caches one Agent per project and mode, and subagent controls
    # are held per Agent, so the first match is usually the wrong one.
    manager.agents["web"] = {
        "other-project": _subagent_agent(set(), other_released),
        "this-project": _subagent_agent({"sess_a"}, owner_released),
    }
    assert await manager.release_subagent_runtime_for_session(
        channel_id="web", session_id="sess_a", reason="session_archived"
    )
    # Releasing against the first Agent cancels nothing and leaves the
    # resident subagents running.
    assert other_released == []
    assert owner_released == [("sess_a", "session_archived")]
    assert manager.session_has_live_subagents(channel_id="web", session_id="sess_a")


@pytest.mark.asyncio
async def test_subagent_release_falls_back_to_channel_agent_without_owner():
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    manager = AgentManager()
    released: list[tuple[str, str]] = []
    manager.agents["web"] = {"only": _subagent_agent(set(), released)}
    # No Agent claims the Session runtime (already torn down, or never bound);
    # the release still runs rather than being skipped.
    assert await manager.release_subagent_runtime_for_session(
        channel_id="web", session_id="sess_gone"
    )
    assert released == [("sess_gone", "session_deleted")]


@pytest.mark.asyncio
async def test_archive_releases_subagents_through_the_session_own_channel(archive):
    service, create, _, runtime = archive
    directory = create("cli_subagent")
    lc.atomic_json(
        directory / "metadata.json",
        dict(
            session_id="cli_subagent",
            channel_id="cli",
            title="cli_subagent",
            work_mode="work",
            project_id="default",
        ),
    )
    sm._METADATA_CACHE.clear()
    seen: list[str] = []

    async def release(session_id, *, channel_id="", reason="session_deleted"):
        seen.append(channel_id)
        runtime.is_session_running = Mock(return_value=False)
        return True

    runtime.is_session_running = Mock(return_value=True)
    runtime.stop_subagent_runtimes = release
    assert (await service.session("cli_subagent", "archive", "web"))["ok"]
    # Agents are cached per channel, so the release has to reach the Session's
    # own channel rather than the one serving this request.
    assert seen == ["cli"]
