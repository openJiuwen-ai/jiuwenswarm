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
    # Archive now returns before the directory move; the event says cleanup is
    # still pending instead of pretending it already finished.
    assert payload["stop_pending"] is True
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

    async def _drain_finalizers() -> None:
        """Run background cleanup so lifecycle assertions stay synchronous."""
        for _ in range(50):
            tasks = [
                task
                for task in service._finalizing.values()
                if task is not None and not task.done()
            ]
            if not tasks:
                return
            done = await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
    original_session = service.session
    original_batch = service.project_batch
    original_cron_delete = service.delete_cron_sessions

    async def session_and_drain(*args, **kwargs):
        result = await original_session(*args, **kwargs)
        await _drain_finalizers()
        return result

    async def batch_and_drain(*args, **kwargs):
        result = await original_batch(*args, **kwargs)
        await _drain_finalizers()
        return result

    async def cron_delete_and_drain(*args, **kwargs):
        result = await original_cron_delete(*args, **kwargs)
        await _drain_finalizers()
        return result

    service.session = session_and_drain
    service.project_batch = batch_and_drain
    service.delete_cron_sessions = cron_delete_and_drain

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
    result = await service.session("sess_a", "delete", "web")
    assert result["ok"] is True
    assert result["finalizing"] is True
    state = lc.state("session", "sess_a")
    operation = state["operation"]
    # The failure is recorded for background retry, not returned to the user.
    assert state["deleted"] is True
    assert operation["status"] == "failed"
    assert operation["retryable"] is True
    assert operation["cleanup_attempts"] == 1
    assert operation["cleanup_next_at"] > 0
    assert (root / "sessions/sess_a").exists()
    # A manual retry resets the cleanup budget and finishes the delete.
    runtime.stop_session_for_archive.side_effect = None
    await service.session("sess_a", "delete", "web")
    runtime.delete_session.assert_awaited_once()
    assert lc.state("session", "sess_a")["operation"]["status"] == "completed"


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
async def test_running_session_archive_and_delete_are_accepted(archive):
    service, create, root, runtime = archive
    create()
    runtime.is_session_running.return_value = True

    archived = await service.session("sess_a", "archive", "web")
    assert archived["ok"] is True
    assert archived["finalizing"] is True
    # Cleanup runs in the background but still stops the live runtime.
    runtime.stop_session_for_archive.assert_awaited_once()
    assert not (root / "sessions/sess_a").exists()
    assert (root / "sessions_archived/sess_a").exists()

    create("sess_b")
    deleted = await service.session("sess_b", "delete", "web")
    assert deleted["ok"] is True
    assert deleted["finalizing"] is True
    runtime.delete_session.assert_awaited_once()


@pytest.mark.parametrize("state", ["queued", "running", "waiting_for_control", "succeeded", "failed", "cancelled"])
def test_runtime_running_check_counts_only_running(state, monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from jiuwenswarm.agents.harness.team import team_manager

    state = SessionExecutionState(state)
    snapshot = SimpleNamespace(
        executions=[SimpleNamespace(state=state)]
    )
    runtime = SimpleNamespace(
        _session_coordinator=SimpleNamespace(snapshot_session=lambda sid: snapshot)
    )
    monkeypatch.setattr(team_manager, "_team_manager", None)
    assert AgentRuntime.is_session_running(runtime, "sess_a") is (state is SessionExecutionState.RUNNING)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("phase", ["running", "pausing", "idle", "paused"])
async def test_local_runner_team_execution_controls_lifecycle_busy(
    archive, monkeypatch, action, phase,
):
    from openjiuwen.agent_teams.agent.member_activity import MemberActivityRegistry
    from openjiuwen.agent_teams.agent.stream_controller import StreamController
    from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager
    from openjiuwen.agent_teams.runtime.pool import ActiveTeam
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from openjiuwen.harness_protocol.state import HarnessState
    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.runtime.service import AgentRuntime

    service, create, root, runtime = archive
    create()
    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    monkeypatch.setattr(manager, "_is_distributed_mode", lambda config: False)
    runner = TeamRuntimeManager()
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runner, raising=False)
    agent = TeamAgent.__new__(TeamAgent)
    agent._stream_controller = StreamController.__new__(StreamController)
    agent._stream_controller._resources = SimpleNamespace(
        harness=SimpleNamespace(state=HarnessState(phase)),
    )
    agent._configurator = SimpleNamespace(harness=agent._stream_controller._resources.harness)
    agent._state = SimpleNamespace(member_registry=MemberActivityRegistry("leader"))
    # Ordinary Teams live in the Runner pool, not the local/distributed cache.
    await runner.pool.add(ActiveTeam("team", agent, "sess_a"))
    assert manager.get_team_agent("sess_a") is None
    assert not await manager.attach_distributed_hooks_for_runner_runtime("team", "sess_a", "web")
    assert manager.get_team_agent("sess_a") is agent
    manager.begin_round("sess_a", "old-owner")
    runtime._session_coordinator = SimpleNamespace(snapshot_session=lambda sid: None)
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(runtime, sid)

    result = await service.session("sess_a", action, "web")
    assert result["ok"] is True
    assert result["finalizing"] is True
    if action == "archive":
        assert not (root / "sessions/sess_a").exists()
        assert (root / "sessions_archived/sess_a").exists()
    else:
        runtime.delete_session.assert_awaited_once()
    if phase in {"running", "pausing"} or action == "delete":
        runtime.stop_session_for_archive.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("state", ["queued", "waiting_for_control", "succeeded"])
