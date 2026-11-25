# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.mark.asyncio
async def test_channel_sync_handler_reports_unapplied_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.apply_channel_config_snapshot",
        lambda channels: False,
    )
    request = AgentRequest(
        request_id="channel-sync-handler",
        channel_id="web",
        req_method=ReqMethod.JIUWENMATE_CHANNEL_CONFIG_SYNC,
        params={"channels": {"feishu": {"apps": []}}},
    )

    await AgentWebSocketServer.dispatch_parsed_request(
        object.__new__(AgentWebSocketServer), object(), request, asyncio.Lock()
    )

    assert sent[0]["body"]["details"] == {
        "applied": False,
        "error": "channel snapshot was not applied",
        "code": "BAD_REQUEST",
    }
