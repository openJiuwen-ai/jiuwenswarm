from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.host_services import send_runtime_push
from jiuwenswarm.server import http_chat
from jiuwenswarm.server.transports.http_sink import SSESink


@pytest.mark.asyncio
async def test_file_push_uses_only_its_live_http_request(monkeypatch):
    request = AgentRequest(
        request_id="r", channel_id="web", session_id="s", is_stream=True
    )
    sink = SSESink()
    ctx = SimpleNamespace(request=request, sink=sink)
    message = {
        "request_id": "r",
        "channel_id": "web",
        "session_id": "s",
        "payload": {"event_type": "chat.file", "files": [{"name": "a.txt"}]},
    }

    async def run(ctx):
        assert await send_runtime_push(message)
        assert not await send_runtime_push({**message, "session_id": "other"})
        assert not await send_runtime_push({**message, "request_id": "other"})
        assert not await send_runtime_push(
            {**message, "payload": {"event_type": "chat.final"}}
        )
        await sink.close()
        assert not await send_runtime_push(message)

    monkeypatch.setattr(http_chat, "_handle_http_chat", run)
    await http_chat.handle_http_chat(ctx)
    frames = [frame async for frame in sink.frames()]
    assert len(frames) == 1 and '"chat.file"' in frames[0]


@pytest.mark.asyncio
async def test_unary_does_not_report_invisible_file_delivery(monkeypatch):
    request = AgentRequest(
        request_id="r", channel_id="web", session_id="s", is_stream=False
    )

    async def run(ctx):
        assert not await send_runtime_push(
            {
                "request_id": "r",
                "channel_id": "web",
                "session_id": "s",
                "payload": {"event_type": "chat.file"},
            }
        )

    monkeypatch.setattr(http_chat, "_handle_http_chat", run)
    await http_chat.handle_http_chat(SimpleNamespace(request=request, sink=AsyncMock()))