async def test_nonexecuting_session_with_orphan_team_round_can_be_removed(
    archive, monkeypatch, action, state,
):
    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session.model import SessionExecutionState

    service, create, root, runtime = archive
    create()
    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    # The owner handler has gone, but its admission and persistent stream remain.
    manager.begin_request("sess_a", "orphan")
    manager.begin_round("sess_a", "orphan")
    stream = asyncio.get_running_loop().create_future()
    manager._stream_tasks["sess_a"] = stream
    runtime._pending_chat_requests = {"sess_a": {"orphan"}}
    runtime._session_coordinator = SimpleNamespace(
        snapshot_session=lambda sid: SimpleNamespace(
            executions=[SimpleNamespace(state=SessionExecutionState(state))],
        ),
    )
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(runtime, sid)
    try:
        assert not runtime.is_session_running("sess_a")
        assert (await service.session("sess_a", action, "web"))["ok"]
        if action == "archive":
            assert not (root / "sessions/sess_a").exists()
            assert (root / "sessions_archived/sess_a").exists()
        else:
            runtime.stop_session_for_archive.assert_awaited_once()
            runtime.delete_session.assert_awaited_once()
    finally:
        stream.cancel()


@pytest.mark.asyncio
async def test_chat_preparation_does_not_block_archive(archive, monkeypatch):
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
    assert not runtime.is_session_running("sess_a")
    await service.session("sess_a", "archive", "web")
    AgentRuntime.end_chat_request(runtime, "sess_a", "first")
    assert not runtime.is_session_running("sess_a")
    AgentRuntime.end_chat_request(runtime, "sess_a", "second")
    assert not runtime.is_session_running("sess_a")
    assert (root / "sessions_archived/sess_a").exists()


@pytest.mark.asyncio
async def test_chat_admission_does_not_mark_session_busy_before_team_binding(archive, monkeypatch):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.server import agent_ws_server as module
    from jiuwenswarm.agents.harness.team import team_manager

    service, create, _, runtime = archive
    create()
    monkeypatch.setattr(team_manager, "_team_manager", None)
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
        assert not runtime.is_session_running("sess_a")
        assert (await service.session("sess_a", "archive", "web"))["ok"]
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


@pytest.mark.parametrize("work_mode", ["work", "code", "design"])
def test_archived_page_filters_every_supported_work_mode(work_mode):
    items = [
        {"session_id": f"sess_{mode}", "work_mode": mode, "archived_at": 1}
        for mode in ("work", "code", "design")
    ]

    result = lc.page(items, {"work_mode": work_mode}, "sessions", 200)

    assert [item["session_id"] for item in result["sessions"]] == [f"sess_{work_mode}"]
    assert result["total"] == 1


def test_archived_page_design_filter_returns_empty_without_design_items():
    items = [{"session_id": "sess_work", "work_mode": "work", "archived_at": 1}]

    result = lc.page(items, {"work_mode": "design"}, "sessions", 200)

    assert result["sessions"] == []
    assert result["total"] == 0


