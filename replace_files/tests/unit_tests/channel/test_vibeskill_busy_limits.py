from __future__ import annotations

import json
from typing import Any

import pytest

from jiuwenclaw.channel.vibeskill_channel import VibeSkillChannel, VibeSkillConfig
from jiuwenclaw.channel.vibeskill_session import VIBESKILL_CHANNEL_ID, VibeSkillSessionState
from jiuwenclaw.schema.message import Message


class FakeRouter:
    def __init__(self) -> None:
        self.delivered: list[Any] = []

    def deliver_to_message_handler(self, msg: Any) -> None:
        self.delivered.append(msg)


class FakeAgentClient:
    def __init__(self) -> None:
        self.reconnecting = False

    def is_openability_reconnecting(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        return self.reconnecting


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.close_code = None
        self.close_reason = None

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _message_send(session_id: str, *, request_id: str = "req-1") -> dict[str, Any]:
    return {
        "type": "message.send",
        "id": request_id,
        "sessionID": session_id,
        "parts": [{"type": "text", "text": "build a skill"}],
    }


def _events(ws: FakeWebSocket, event_type: str) -> list[dict[str, Any]]:
    return [event for event in ws.sent if event.get("type") == event_type]


@pytest.fixture
def channel_factory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SANDBOX_DCS_HOST", raising=False)
    monkeypatch.setattr(
        VibeSkillChannel,
        "_get_local_ip",
        staticmethod(lambda: "127.0.0.1"),
    )

    def _make_channel(**config_overrides: Any) -> tuple[VibeSkillChannel, FakeRouter]:
        router = FakeRouter()
        config = VibeSkillConfig(**config_overrides)
        channel = VibeSkillChannel(config=config, router=router, agent_client=FakeAgentClient())
        return channel, router

    return _make_channel


@pytest.mark.asyncio
async def test_message_send_rejects_empty_parts_with_task_error(channel_factory) -> None:
    channel, router = channel_factory()
    session = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(session.session_id, {"user_id": "user-a"})
    ws = FakeWebSocket()
    payload = _message_send(session.session_id)
    payload["parts"] = []

    handled = await channel._handle_message_send(ws, payload)

    assert handled is True
    assert router.delivered == []
    assert await channel._store.get_state(session.session_id) is VibeSkillSessionState.IDLE
    task_errors = _events(ws, "task.error")
    assert task_errors
    assert task_errors[-1]["properties"]["error"] == "parts must not be empty"
    statuses = _events(ws, "session.status")
    assert statuses[-1]["properties"]["status"]["type"] == "idle"
    res = _events(ws, "res")
    assert res[-1]["ok"] is False
    assert res[-1]["error"] == "parts must not be empty"


@pytest.mark.asyncio
async def test_message_send_without_limits_delivers_to_message_handler(channel_factory) -> None:
    channel, router = channel_factory()
    session = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(session.session_id, {"user_id": "user-a"})
    ws = FakeWebSocket()

    handled = await channel._handle_message_send(ws, _message_send(session.session_id))

    assert handled is True
    assert len(router.delivered) == 1
    assert await channel._store.get_state(session.session_id) is VibeSkillSessionState.BUSY
    assert _events(ws, "task.error") == []


@pytest.mark.asyncio
async def test_message_send_rejects_when_user_busy_limit_reached(channel_factory) -> None:
    channel, router = channel_factory(max_busy_sessions_user=1)
    busy = await channel._store.get_or_create(session_id="sid-busy", mode="SkillCreate")
    current = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(busy.session_id, {"user_id": "user-a"})
    await channel._store.set_metadata(current.session_id, {"user_id": "user-a"})
    await channel._store.set_state(busy.session_id, VibeSkillSessionState.BUSY)
    ws = FakeWebSocket()

    handled = await channel._handle_message_send(ws, _message_send(current.session_id))

    assert handled is True
    assert router.delivered == []
    assert await channel._store.get_state(current.session_id) is VibeSkillSessionState.IDLE
    task_errors = _events(ws, "task.error")
    assert task_errors
    assert task_errors[-1]["properties"]["error"] == "用户最多可同时运行1个skill任务"
    res = _events(ws, "res")
    assert res[-1]["ok"] is False
    assert res[-1]["error"] == "用户最多可同时运行1个skill任务"


@pytest.mark.asyncio
async def test_message_send_rejects_when_gateway_busy_limit_reached(channel_factory) -> None:
    channel, router = channel_factory(max_busy_sessions=1)
    busy = await channel._store.get_or_create(session_id="sid-busy", mode="SkillCreate")
    current = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(busy.session_id, {"user_id": "user-a"})
    await channel._store.set_metadata(current.session_id, {"user_id": "user-b"})
    await channel._store.set_state(busy.session_id, VibeSkillSessionState.BUSY)
    ws = FakeWebSocket()

    handled = await channel._handle_message_send(ws, _message_send(current.session_id))

    assert handled is True
    assert router.delivered == []
    assert await channel._store.get_state(current.session_id) is VibeSkillSessionState.IDLE
    task_errors = _events(ws, "task.error")
    assert task_errors[-1]["properties"]["error"] == "服务端繁忙，请稍后再试"


@pytest.mark.asyncio
async def test_message_send_prefers_user_limit_when_both_limits_match(channel_factory) -> None:
    channel, router = channel_factory(max_busy_sessions_user=1, max_busy_sessions=1)
    busy = await channel._store.get_or_create(session_id="sid-busy", mode="SkillCreate")
    current = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(busy.session_id, {"user_id": "user-a"})
    await channel._store.set_metadata(current.session_id, {"user_id": "user-a"})
    await channel._store.set_state(busy.session_id, VibeSkillSessionState.BUSY)
    ws = FakeWebSocket()

    handled = await channel._handle_message_send(ws, _message_send(current.session_id))

    assert handled is True
    assert router.delivered == []
    task_errors = _events(ws, "task.error")
    assert task_errors[-1]["properties"]["error"] == "用户最多可同时运行1个skill任务"


@pytest.mark.asyncio
async def test_skilldev_error_without_ws_releases_busy_session(channel_factory) -> None:
    channel, _router = channel_factory(max_busy_sessions_user=1)
    session = await channel._store.get_or_create(session_id="sid-failed", mode="SkillCreate")
    await channel._store.set_metadata(session.session_id, {"user_id": "user-a"})
    await channel._store.set_state(session.session_id, VibeSkillSessionState.BUSY)

    await channel.send(
        Message(
            id="evt-1",
            type="event",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session.session_id,
            params={},
            timestamp=0.0,
            ok=True,
            payload={"event_type": "skilldev.error", "error": "skill create failed"},
        )
    )

    assert await channel._store.get_state(session.session_id) is VibeSkillSessionState.IDLE


@pytest.mark.asyncio
async def test_busy_limit_allows_new_task_after_skilldev_error_without_ws(channel_factory) -> None:
    channel, router = channel_factory(max_busy_sessions_user=1)
    failed = await channel._store.get_or_create(session_id="sid-failed", mode="SkillCreate")
    current = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(failed.session_id, {"user_id": "user-a"})
    await channel._store.set_metadata(current.session_id, {"user_id": "user-a"})
    await channel._store.set_state(failed.session_id, VibeSkillSessionState.BUSY)

    await channel.send(
        Message(
            id="evt-1",
            type="event",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=failed.session_id,
            params={},
            timestamp=0.0,
            ok=True,
            payload={"event_type": "skilldev.error", "error": "skill create failed"},
        )
    )

    ws = FakeWebSocket()
    handled = await channel._handle_message_send(ws, _message_send(current.session_id))

    assert handled is True
    assert len(router.delivered) == 1
    assert await channel._store.get_state(current.session_id) is VibeSkillSessionState.BUSY
    assert _events(ws, "task.error") == []


@pytest.mark.asyncio
async def test_user_busy_limit_falls_back_to_session_id_without_user_metadata(channel_factory) -> None:
    channel, router = channel_factory(max_busy_sessions_user=1)
    current = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_state(current.session_id, VibeSkillSessionState.BUSY)
    ws = FakeWebSocket()

    handled = await channel._handle_message_send(ws, _message_send(current.session_id))

    assert handled is True
    assert router.delivered == []
    task_errors = _events(ws, "task.error")
    assert task_errors[-1]["properties"]["error"] == "用户最多可同时运行1个skill任务"


@pytest.mark.asyncio
async def test_message_send_rejects_while_openability_reconnecting(channel_factory) -> None:
    channel, router = channel_factory()
    assert isinstance(channel._agent_client, FakeAgentClient)
    channel._agent_client.reconnecting = True
    session = await channel._store.get_or_create(session_id="sid-current", mode="SkillCreate")
    await channel._store.set_metadata(session.session_id, {"user_id": "user-a"})
    await channel._store.set_state(session.session_id, VibeSkillSessionState.BUSY)
    ws = FakeWebSocket()

    handled = await channel._handle_message_send(ws, _message_send(session.session_id))

    assert handled is True
    assert router.delivered == []
    assert await channel._store.get_state(session.session_id) is VibeSkillSessionState.IDLE
    task_errors = _events(ws, "task.error")
    assert task_errors[-1]["properties"]["error"] == "后端网络连接中断，请重试请求"
    statuses = _events(ws, "session.status")
    assert statuses[-1]["properties"]["status"]["type"] == "idle"
    res = _events(ws, "res")
    assert res[-1]["ok"] is False
    assert res[-1]["error"] == "后端网络连接中断，请重试请求"
