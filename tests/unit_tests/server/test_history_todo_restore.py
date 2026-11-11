from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.todo_snapshot import TODO_SNAPSHOT_MODE
from jiuwenswarm.server.context import AgentServerServices, RequestContext
from jiuwenswarm.server.handlers import session as session_handlers


class _Sink:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.consumer_tasks = [{"id": "old"}]

    async def send_wire(self, wire: dict) -> bool:
        self.sent.append(wire)
        if wire.get("payload", {}).get("event_type") == "todo.updated":
            self.consumer_tasks = wire["payload"]["todos"]
        return True


def _context(page_idx: int) -> tuple[RequestContext, _Sink]:
    sink = _Sink()
    request = AgentRequest(
        request_id=f"history-{page_idx}",
        channel_id="web",
        req_method="history.get",
        params={"session_id": "web_todo_1", "page_idx": page_idx},
        is_stream=True,
    )
    return RequestContext(request, sink, "test", SimpleNamespace()), sink


def _with_reader(ctx, sink, reader):
    owner = object()
    return RequestContext(ctx.request, sink, "test", SimpleNamespace(
        agent_manager=SimpleNamespace(get_agent_for_session_nowait=lambda **_kwargs: owner),
        resolve_adapter=lambda agent: SimpleNamespace(read_todo_snapshot=reader) if agent is owner else None,
    ))


@pytest_asyncio.fixture(autouse=True)
async def _isolated_todo_io(tmp_path, monkeypatch):
    from openjiuwen.core.sys_operation.local._rw_lock_manager import ReadWriteLockManager
    from openjiuwen.harness.tools.todo_store import _BINDING

    await ReadWriteLockManager.stop()
    monkeypatch.setattr(ReadWriteLockManager, "_lock_dir", tmp_path / "locks")
    token = _BINDING.set(None)
    yield
    _BINDING.reset(token)
    await ReadWriteLockManager.stop()


def _local_reader(tmp_path):
    from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
    from openjiuwen.harness.tools.todo import TodoModifyTool

    fs = FsOperation("history-todo", OperationMode.LOCAL, "history test", LocalWorkConfig())
    tool = TodoModifyTool(SimpleNamespace(fs=lambda: fs), str(tmp_path / "todo"))

    async def reader(session_id, *, request=None):
        items = await tool.read_todos(session_id)
        return items.snapshot, None
    return reader


@pytest.fixture
def _history(monkeypatch):
    monkeypatch.setattr(
        session_handlers,
        "get_conversation_history",
        lambda session_id, page_idx, sessions_root=None: {
            "messages": [{"role": "user", "content": "hello"}],
            "total_pages": 2,
            "page_idx": page_idx,
        },
    )
    monkeypatch.setattr(
        session_handlers,
        "encode_agent_chunk_for_wire",
        lambda chunk, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )


@pytest.mark.asyncio
async def test_history_first_page_emits_todo_snapshot_before_done(monkeypatch, _history):
    captured = {}

    async def _load(_ctx, session_id):
        captured["session_id"] = session_id
        from jiuwenswarm.common.todo_snapshot import FrontendTodos
        return FrontendTodos([{"id": "t1", "status": "pending"}], {"plan_id": "P", "revision": 1})

    monkeypatch.setattr(
        session_handlers,
        "_todo_snapshot_for_history",
        _load,
    )
    ctx, sink = _context(1)

    await session_handlers.handle_history_get_stream(ctx)

    event_types = [item["payload"]["event_type"] for item in sink.sent]
    assert event_types == ["history.message", "todo.updated", "history.message"]
    assert sink.sent[1]["sequence"] == 1
    assert sink.sent[2]["sequence"] == 2
    assert captured == {"session_id": "web_todo_1"}


@pytest.mark.asyncio
async def test_history_later_page_does_not_load_todo(monkeypatch, _history):
    called = False

    async def _load(_ctx, _session_id):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(session_handlers, "_todo_snapshot_for_history", _load)
    ctx, sink = _context(2)

    await session_handlers.handle_history_get_stream(ctx)

    assert called is False
    assert all(item["payload"]["event_type"] != "todo.updated" for item in sink.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "versioned"])