@pytest.mark.parametrize("work_mode", ["unknown", "", 1])
def test_archived_page_rejects_unsupported_work_mode(work_mode):
    with pytest.raises(lc.LifecycleError) as error:
        lc.page([], {"work_mode": work_mode}, "sessions", 200)

    assert error.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_delete_cron_sessions_accepts_running_child(archive):
    import shutil

    service, create, root, runtime = archive
    for sid in ("cron_running", "cron_idle"):
        directory = create(sid)
        meta = lc.raw_metadata(sid)
        meta["cron_id"] = "job_a"
        lc.atomic_json(directory / "metadata.json", meta)
    # Deletion no longer refuses a running child; the background finalizer
    # stops it and then removes the directory.
    runtime.is_session_running.side_effect = lambda sid: sid == "cron_running"

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    result = await service.delete_cron_sessions("job_a", "web")
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    assert service.cron_sessions("job_a") == []
    assert not lc.session_paths("cron_running")[0].exists()


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
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0
    assert {item["session_id"] for item in result["results"]} == {
        "sess_idle",
        "sess_busy",
    }
    assert all(item["ok"] and item["finalizing"] for item in result["results"])
    # Only the running session needed a stop; idle archives skip the runtime.
    runtime.stop_session_for_archive.assert_awaited_once()
    assert (root / "sessions_archived/sess_busy").exists()
    assert lc.state("project", project.project_id) == {}
    assert service.list_sessions({"project_id": project.project_id})["total"] == 3
    lc.guard(project_id=project.project_id)

    async def delete_session(*, channel_id, session_id):
        shutil.rmtree(lc.resolve_session(session_id))
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    result = await service.project_batch(project.project_id, "delete_archived", "web")
    assert result["succeeded_count"] == 3 and result["failed_count"] == 0
    assert set(service.project_sessions(project.project_id)) == {
        "cron_run",
        "heartbeat_run",
    }
    assert (await service.project_batch(project.project_id, "delete_archived", "web"))[
        "results"
    ] == []


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
    # Each accepted archive finalizes on its own; reindex runs per pinned
    # session rather than once for the whole batch.
    assert reindex.call_count == 2
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

    result = await service.session("first", "archive", "web")
    assert result["ok"] is True
    assert result["finalizing"] is True

    # 置顶状态跨归档保留：取消归档时要靠它把会话放回置顶区
    assert lc.raw_metadata("first")["pinned"] is True
    assert lc.raw_metadata("first")["pin_order"] == 1
    assert lc.raw_metadata("second")["pin_order"] == 2
    operation = lc.state("session", "first")["operation"]
    # The move succeeded; only the pin reindex failed, so the operation stays
    # retryable with the requirement persisted.
    assert operation["status"] == "failed"
    assert operation["pin_reindex_required"] is True

    # A fresh service must recover the requirement from disk, not memory.
    restarted = SessionArchiveService(runtime)
    result = await restarted.session("first", "archive", "web")
    assert result["ok"] is True
    await asyncio.gather(
        *list(restarted._finalizing.values()), return_exceptions=True
    )
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
    result = await service.session("sess_a", "archive", "web")
    assert result["ok"] is True
    assert directory.exists()
    assert not (root / "sessions_archived/sess_a").exists()
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "failed"
    stamp = operation["archived_at"]
    monkeypatch.setattr(lc, "atomic_json", original)
    retry = await service.session("sess_a", "archive", "web")
    assert retry["archived_at"] == stamp
    assert not directory.exists()
    assert (root / "sessions_archived/sess_a").exists()


@pytest.mark.asyncio
async def test_parked_team_stream_archive_proceeds_without_touching_stream(
    archive, monkeypatch
):
    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.runtime.service import AgentRuntime

    service, create, root, runtime = archive
    create()
    stop_session_runtime = AsyncMock()
    manager = SimpleNamespace(
        get_team_agent=lambda sid: None,
        find_background_task_controller=lambda sid: None,
        get_workflow_handler=lambda sid: None,
        has_stream_task=lambda sid: True,
        has_inflight_request=lambda sid: False,
        is_round_active=lambda sid: False,
        is_round_ended_request=lambda sid, rid: True,
        stop_session_runtime=stop_session_runtime,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    runtime._pending_chat_requests = {"sess_a": {"req_1"}}
    runtime._session_coordinator = SimpleNamespace(snapshot_session=lambda sid: None)
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(runtime, sid)
    assert not runtime.is_session_running("sess_a")

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
async def test_parked_team_stream_delete_proceeds_and_stops_runtime(archive, monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager
    from jiuwenswarm.runtime.service import AgentRuntime

    service, create, _, runtime = archive
    create()
    manager = SimpleNamespace(
        get_team_agent=lambda sid: SimpleNamespace(is_agent_running=lambda: sid == "sess_b"),
        find_background_task_controller=lambda sid: None,
        get_workflow_handler=lambda sid: None,
        has_stream_task=lambda sid: True,
        has_inflight_request=lambda sid: False,
        is_round_active=lambda sid: sid == "sess_b",
        is_round_ended_request=lambda sid, rid: True,
    )
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    runtime._pending_chat_requests = {"sess_a": {"req_1"}, "sess_b": {"req_2"}}
    runtime._session_coordinator = SimpleNamespace(snapshot_session=lambda sid: None)
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(runtime, sid)

    payload = await service.session("sess_a", "delete", "web")

    assert payload["ok"] is True
    assert payload["finalizing"] is True
    runtime.stop_session_for_archive.assert_awaited_once()
    runtime.delete_session.assert_awaited_once()


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
        finalizer = service._finalizing.get(sid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if finalizer is not None and not finalizer.done():
            finalizer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await finalizer
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
async def test_interrupted_archive_is_finalized_by_recovery(archive):
    service, create, root, _ = archive
    create()
    service.start_recovery()
    await _cancel_mid_move(service, "sess_a", "archive")

    assert lc.state("session", "sess_a")["operation"]["status"] == "failed"
    assert (root / "sessions/sess_a").exists()
    # Recovery retries the bounded finalizer and finishes the move.
    await _wait_until(
        lambda: lc.state("session", "sess_a")["operation"]["status"] == "completed"
    )
    assert not (root / "sessions/sess_a").exists()
    assert (root / "sessions_archived/sess_a").exists()
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code == "SESSION_ARCHIVED"
    assert (await service.session("sess_a", "unarchive", "web"))["ok"] is True
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
async def test_abandoned_archive_finalization_repairs_pin_reindex(archive, monkeypatch):
    service, create, root, _ = archive
    directory = create(pinned=True)
    lc.begin("session", "sess_a", "archive", block_execution=True)
    lc.mark_finalizing("session", "sess_a")
    lc.update("session", "sess_a", pin_reindex_required=True)
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)

    calls = []
    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(lambda: calls.append(1))
    )
    await service._finalize_session("sess_a", "archive", "web", "default", None)
    assert calls == [1]
    assert lc.state("session", "sess_a")["operation"]["status"] == "completed"
    with pytest.raises(lc.LifecycleError) as error:
        lc.guard("sess_a")
    assert error.value.code == "SESSION_ARCHIVED"


@pytest.mark.asyncio
async def test_pin_reindex_failure_keeps_operation_retryable(archive, monkeypatch):
    service, create, root, _ = archive
    directory = create(pinned=True)
    lc.begin("session", "sess_a", "archive", block_execution=True)
    lc.mark_finalizing("session", "sess_a")
    lc.update("session", "sess_a", pin_reindex_required=True)
    destination = root / "sessions_archived/sess_a"
    destination.parent.mkdir()
    directory.rename(destination)

    def failing_reindex():
        raise OSError("disk full")

    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(failing_reindex)
    )
    await service._finalize_session("sess_a", "archive", "web", "default", None)
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
async def test_running_team_round_archive_is_accepted(archive):
    service, create, _, runtime = archive
    create()
    # A running round no longer rejects the action; the finalizer stops it.
    runtime.is_session_running = Mock(return_value=True)
    runtime.has_parked_team_streams = Mock(return_value=False)
    runtime.is_team_round_finishing = Mock(return_value=True)

    result = await service.session("sess_a", "archive", "web")

    assert result["ok"] is True
    assert result["finalizing"] is True
    runtime.stop_session_for_archive.assert_awaited_once()


