# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.router import MethodRouter
from jiuwenswarm.server.lifecycle import Readiness


class _Admission:
    def __init__(self) -> None:
        self.called = False

    async def dispatch(self, ws, request, send_lock) -> None:
        _ = ws, request, send_lock
        self.called = True


@pytest.mark.asyncio
async def test_channel_sync_is_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    admission = _Admission()
    router = MethodRouter(Readiness(), admission)  # type: ignore[arg-type]
    request = AgentRequest(
        request_id="channel-sync-1",
        channel_id="web",
        req_method=ReqMethod.JIUWENMATE_CHANNEL_CONFIG_SYNC,
        params={"channels": {"feishu": {"apps": []}}},
    )

    await router.dispatch(
        SimpleNamespace(request_headers={}),
        request,
        asyncio.Lock(),
    )

    assert admission.called is True
    assert sent == []
