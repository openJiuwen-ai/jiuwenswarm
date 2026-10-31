# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""定时任务执行会话的实时流：晚到的页面还能补上已经产生的结果。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from fastapi import FastAPI

from gateway.cron.live_bus import CronLiveBus


def test_late_subscriber_replays_cron_final() -> None:
    async def _run() -> None:
        bus = CronLiveBus()
        sid = "__cron___1a1640a452f_40c17fbf6426"
        bus.publish(sid, {"event": "chat.final", "payload": {"content": "你好", "session_id": sid}})
        queue = bus.subscribe(sid, replay=True)
        frame = await asyncio.wait_for(queue.get(), timeout=1)
        bus.unsubscribe(sid, queue)
        assert frame["event"] == "chat.final"
        assert frame["payload"]["content"] == "你好"
        assert frame["replay"] is True

    asyncio.run(_run())


def test_http_stream_marks_replay_but_not_new_frames(monkeypatch) -> None:
    from gateway.channel_manager.web.web_http_routes import register_web_http_api_routes

    async def _run() -> None:
        bus = CronLiveBus()
        monkeypatch.setattr("gateway.cron.live_bus.cron_live_bus", bus)
        app = FastAPI()
        register_web_http_api_routes(app, object(), enterprise=True)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/v1/sessions/{session_id}/live")
        sid = "cron_http_replay_marker"
        bus.publish(sid, {"event": "chat.delta", "payload": {"content": "old", "live_seq": 1}})
        request = AsyncMock()
        request.is_disconnected.return_value = False
        response = await endpoint(sid, request)
        try:
            replay = await anext(response.body_iterator)
            assert '"live_replay": true' in replay
            bus.publish(sid, {"event": "chat.final", "payload": {"content": "new", "live_seq": 2}})
            live = await anext(response.body_iterator)
            assert '"live_replay": false' in live
        finally:
            await response.body_iterator.aclose()
            bus.clear_session(sid)

    asyncio.run(_run())


def test_replay_marker_does_not_leak_into_live_fanout_or_stored_frames() -> None:
    async def _run() -> None:
        bus = CronLiveBus()
        sid = "cron_replay_marker"
        first = {"event": "chat.delta", "payload": {"content": "first", "live_seq": 1}}
        bus.publish(sid, first)
        queue = bus.subscribe(sid)
        assert (await queue.get())["replay"] is True
        assert "replay" not in first
        second = {"event": "chat.final", "payload": {"content": "done", "live_seq": 2}}
        bus.publish(sid, second)
        assert await queue.get() == second
        assert "replay" not in second
        bus.unsubscribe(sid, queue)
        bus.clear_session(sid)

    asyncio.run(_run())


def test_redis_backlog_also_marks_replayed_frames(monkeypatch) -> None:
    from gateway.cron.live_bus import _encode_wire

    async def _run() -> None:
        frame = {"event": "chat.final", "payload": {"content": "remote answer", "live_seq": 7}}
        client = AsyncMock()
        client.lrange.return_value = [_encode_wire(origin="other-pod", frame=frame)]
        monkeypatch.setattr("gateway.cron.live_bus._redis_client", lambda: client)
        bus = CronLiveBus()
        queue = asyncio.Queue()
        await bus._redis_replay_into("cron_remote_replay", queue)
        assert await queue.get() == {**frame, "replay": True}

    asyncio.run(_run())