@pytest.mark.asyncio
async def test_team_round_finishing_after_flow_probe(monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    # 无 waiter 的 flow 终态事件会走 gateway 推送兜底，单测里没有 server 实例。
    monkeypatch.setattr(manager, "_push_event_without_waiter", AsyncMock())

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
async def test_terminal_frame_without_waiters_falls_back_to_server_push(monkeypatch):
    """A mailbox-driven round has no waiter; its terminal frame must still reach the frontend."""
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    push = AsyncMock()
    monkeypatch.setattr(manager, "_push_event_without_waiter", push)
    manager.begin_round("sess_a", "req_1", terminal_armed=True)

    terminal = {
        "event_type": "chat.processing_status",
        "session_id": "sess_a",
        "rid": 1,
        "is_processing": False,
        "is_complete": True,
    }
    await manager.broadcast_event("sess_a", terminal, channel_id="web")

    push.assert_awaited_once()
    pushed_session, pushed_event = push.await_args.args
    assert pushed_session == "sess_a"
    assert pushed_event["is_processing"] is False


@pytest.mark.asyncio
async def test_terminal_frame_with_waiters_skips_server_push(monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    push = AsyncMock()
    monkeypatch.setattr(manager, "_push_event_without_waiter", push)
    manager.begin_round("sess_a", "req_1", terminal_armed=True)
    queue = asyncio.Queue()
    manager.add_waiter("sess_a", "req_1", queue)

    terminal = {
        "event_type": "chat.processing_status",
        "session_id": "sess_a",
        "rid": 1,
        "is_processing": False,
        "is_complete": True,
    }
    await manager.broadcast_event("sess_a", terminal, channel_id="web")

    push.assert_not_awaited()
    delivered = queue.get_nowait()
    assert delivered["is_processing"] is False


@pytest.mark.asyncio
async def test_non_terminal_without_waiters_skips_server_push(monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    push = AsyncMock()
    monkeypatch.setattr(manager, "_push_event_without_waiter", push)
    manager.begin_round("sess_a", "req_1", terminal_armed=True)

    await manager.broadcast_event(
        "sess_a", {"event_type": "chat.delta", "content": "hi"}, channel_id="web",
    )

    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_unarmed_terminal_is_dropped_before_waiterless_fallback(monkeypatch):
    """The stale-frame guard outranks the fallback: an unarmed terminal never ships."""
    from jiuwenswarm.agents.harness.team import team_manager

    manager = team_manager.TeamManager()
    monkeypatch.setattr(team_manager, "_team_manager", manager)
    push = AsyncMock()
    monkeypatch.setattr(manager, "_push_event_without_waiter", push)
    manager.begin_round("sess_a", "req_1", terminal_armed=False)

    terminal = {
        "event_type": "chat.processing_status",
        "session_id": "sess_a",
        "rid": 1,
        "is_processing": False,
        "is_complete": True,
    }
    await manager.broadcast_event("sess_a", terminal, channel_id="web")

    push.assert_not_awaited()


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
    monkeypatch.setattr(manager, "_push_event_without_waiter", AsyncMock())
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
    monkeypatch.setattr(manager, "_push_event_without_waiter", AsyncMock())
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
async def test_archive_continues_when_flush_reports_pending(archive, monkeypatch):
    service, create, root, _ = archive
    create()
    monkeypatch.setattr(sm, "flush_pending_writes", lambda timeout: False)
    result = await service.session("sess_a", "archive", "web")
    # A stuck writer is best-effort now: the logical archive is accepted and
    # the directory move still happens.
    assert result["ok"] is True
    assert result["finalizing"] is True
    assert (root / "sessions_archived/sess_a").exists()
    assert lc.state("session", "sess_a")["operation"]["status"] == "completed"


@pytest.mark.asyncio
async def test_delete_stops_heartbeat_instead_of_reporting_busy(archive, monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import SessionWorkKind
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission

    service, create, _, runtime = archive
    create("heartbeat_run")
    admission = SessionRunAdmission()
    cancelled_runs: list[str] = []
    cancelled_executions: list[str] = []
    execution = SimpleNamespace(
        execution_id="exec-heartbeat",
        work_kind=SessionWorkKind.HEARTBEAT,
        state=SessionExecutionState.RUNNING,
    )

    async def cancel_run(run_id):
        # Releasing the admission marker alone must not be what settles the
        # session: the coordinator still owns a live heartbeat execution.
        cancelled_runs.append(run_id)
        await admission.end_heartbeat("heartbeat_run", run_id)
        return True

    async def cancel_execution(
        session_id: str,
        *,
        execution_id: str | None = None,
        generation: int | None = None,
    ):
        assert generation == 1
        cancelled_executions.append(execution_id)
        execution.state = SessionExecutionState.CANCELLED
        return SimpleNamespace(matched=1, cancelled=1, timed_out=())

    admission.set_heartbeat_preemptor(cancel_run)
    probe = SimpleNamespace(
        _admission_controller=admission,
        _pending_chat_requests={},
        _session_coordinator=SimpleNamespace(
            snapshot_session=lambda sid: SimpleNamespace(generation=1, executions=(execution,)),
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
async def test_delete_proceeds_when_heartbeat_refuses_to_stop(archive, monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import SessionWorkKind
    from jiuwenswarm.runtime.session.model import SessionExecutionState
    from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission

    service, create, _, runtime = archive
    create("heartbeat_run")
    admission = SessionRunAdmission()
    execution = SimpleNamespace(
        execution_id="exec-heartbeat",
        work_kind=SessionWorkKind.HEARTBEAT,
        state=SessionExecutionState.RUNNING,
    )

    async def refuse(run_id):
        return False

    admission.set_heartbeat_preemptor(refuse)
    probe = SimpleNamespace(
        _admission_controller=admission,
        _pending_chat_requests={},
        _session_coordinator=SimpleNamespace(
            snapshot_session=lambda sid: SimpleNamespace(generation=1, executions=(execution,)),
            cancel_execution=AsyncMock(),
        ),
    )
    runtime.is_session_running = lambda sid: AgentRuntime.is_session_running(probe, sid)
    runtime.has_parked_team_streams = lambda sid: AgentRuntime.has_parked_team_streams(probe, sid)
    runtime.stop_heartbeat_runs = lambda sid: AgentRuntime.stop_heartbeat_runs(probe, sid)
    assert await admission.try_begin_heartbeat("heartbeat_run", "run-1")
    result = await service.session("heartbeat_run", "delete", "web")
    # A refused heartbeat no longer blocks the user-visible deletion.  The
    # finalizer still attempts the stop; write fencing protects the directory
    # from a late heartbeat write.
    assert result["ok"] is True
    assert result["finalizing"] is True
    runtime.delete_session.assert_awaited_once()


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
async def test_delete_releases_finishing_subagents(archive):
    service, create, _, runtime = archive
    create("subagent_finishing")

    async def release(session_id, *, channel_id="", reason="session_deleted"):
        return True

    runtime.is_session_running = Mock(return_value=True)
    runtime.stop_subagent_runtimes = release
    runtime.is_subagent_finishing = lambda sid, *, channel_id="": True
    result = await service.session("subagent_finishing", "delete", "web")
    assert result["ok"] is True
    assert result["finalizing"] is True
    # The resident subagent is released in the background instead of turning
    # the user's delete into a busy rejection.
    runtime.delete_session.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_archive_finalizing_is_hidden_from_active_and_listed_in_archive(archive):
    service, create, _, _ = archive
    create()
    # Simulate the window between accepting the archive and moving the
    # directory: the operation is durable but the bytes are still active.
    lc.begin("session", "sess_a", "archive", block_execution=True)
    lc.mark_finalizing("session", "sess_a")

    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    assert [item["session_id"] for item in collect_all_sessions_metadata()] == []
    listing = service.list_sessions({})
    item = next(
        entry for entry in listing["sessions"] if entry["session_id"] == "sess_a"
    )
    assert item["archived"] is True
    assert item["finalizing"] is True
    assert item["stop_pending"] is True


@pytest.mark.asyncio
async def test_repeated_archive_reports_completed_acceptance(archive):
    service, create, _, runtime = archive
    create()
    await service.session("sess_a", "archive", "web")
    previous = lc.state("session", "sess_a")["operation"]
    runtime.stop_session_for_archive.reset_mock()

    result = await service.session("sess_a", "archive", "web")

    assert result["accepted"] is True
    assert result["finalizing"] is False
    assert result["stop_pending"] is False
    assert lc.state("session", "sess_a")["operation"]["operation_id"] == previous["operation_id"]
    runtime.stop_session_for_archive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [1, 5])
async def test_cleanup_failure_lock_wait_keeps_event_loop_responsive(
    archive, monkeypatch: pytest.MonkeyPatch, attempts: int,
) -> None:
    service, create, _, _ = archive
    create()
    operation = lc.begin("session", "sess_a", "archive")
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(project_store, "_LOCK_TIMEOUT_SEC", 0.2)

    def hold_resource() -> None:
        with lc.resource_lock("session", "sess_a"):
            entered.set()
            release.wait(2)

    holder = asyncio.create_task(asyncio.to_thread(hold_resource))
    timer = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # Only the event loop can release the competing lock. A synchronous
        # acquisition here would block this callback until the lock timed out.
        timer = asyncio.get_running_loop().call_later(0.01, release.set)
        await asyncio.wait_for(service._record_finalize_failure(
            "sess_a", "archive", attempts, RuntimeError("retry"), operation["operation_id"],
        ), 1)
        current = lc.state("session", "sess_a")["operation"]
        assert current["status"] == ("abandoned" if attempts == 5 else "failed")
    finally:
        release.set()
        if timer is not None:
            timer.cancel()
        await holder


@pytest.mark.asyncio
async def test_cancelled_lease_release_keeps_owner_until_write_finishes(
    archive, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, create, _, runtime = archive
    create()
    entered = threading.Event()
    allow_release = threading.Event()
    acquired = asyncio.Event()
    renew = lc.renew_operation

    def slow_release(kind, resource_id, owner_id, *, release=False):
        if release and owner_id == service._owner_id:
            entered.set()
            allow_release.wait(2)
        return renew(kind, resource_id, owner_id, release=release)

    monkeypatch.setattr(lc, "renew_operation", slow_release)

    async def finish_owner() -> None:
        async with service.lock("session", "sess_a"):
            pass

    contender = SessionArchiveService(runtime)

    async def next_owner() -> None:
        async with contender.lock("session", "sess_a"):
            acquired.set()

    owner_task = asyncio.create_task(finish_owner())
    contender_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        owner_task.cancel()
        contender_task = asyncio.create_task(next_owner())
        await asyncio.sleep(0.02)
        assert not acquired.is_set()
        allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner_task
        await asyncio.wait_for(contender_task, 3)
        assert acquired.is_set()
    finally:
        allow_release.set()
        await asyncio.gather(owner_task, return_exceptions=True)
        if contender_task is not None:
            await asyncio.gather(contender_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_mode", ["recovery", "manual", "exhausted"])
async def test_deleted_session_retries_mailbox_cleanup_without_repeating_delete(
    archive, retry_mode: str,
) -> None:
    service, create, _, runtime = archive
    directory = create()
    mailbox = SimpleNamespace(
        block_target_delete=Mock(),
        begin_target_delete=AsyncMock(),
        on_target_deleted=AsyncMock(side_effect=lc.LifecycleError("STOP_TIMEOUT", "mailbox busy")),
        abort_target_delete=AsyncMock(),
    )
    runtime.session_message_service = mailbox

    async def delete_session(*, channel_id: str, session_id: str):
        (directory / "metadata.json").unlink()
        (directory / "history.json").unlink()
        directory.rmdir()
        return SimpleNamespace(ok=True)

    runtime.delete_session.side_effect = delete_session
    await service.session("sess_a", "delete", "web")
    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "failed"
    assert operation["phase"] == "cleanup_mailbox"
    assert operation["runtime_deleted"] is True
    assert operation["retryable"] is True
    assert not directory.exists()

    restarted = SessionArchiveService(runtime)
    if retry_mode != "exhausted":
        mailbox.on_target_deleted.side_effect = None
        if retry_mode == "manual":
            await restarted.session("sess_a", "delete", "web")
            await asyncio.gather(*restarted._finalizer_tasks)
        else:
            await restarted._finalize_session("sess_a", "delete", "web", "default", None)
        assert lc.state("session", "sess_a")["operation"]["status"] == "completed"
    else:
        for _ in range(4):
            await restarted._finalize_session("sess_a", "delete", "web", "default", None)
        operation = lc.state("session", "sess_a")["operation"]
        assert operation["status"] == "abandoned"
        assert operation["cleanup_attempts"] == 5
    runtime.delete_session.assert_awaited_once()
    mailbox.abort_target_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_attempts_are_bounded(archive):
    service, create, _, runtime = archive
    create()
    runtime.delete_session = AsyncMock(
        return_value=SimpleNamespace(
            ok=False, error_code="DELETE_FAILED", error_message="nope"
        )
    )
    lc.begin("session", "sess_a", "delete", block_execution=True)
    lc.mark_finalizing("session", "sess_a", deleted=True)

    for _ in range(5):
        await service._finalize_session("sess_a", "delete", "web", "default", None)

    operation = lc.state("session", "sess_a")["operation"]
    assert operation["status"] == "abandoned"
    assert operation["cleanup_attempts"] == 5
    assert operation["cleanup_exhausted"] is True
    assert operation["retryable"] is False
    assert runtime.delete_session.await_count == 5

    # Once abandoned, neither recovery nor a manual re-entry may retry it.
    await service._finalize_session("sess_a", "delete", "web", "default", None)
    assert runtime.delete_session.await_count == 5


@pytest.mark.asyncio
async def test_unarchive_restores_active_listing(archive):
    service, create, _, _ = archive
    create()
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    await service.session("sess_a", "archive", "web")
    assert [item["session_id"] for item in collect_all_sessions_metadata()] == []
    await service.session("sess_a", "unarchive", "web")
    assert [item["session_id"] for item in collect_all_sessions_metadata()] == ["sess_a"]


@pytest.mark.asyncio
async def test_unarchive_after_failed_archive_restores_active_listing(
    archive, monkeypatch
):
    service, create, _, _ = archive
    create()
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    original = SessionArchiveService.__dict__["_move_session_directory"]

    def fail_move(*args, **kwargs):
        raise OSError("move failed")

    monkeypatch.setattr(
        SessionArchiveService, "_move_session_directory", staticmethod(fail_move)
    )
    result = await service.session("sess_a", "archive", "web")
    assert result["ok"] is True
    # The failed archive is logically archived: hidden from the active list.
    assert [item["session_id"] for item in collect_all_sessions_metadata()] == []

    monkeypatch.setattr(
        SessionArchiveService, "_move_session_directory", original
    )
    result = await service.session("sess_a", "unarchive", "web")
    assert result["restored"] is True
    assert [item["session_id"] for item in collect_all_sessions_metadata()] == ["sess_a"]


@pytest.mark.asyncio
async def test_unarchive_after_moved_but_failed_archive_restores_listing(
    archive, monkeypatch
):
    service, create, root, _ = archive
    create(pinned=True)
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    def fail_reindex():
        raise OSError("reindex failed")

    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(fail_reindex)
    )
    result = await service.session("sess_a", "archive", "web")
    assert result["ok"] is True
    # Move succeeded, reindex failed: logically archived, directory archived.
    assert (root / "sessions_archived/sess_a").exists()
    assert [item["session_id"] for item in collect_all_sessions_metadata()] == []

    monkeypatch.setattr(
        SessionArchiveService, "reindex_pins", staticmethod(lambda **kwargs: None)
    )
    await service.session("sess_a", "unarchive", "web")
    assert (root / "sessions/sess_a").exists()
    assert [item["session_id"] for item in collect_all_sessions_metadata()] == ["sess_a"]


@pytest.mark.asyncio
async def test_delete_archived_includes_failed_archive(archive, monkeypatch):
    service, create, root, runtime = archive
    project = project_store.create_project("p", str(root / "work"))
    path = create("sess_a")
    meta = lc.raw_metadata("sess_a")
    meta["project_id"] = project.project_id
    lc.atomic_json(path / "metadata.json", meta)

    original = SessionArchiveService.__dict__["_move_session_directory"]

    def fail_move(*args, **kwargs):
        raise OSError("move failed")

    monkeypatch.setattr(
        SessionArchiveService, "_move_session_directory", staticmethod(fail_move)
    )
    result = await service.session("sess_a", "archive", "web")
    assert result["ok"] is True
    assert lc.state("session", "sess_a")["operation"]["status"] == "failed"
    # Logically archived even though the directory never moved.
    assert "sess_a" in [
        item["session_id"] for item in service.list_sessions({})["sessions"]
    ]

    monkeypatch.setattr(SessionArchiveService, "_move_session_directory", original)
    result = await service.project_batch(project.project_id, "delete_archived", "web")
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 0
    # The delete intent is persisted; physical removal stays best-effort.
    assert lc.state("session", "sess_a")["deleted"] is True
    assert "sess_a" not in [
        item["session_id"] for item in service.list_sessions({})["sessions"]
    ]


@pytest.mark.asyncio
async def test_unarchive_after_failed_archive_restores_hidden_project(
    archive, monkeypatch
):
    service, create, root, _ = archive
    project = project_store.create_project("hidden", str(root / "work"))
    path = create("sess_a")
    meta = lc.raw_metadata("sess_a")
    meta["project_id"] = project.project_id
    lc.atomic_json(path / "metadata.json", meta)
    project_store.hide_project(project.project_id)

    original = SessionArchiveService.__dict__["_move_session_directory"]

    def fail_move(*args, **kwargs):
        raise OSError("move failed")

    monkeypatch.setattr(
        SessionArchiveService, "_move_session_directory", staticmethod(fail_move)
    )
    result = await service.session("sess_a", "archive", "web")
    assert result["ok"] is True
    assert lc.state("session", "sess_a")["operation"]["status"] == "failed"
    assert (
        project_store.get_project_by_id(
            project.project_id, cache_bust=True
        ).hidden
        is True
    )

    monkeypatch.setattr(SessionArchiveService, "_move_session_directory", original)
    result = await service.session("sess_a", "unarchive", "web")
    assert result["ok"] is True
    assert result["restored"] is True
    # Undo restores the project even though the directory never moved.
    assert (
        project_store.get_project_by_id(
            project.project_id, cache_bust=True
        ).hidden
        is False
    )
    assert "sess_a" in service.project_sessions(project.project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("tracked_retry", [False, True])
async def test_queued_archive_retry_cannot_take_over_new_operation(
    archive, tracked_retry: bool,
) -> None:
    service, create, root, runtime = archive
    create()
    runtime._session_coordinator = SimpleNamespace(
        snapshot_session=lambda sid: SimpleNamespace(generation=2)
    )
    runtime.is_session_running = Mock(return_value=True)
    runtime.stop_session_for_lifecycle = AsyncMock()
    old_operation = lc.begin("session", "sess_a", "archive")
    lc.mark_finalizing("session", "sess_a")
    lc.update("session", "sess_a", status="failed", stop_generation=1)
    service._finalize_semaphore = asyncio.Semaphore(0)
    if tracked_retry:
        old_task = service._spawn_finalizer("sess_a", "archive", "web", "default", 1)
    else:
        old_task = asyncio.create_task(service._finalize_session(
            "sess_a", "archive", "web", "default", 1,
            operation_id=old_operation["operation_id"],
        ))
    await asyncio.sleep(0)
    try:
        # Bypass the fixture's synchronous drain while both tasks are queued.
        await SessionArchiveService.session(service, "sess_a", "unarchive", "web")
        await SessionArchiveService.session(service, "sess_a", "archive", "web")
        operation = lc.state("session", "sess_a")["operation"]
        assert operation["operation_id"] != old_operation["operation_id"]
        assert operation["stop_generation"] == 2
        new_task = service._finalizing["sess_a"]
        assert new_task is not old_task

        service._finalize_semaphore.release()
        service._finalize_semaphore.release()
        await asyncio.wait_for(new_task, 2)
        await asyncio.gather(old_task, return_exceptions=True)

        runtime.stop_session_for_lifecycle.assert_awaited_once_with(
            channel_id="web", session_id="sess_a", generation=2, stop_runner=True
        )
        assert (root / "sessions_archived/sess_a").exists()
        current = lc.state("session", "sess_a")["operation"]
        assert current["operation_id"] == operation["operation_id"]
        assert current["status"] == "completed"
        assert current["errors"] == []
    finally:
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_stuck_mailbox_consumers_release_finalizer_slots(
    archive, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.session import session_message_service as mailbox_module
    from jiuwenswarm.server.runtime.session.session_message_service import SessionMessageService
    from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore

    service, create, root, runtime = archive
    monkeypatch.setattr(mailbox_module, "_TARGET_DELETE_STOP_TIMEOUT_SECONDS", 0.01)
    mailbox = SessionMessageService(
        store=SessionMessageStore(root / "messages.sqlite3"),
        admission=SimpleNamespace(),
        execute=AsyncMock(),
    )
    runtime.session_message_service = mailbox
    release = asyncio.Event()

    async def resistant_consumer() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    consumers = []
    for index in range(4):
        sid = f"delete_{index}"
        create(sid)
        task = asyncio.create_task(resistant_consumer())
        consumers.append(task)
        mailbox._workers[(sid, "")] = task
        task.add_done_callback(
            lambda done, target=sid: mailbox._worker_finished(target, done)
        )
    create("unrelated_archive")
    await asyncio.sleep(0)
    try:
        for index in range(4):
            await SessionArchiveService.session(service, f"delete_{index}", "delete", "web")
        await SessionArchiveService.session(service, "unrelated_archive", "archive", "web")
        await asyncio.wait_for(
            asyncio.gather(*list(service._finalizing.values())), 2
        )

        assert (root / "sessions_archived/unrelated_archive").exists()
        runtime.delete_session.assert_not_awaited()
        for index, task in enumerate(consumers):
            sid = f"delete_{index}"
            operation = lc.state("session", sid)["operation"]
            assert operation["status"] == "failed"
            assert operation["cleanup_attempts"] == 1
            assert not task.done()
            assert mailbox._workers[(sid, "")] is task

        # A retry still sees the original consumer and cannot delete its data.
        await service._finalize_session("delete_0", "delete", "web", "default", None)
        runtime.delete_session.assert_not_awaited()
        assert lc.state("session", "delete_0")["operation"]["cleanup_attempts"] == 2

        release.set()
        await asyncio.gather(*consumers, return_exceptions=True)
        await service._finalize_session("delete_0", "delete", "web", "default", None)
        runtime.delete_session.assert_awaited_once()
        assert lc.state("session", "delete_0")["operation"]["status"] == "completed"
    finally:
        release.set()
        await asyncio.gather(*consumers, return_exceptions=True)
        await service.close()
        await mailbox.stop()