@pytest.mark.parametrize("empty", [False, True])
async def test_history_projects_real_file_using_the_consumer_mode(tmp_path, monkeypatch, _history, mode, empty):
    path = tmp_path / "todo/web_todo_1/todo.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plan_id": "P", "revision": 8,
                               "todos": [] if empty else [{"id": "A", "content": "Task", "status": "completed"}]}))

    ctx, sink = _context(1)
    ctx = _with_reader(ctx, sink, _local_reader(tmp_path))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await session_handlers.handle_history_get_stream(ctx)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    updates = [frame["payload"] for frame in sink.sent if frame["payload"]["event_type"] == "todo.updated"]
    assert len(updates) == 1
    assert isinstance(updates[0]["todos"], list)
    if mode == "legacy":
        assert "todo_meta" not in updates[0]
    else:
        assert updates[0]["todo_meta"] == {"plan_id": "P", "revision": 8}
    if empty:
        # Model a consumer that replaces its old list on todo.updated.
        assert sink.consumer_tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "versioned"])
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
async def test_history_unavailable_file_does_not_emit_a_fake_clear(tmp_path, monkeypatch, _history, mode, failure):
    path = tmp_path / "todo/web_todo_1/todo.json"
    path.parent.mkdir(parents=True)
    if failure == "corrupt":
        path.write_text("{invalid")

    ctx, sink = _context(1)
    ctx = _with_reader(ctx, sink, _local_reader(tmp_path))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await session_handlers.handle_history_get_stream(ctx)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert all(item["payload"]["event_type"] != "todo.updated" for item in sink.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("mode", ["legacy", "versioned"])
async def test_history_reads_owner_backend_without_falling_back_to_host(tmp_path, monkeypatch, _history, mode, failure):
    from unittest.mock import AsyncMock, Mock
    from openjiuwen.harness.tools.todo_store import decode_snapshot

    stale = tmp_path / "todo/web_todo_1/todo.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"plan_id": "host-old", "revision": 2, "todos": [
        {"id": "old", "content": "Old host task", "status": "pending"},
    ]}))
    host_read = Mock(return_value=stale.read_bytes())
    monkeypatch.setattr(Path, "read_bytes", host_read)
    reader = AsyncMock(side_effect=OSError("remote unavailable")) if failure else AsyncMock(return_value=(decode_snapshot({
        "plan_id": "remote", "revision": 9, "todos": [],
    }), None))
    owner = object()
    manager = SimpleNamespace(get_agent_for_session_nowait=lambda **_kwargs: owner)
    adapter = SimpleNamespace(read_todo_snapshot=reader)
    ctx, sink = _context(1)
    ctx = RequestContext(ctx.request, sink, "test", SimpleNamespace(
        agent_manager=manager, resolve_adapter=lambda agent: adapter if agent is owner else None,
    ))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await session_handlers.handle_history_get_stream(ctx)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    reader.assert_awaited_once_with("web_todo_1", request=ctx.request)
    host_read.assert_not_called()
    updates = [frame["payload"] for frame in sink.sent if frame["payload"]["event_type"] == "todo.updated"]
    if failure:
        assert updates == []
        assert sink.consumer_tasks == [{"id": "old"}]
    else:
        assert len(updates) == 1 and updates[0]["todos"] == []
        if mode == "versioned":
            assert updates[0]["todo_meta"] == {"plan_id": "remote", "revision": 9}
        else:
            assert "todo_meta" not in updates[0]


@pytest.mark.asyncio
async def test_cold_history_resolves_persisted_owner_without_new_request_fields(tmp_path, monkeypatch, _history):
    from unittest.mock import AsyncMock
    from openjiuwen.harness.tools.todo_store import decode_snapshot
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_handlers, "_sessions_dir_for_request", lambda _request: tmp_path)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *_args, **_kwargs: {
        "adapter_mode": "flash", "channel_id": "original-channel", "project_dir": "/owned/project",
    })
    owner = object()
    create = AsyncMock(return_value=owner)
    manager = SimpleNamespace(get_agent_for_session_nowait=lambda **_kwargs: None, get_agent=create)
    reader = AsyncMock(return_value=(decode_snapshot({"plan_id": "P", "revision": 4, "todos": []}), None))
    ctx, sink = _context(1)
    ctx = RequestContext(ctx.request, sink, "test", SimpleNamespace(
        agent_manager=manager, resolve_adapter=lambda agent: SimpleNamespace(read_todo_snapshot=reader),
    ))
    await session_handlers.handle_history_get_stream(ctx)
    create.assert_awaited_once_with(channel_id="original-channel", mode="flash", project_dir="/owned/project",
                                    request=ctx.request)
    reader.assert_awaited_once_with("web_todo_1", request=ctx.request)
    assert any(frame["payload"]["event_type"] == "todo.updated" for frame in sink.sent)


@pytest.mark.asyncio
async def test_adapter_history_read_uses_session_owned_tool_path_and_backend():
    import hashlib
    from unittest.mock import AsyncMock
    from openjiuwen.harness.tools.todo import TodoModifyTool
    from openjiuwen.harness.tools.todo_store import _BINDING
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    raw = json.dumps({"plan_id": "remote", "revision": 7, "todos": [
        {"id": "A", "content": "Current remote", "status": "completed"},
    ]})
    read = AsyncMock(return_value=SimpleNamespace(code=0, data=SimpleNamespace(
        content=raw, exists=True, content_sha256=hashlib.sha256(raw.encode()).hexdigest(), backend_id="box-A",
    )))
    fs = SimpleNamespace(mode="sandbox", read_file=read)
    tool = TodoModifyTool(SimpleNamespace(fs=lambda: fs), "/owned/remote/todo")
    session_adapter = object.__new__(JiuWenSwarmDeepAdapter)
    session_adapter._get_todo_modify_tool = lambda sid: tool
    session_adapter._instance = SimpleNamespace(
        _interaction_session=SimpleNamespace(get_state=lambda _key: "owner-generation"),
    )
    parent = object.__new__(JiuWenSwarmDeepAdapter)
    parent._get_or_create_session_adapter = AsyncMock(return_value=session_adapter)
    token = _BINDING.set(None)
    try:
        snapshot, generation_token = await parent.read_todo_snapshot("s")
    finally:
        _BINDING.reset(token)
    assert snapshot.plan_id == "remote" and snapshot.revision == 7
    assert generation_token == "owner-generation"
    assert snapshot.todos[0]["content"] == "Current remote"
    parent._get_or_create_session_adapter.assert_awaited_once_with("s", request=None)
    assert read.call_args.args[0] == "/owned/remote/todo/s/todo.json"


