# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Front configuration reads stay responsive, fresh and scoped while preparing."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from e2a.wire_codec import parse_agent_server_wire_unary

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control import config_service as service
from jiuwenswarm.server.front import router as routes
from jiuwenswarm.server.lifecycle import Readiness


def _request(request_id="config") -> AgentRequest:
    return AgentRequest(request_id=request_id, channel_id="web", req_method=ReqMethod.CONFIG_GET, params={})


@pytest.fixture
def panel_files(tmp_path, monkeypatch):
    config, dotenv = tmp_path / "config.yaml", tmp_path / ".env"
    config.write_text("before", encoding="utf-8")
    dotenv.write_text("before", encoding="utf-8")
    monkeypatch.setattr(service, "get_config_file", lambda: config)
    monkeypatch.setattr(service, "get_env_file", lambda: dotenv)
    monkeypatch.setenv("PANEL_READ_PROBE", "before")
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    return config, dotenv


@pytest.mark.asyncio
async def test_first_config_request_shares_background_read_without_blocking_front(panel_files, monkeypatch):
    loop_thread = threading.get_ident()
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                joined.set()
            return super().result(timeout)

    def read_panel():
        calls.append(threading.get_ident())
        assert calls[-1] != loop_thread, "blocking config read ran on the Front event loop"
        entered.set()
        assert release.wait(5)
        return {"model": "configured", "nested": {"values": []}}

    monkeypatch.setattr(service, "get_panel", read_panel)
    monkeypatch.setattr(service, "Future", ObservedFuture, raising=False)
    monkeypatch.setattr(routes, "_HANDLE_CONFIG", None)
    for name in ("_load_session_service", "_load_project_service", "_load_history_service"):
        monkeypatch.setattr(routes, name, lambda: None)
    router = routes.MethodRouter(Readiness(), object())
    router.start_control_warmup()
    request = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        config_socket = SimpleNamespace(send=AsyncMock())
        request = asyncio.create_task(router.dispatch(config_socket, _request(), asyncio.Lock()))
        assert await asyncio.to_thread(joined.wait, 3), "request did not join the prepared read"
        health_socket = SimpleNamespace(send=AsyncMock())
        health = AgentRequest(request_id="health", channel_id="web",
                              req_method=ReqMethod.HEALTH_CHECK_GET_CONF, params={})
        await asyncio.wait_for(router.dispatch(health_socket, health, asyncio.Lock()), .5)
        health_socket.send.assert_awaited_once()
        assert not request.done() and len(calls) == 1
        release.set()
        await request
        config_socket.send.assert_awaited_once()
        response = parse_agent_server_wire_unary(json.loads(config_socket.send.call_args.args[0]))
        assert response.ok and response.payload == {"model": "configured", "nested": {"values": []}}
        assert len(calls) == 1
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await router.close_control_warmup()
    assert not service._PANEL_READS


@pytest.mark.asyncio
async def test_cold_config_request_reads_in_a_thread_and_preserves_response_identity(panel_files, monkeypatch):
    loop_thread = threading.get_ident()
    seen = []

    def read_panel():
        seen.append(threading.get_ident())
        return {"model": "configured"}

    monkeypatch.setattr(service, "get_panel", read_panel)
    request = _request("original-request")
    response = await service.handle_config_request(request)
    assert seen and seen[0] != loop_thread
    assert response.ok and response.payload == {"model": "configured"}
    assert response.request_id == request.request_id and response.channel_id == request.channel_id


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["environment", "config", "dotenv"])
async def test_changed_configuration_does_not_join_an_obsolete_inflight_read(panel_files, monkeypatch, changed):
    config, dotenv = panel_files
    entered, release = threading.Event(), threading.Event()

    def read_panel():
        values = (os.getenv("PANEL_READ_PROBE"), config.read_text(encoding="utf-8"),
                  dotenv.read_text(encoding="utf-8"))
        if values == ("before", "before", "before"):
            entered.set()
            assert release.wait(5)
        return {"values": values}

    monkeypatch.setattr(service, "get_panel", read_panel)
    first = asyncio.create_task(service.handle_config_request(_request("before")))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if changed == "environment":
            monkeypatch.setenv("PANEL_READ_PROBE", "after")
        else:
            (config if changed == "config" else dotenv).write_text("after-change", encoding="utf-8")
        second = await asyncio.wait_for(service.handle_config_request(_request("after")), 2)
        assert tuple(second.payload["values"]) != ("before", "before", "before")
    finally:
        release.set()
        original = await first
    assert tuple(original.payload["values"]) == ("before", "before", "before")


@pytest.mark.asyncio
async def test_inflight_reads_are_tenant_scoped_and_response_copies_are_independent(panel_files, monkeypatch, tmp_path):
    from jiuwenswarm.common import paths
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    entered = {key: threading.Event() for key in ("alice", "bob")}
    release = threading.Event()

    def read_panel():
        key = TenantContext.current_tenant_key()
        entered[key].set()
        assert release.wait(5)
        return {"tenant": key, "nested": {"values": []}}

    async def read_as(key):
        with TenantContext.scoped(key):
            return await service.handle_config_request(_request(key))

    monkeypatch.setattr(service, "get_panel", read_panel)
    tasks = [asyncio.create_task(read_as(key)) for key in entered]
    try:
        for event in entered.values():
            assert await asyncio.to_thread(event.wait, 3)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert [response.payload["tenant"] for response in results] == ["alice", "bob"]
    results[0].payload["nested"]["values"].append("changed")
    assert results[1].payload["nested"]["values"] == []
    assert TenantContext.current_tenant_key() is None


@pytest.mark.asyncio
async def test_failed_read_is_retried_and_cancellation_does_not_poison_following_requests(panel_files, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read_panel():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("temporary configuration read failure")
        entered.set()
        assert release.wait(5)
        return {"nested": {"values": []}}

    monkeypatch.setattr(service, "get_panel", read_panel)
    with pytest.raises(OSError, match="temporary configuration read failure"):
        await service.handle_config_request(_request())
    cancelled = asyncio.create_task(service.handle_config_request(_request("cancelled")))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        following = asyncio.create_task(service.handle_config_request(_request("following")))
        release.set()
        response = await following
        response.payload["nested"]["values"].append("caller mutation")
    finally:
        release.set()
        tasks = [cancelled]
        if following is not None:
            tasks.append(following)
        await asyncio.gather(*tasks, return_exceptions=True)
    fresh = await service.handle_config_request(_request("fresh"))
    assert fresh.payload["nested"]["values"] == [] and not service._PANEL_READS
