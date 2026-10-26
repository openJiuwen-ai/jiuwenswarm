"""Keep the Gateway busy guard until lifecycle cleanup actually completes."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.gateway.channel_manager.web import lifecycle_handlers


class _FakeChannel:
    channel_id = "web"

    def __init__(self) -> None:
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []
        self.cleared: list[str] = []
        self.clients: list[object] = []

    def register_method(self, method, handler) -> None:
        self.methods[method] = handler

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None) -> None:
        self.responses.append(
            {"req_id": req_id, "ok": ok, "payload": payload, "error": error, "code": code}
        )

    async def send_event(self, ws, event, payload) -> None:
        return None

    def clear_session_busy(self, session_id: str) -> None:
        self.cleared.append(session_id)

    def connection_user_id(self, ws) -> str:
        return "u1"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "session.archive", "session.delete", "project.sessions.archive", "project.sessions.delete_archived",
])
async def test_accepted_lifecycle_keeps_busy_flags(monkeypatch, method):
    channel = _FakeChannel()
    payload = {
        "succeeded_count": 2,
        "failed_count": 0,
        "results": [
            {"session_id": "s1", "ok": True, "accepted": True, "finalizing": True, "stop_pending": True},
            {"session_id": "s2", "ok": True, "accepted": True},
        ],
    }

    async def fake_call(*args, **kwargs):
        return True, payload

    monkeypatch.setattr(
        lifecycle_handlers, "fetch_agent_unary", fake_call
    )
    lifecycle_handlers.register_lifecycle_handlers(channel, lambda: None, lambda: None)
    handler = channel.methods[method]
    await handler(object(), "req-1", {"session_ids": ["s1", "s2"]}, None, "u1")

    assert channel.cleared == []
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_completed_archive_clears_busy_flags(monkeypatch):
    channel = _FakeChannel()

    async def fake_call(*args, **kwargs):
        return True, {"results": [
            {"session_id": "s1", "ok": True, "accepted": True, "finalizing": False, "stop_pending": False},
            {"session_id": "s2", "ok": False},
        ]}

    monkeypatch.setattr(lifecycle_handlers, "fetch_agent_unary", fake_call)
    lifecycle_handlers.register_lifecycle_handlers(channel, lambda: None, lambda: None)
    await channel.methods["session.archive"](object(), "req", {"session_ids": ["s1", "s2"]}, None, "u1")

    assert channel.cleared == ["s1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("already_completed", [False, True])
async def test_lifecycle_completion_poll_clears_busy_flags(monkeypatch, already_completed):
    channel = _FakeChannel()
    channel.clients.append(object())
    polls = 0
    original_sleep = asyncio.sleep

    async def fake_call(*args, **kwargs):
        nonlocal polls
        polls += 1
        completed = already_completed or polls > 1
        return True, {"events": [{
            "event": "session.lifecycle",
            "kind": "archive",
            "completed": completed,
            "payload": {"resource_id": "s1", "project_id": "p1", "revision": 2 if completed else 1},
            "result": {"session_id": "s1", "ok": True},
        }]}

    async def poll_again(delay):
        if already_completed or polls > 1:
            channel.clients.clear()
        else:
            assert channel.cleared == []
        await original_sleep(0)

    monkeypatch.setattr(lifecycle_handlers, "fetch_agent_unary", fake_call)
    monkeypatch.setattr(lifecycle_handlers.asyncio, "sleep", poll_again)
    lifecycle_handlers.register_lifecycle_handlers(channel, lambda: None, lambda: None)
    channel.ensure_lifecycle_watch("u1")
    watcher = next(task for task in asyncio.all_tasks() if task.get_name() == "web-lifecycle-events")
    await asyncio.wait_for(watcher, 1)

    assert channel.cleared == ["s1"]


@pytest.mark.asyncio
async def test_failed_archive_does_not_clear_busy_flags(monkeypatch):
    channel = _FakeChannel()

    async def fake_call(*args, **kwargs):
        return False, {
            "succeeded_count": 0,
            "failed_count": 1,
            "results": [{"session_id": "s1", "ok": False, "code": "SESSION_BUSY"}],
        }

    monkeypatch.setattr(
        lifecycle_handlers, "fetch_agent_unary", fake_call
    )
    lifecycle_handlers.register_lifecycle_handlers(channel, lambda: None, lambda: None)
    handler = channel.methods["session.archive"]
    await handler(object(), "req-2", {"session_ids": ["s1"]}, None, "u1")

    assert channel.cleared == []
