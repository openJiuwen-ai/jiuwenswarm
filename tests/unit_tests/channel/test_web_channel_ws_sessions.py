# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebChannel `_ws_sessions` 仅追踪显式 session（Issue #2334）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
    _resolve_ws_auth_session,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, data: str) -> None:
        self.sent_frames.append(json.loads(data))


def _req(*, req_id: str, method: str, params: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.resume"])
async def test_group_id_selects_real_session_before_files_and_routing(method, monkeypatch):
    from unittest.mock import AsyncMock

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _FakeWebSocket()
    seen = []
    channel.on_message(lambda msg: seen.append(msg) or True)
    process_files = AsyncMock(side_effect=lambda params, **kwargs: params)
    register_ws = AsyncMock()
    monkeypatch.setattr(channel, "_process_files", process_files)
    monkeypatch.setattr(channel, "register_ws", register_ws)
    await channel._handle_raw_message(
        ws,
        _req(req_id="group-send", method=method, params={
            "group_id": "group-one", "content": "hello", "mode": "team.work.normal",
        }),
        {},
    )
    assert seen[0].session_id == "group-one"
    if method == "chat.send":
        assert process_files.await_args.kwargs["session_id"] == "group-one"
    else:
        process_files.assert_not_awaited()
    assert register_ws.await_args.args[1].session_id == "group-one"
    assert channel._ws_sessions[id(ws)] == {"group-one"}


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [None, "", [], "../outside", "group-one"])
async def test_group_id_validation_precedes_files_and_routing(group_id, monkeypatch):
    from unittest.mock import AsyncMock

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _FakeWebSocket()
    callback = AsyncMock(return_value=True)
    channel.on_message(callback)
    response = AsyncMock()
    monkeypatch.setattr(channel, "send_response", response)
    process_files = AsyncMock()
    register_ws = AsyncMock()
    monkeypatch.setattr(channel, "_process_files", process_files)
    monkeypatch.setattr(channel, "register_ws", register_ws)
    await channel._handle_raw_message(
        ws,
        _req(req_id="bad-group", method="chat.send", params={
            "group_id": group_id, "session_id": "another-session", "content": "hello",
        }),
        {},
    )
    response.assert_awaited_once()
    assert response.await_args.kwargs["code"] == "BAD_REQUEST"
    process_files.assert_not_awaited()
    register_ws.assert_not_awaited()
    callback.assert_not_awaited()
