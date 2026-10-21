# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Control-service imports yield to requests and do not block the event loop."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front import router
from jiuwenswarm.server.lifecycle import Readiness


def make_router():
    return router.MethodRouter(Readiness(), object())


@pytest.mark.asyncio
async def test_idle_preparation_waits_for_foreground_control(monkeypatch):
    instance = make_router()
    calls = []
    for name in ("session", "project", "config", "history"):
        monkeypatch.setattr(router, f"_load_{name}_service", lambda n=name: calls.append(n))
    instance._active_controls = 1
    instance.start_control_warmup()
    await asyncio.sleep(0.08)
    assert calls == []
    instance._active_controls = 0
    await asyncio.wait_for(asyncio.shield(instance._control_warmup), 2)
    assert calls == ["session", "project", "config", "history"]
    await instance.close_control_warmup()


@pytest.mark.asyncio
async def test_first_import_does_not_block_loop(monkeypatch):
    instance = make_router()
    entered, release = threading.Event(), threading.Event()

    async def handle(_request):
        return AgentResponse(request_id="control", payload={}, ok=True)

    def load():
        entered.set()
        assert release.wait(2)
        router._HANDLE_SESSION = handle

    monkeypatch.setattr(router, "_HANDLE_SESSION", None)
    monkeypatch.setattr(router, "_load_session_service", load)
    monkeypatch.setattr(instance, "_send", AsyncMock())
    task = asyncio.create_task(instance.dispatch(
        object(), AgentRequest(request_id="control", req_method=ReqMethod.SESSION_LIST), object()))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set() and not task.done()
    finally:
        release.set()
    await asyncio.wait_for(task, 2)
    assert instance._active_controls == 0
    instance._send.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_drains_an_accepted_import(monkeypatch):
    instance = make_router()
    entered, release = threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(router, "_load_session_service", load)
    instance.start_control_warmup()
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert entered.is_set()
    close = asyncio.create_task(instance.close_control_warmup())
    try:
        await asyncio.sleep(0.01)
        assert not close.done()
    finally:
        release.set()
    await asyncio.wait_for(close, 2)
    assert instance._control_warmup is None