@pytest.mark.asyncio
async def test_history_without_owner_does_not_read_host_copy(tmp_path, monkeypatch, _history):
    from unittest.mock import Mock

    path = tmp_path / "todo/web_todo_1/todo.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plan_id": "old-host", "revision": 1, "todos": []}))
    host_read = Mock(return_value=path.read_bytes())
    monkeypatch.setattr(Path, "read_bytes", host_read)
    ctx, sink = _context(1)
    await session_handlers.handle_history_get_stream(ctx)
    host_read.assert_not_called()
    assert all(frame["payload"]["event_type"] != "todo.updated" for frame in sink.sent)
    assert sink.consumer_tasks == [{"id": "old"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "versioned"])
async def test_history_generation_comes_from_the_same_owner(mode, _history):
    from unittest.mock import AsyncMock
    from openjiuwen.harness.tools.todo_store import decode_snapshot

    snapshot = decode_snapshot({"plan_id": "P", "revision": 8, "todos": [
        {"id": "old", "content": "Old turn", "status": "pending", "generation_token": "old"},
        {"id": "current", "content": "Current turn", "status": "completed", "generation_token": "owner"},
    ]})
    ctx, sink = _context(1)
    ctx = _with_reader(ctx, sink, AsyncMock(return_value=(snapshot, "owner")))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await session_handlers.handle_history_get_stream(ctx)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    payload = next(frame["payload"] for frame in sink.sent if frame["payload"]["event_type"] == "todo.updated")
    assert [row["id"] for row in payload["todos"]] == (["current"] if mode == "legacy" else ["old", "current"])
    assert payload["todos"][-1]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,agent_id,service_id,uses_pool", [
    ("officeclaw", "office", "default", True),
    ("web", "office", "default", True),
    ("web", "default", "other", True),
    ("web", "default", "default", False),
])
async def test_history_selects_the_request_manager(_history, channel, agent_id, service_id, uses_pool):
    from unittest.mock import AsyncMock, Mock
    from openjiuwen.harness.tools.todo_store import decode_snapshot
    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    ctx, sink = _context(1)
    ctx.request.channel_id, ctx.request.agent_id, ctx.request.service_id = channel, agent_id, service_id
    owner = object()
    native_lookup, tenant_lookup = Mock(return_value=owner), Mock(return_value=owner)
    pool = SimpleNamespace(get_agent_manager=AsyncMock(return_value=SimpleNamespace(
        get_agent_for_session_nowait=tenant_lookup,
    )))
    pool_accessor = Mock(return_value=pool)
    reader = AsyncMock(return_value=(
        decode_snapshot({"plan_id": "P", "revision": 1, "todos": []}), None,
    ))
    services = AgentServerServices(SimpleNamespace(
        _agent_manager=SimpleNamespace(get_agent_for_session_nowait=native_lookup),
        _tenant_pool=pool_accessor,
        _resolve_adapter=lambda agent: SimpleNamespace(read_todo_snapshot=reader) if agent is owner else None,
    ))
    ctx = RequestContext(ctx.request, sink, "test", services)
    await session_handlers.handle_history_get_stream(ctx)
    selected, unused = (tenant_lookup, native_lookup) if uses_pool else (native_lookup, tenant_lookup)
    selected.assert_called_once_with(channel_id=channel, session_id="web_todo_1")
    unused.assert_not_called()
    if uses_pool:
        pool_accessor.assert_called_once_with()
        pool.get_agent_manager.assert_awaited_once_with(*TenantAgentPool.extract_ids(ctx.request))
    else:
        pool_accessor.assert_not_called()
    reader.assert_awaited_once_with("web_todo_1", request=ctx.request)
    assert any(frame["payload"]["event_type"] == "todo.updated" for frame in sink.sent)


@pytest.mark.asyncio
async def test_history_tenant_failure_keeps_messages_without_using_default_manager(_history):
    from unittest.mock import AsyncMock, Mock

    ctx, sink = _context(1)
    ctx.request.channel_id = "officeclaw"
    native_lookup = Mock()
    services = AgentServerServices(SimpleNamespace(
        _agent_manager=SimpleNamespace(get_agent_for_session_nowait=native_lookup),
        _tenant_pool=lambda: SimpleNamespace(get_agent_manager=AsyncMock(side_effect=OSError("tenant unavailable"))),
    ))
    await session_handlers.handle_history_get_stream(RequestContext(ctx.request, sink, "test", services))
    native_lookup.assert_not_called()
    assert sink.sent and all(frame["payload"]["event_type"] == "history.message" for frame in sink.sent)
