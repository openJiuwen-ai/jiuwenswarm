from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.todo_output import TODO_SNAPSHOT_MODE
from jiuwenswarm.server.context import RequestContext
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

    async def _token(_ctx, _session_id):
        return "current-token"

    def _load(_session_id, *, todo_root, generation_token):
        captured["todo_root"] = todo_root
        captured["generation_token"] = generation_token
        from jiuwenswarm.common.todo_snapshot import FrontendTodos
        return FrontendTodos([{"id": "t1", "status": "pending"}], {"plan_id": "P", "revision": 1})

    monkeypatch.setattr(
        session_handlers,
        "_todo_generation_token_for_history",
        _token,
    )
    monkeypatch.setattr(
        session_handlers,
        "_agent_workspace_dir_for_request",
        lambda _request: Path("tenant-agent-workspace"),
    )
    monkeypatch.setattr(
        session_handlers,
        "load_todo_snapshot_for_frontend",
        _load,
    )
    ctx, sink = _context(1)

    await session_handlers.handle_history_get_stream(ctx)

    event_types = [item["payload"]["event_type"] for item in sink.sent]
    assert event_types == ["history.message", "todo.updated", "history.message"]
    assert sink.sent[1]["sequence"] == 1
    assert sink.sent[2]["sequence"] == 2
    assert captured == {
        "todo_root": Path("tenant-agent-workspace") / "todo",
        "generation_token": "current-token",
    }


@pytest.mark.asyncio
async def test_history_later_page_does_not_load_todo(monkeypatch, _history):
    called = False

    def _load(_session_id):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(session_handlers, "load_todo_snapshot_for_frontend", _load)
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
    monkeypatch.setattr(session_handlers, "_agent_workspace_dir_for_request", lambda _request: tmp_path)

    async def generation(_ctx, _sid):
        return None

    monkeypatch.setattr(session_handlers, "_todo_generation_token_for_history", generation)
    ctx, sink = _context(1)
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
    monkeypatch.setattr(session_handlers, "_agent_workspace_dir_for_request", lambda _request: tmp_path)

    async def generation(_ctx, _sid):
        return None

    monkeypatch.setattr(session_handlers, "_todo_generation_token_for_history", generation)
    ctx, sink = _context(1)
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await session_handlers.handle_history_get_stream(ctx)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert all(item["payload"]["event_type"] != "todo.updated" for item in sink.sent)
