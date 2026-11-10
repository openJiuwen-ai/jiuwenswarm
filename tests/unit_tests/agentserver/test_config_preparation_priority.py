# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepare config early while retaining the existing live-read authority."""

from __future__ import annotations

import asyncio
import threading

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front import router as routes
from jiuwenswarm.server.lifecycle import Readiness


@pytest.mark.asyncio
async def test_config_preparation_precedes_blocked_session_service(monkeypatch) -> None:
    order = []
    entered = threading.Event()
    release = threading.Event()

    def session():
        order.append("session")
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(routes, "_prepare_config_service", lambda: order.append("config"))
    monkeypatch.setattr(routes, "_load_session_service", session)
    monkeypatch.setattr(routes, "_load_project_service", lambda: order.append("project"))
    monkeypatch.setattr(routes, "_load_history_service", lambda: order.append("history"))
    router = routes.MethodRouter(Readiness(), object())
    router.start_control_warmup()
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert order == ["config", "session"]
    finally:
        release.set()
        await router.close_control_warmup()


@pytest.mark.asyncio
async def test_config_preparation_does_not_cache_stale_values_or_response_mutations(tmp_path, monkeypatch) -> None:
    from jiuwenswarm.server.control import config_service as service

    config = tmp_path / "config.yaml"
    dotenv = tmp_path / ".env"
    config.write_text("permissions:\n  enabled: false\n", encoding="utf-8")
    dotenv.write_text("MODEL_NAME=before-preparation\n", encoding="utf-8")
    monkeypatch.setattr(service, "get_config_file", lambda: config)
    monkeypatch.setattr(service, "get_env_file", lambda: dotenv)
    monkeypatch.setattr(routes, "_HANDLE_CONFIG", None)
    await asyncio.to_thread(routes._prepare_config_service)
    request = AgentRequest(request_id="config-check", channel_id="web", req_method=ReqMethod.CONFIG_GET, params={})
    first = await service.handle_config_request(request)
    assert first.payload["model"] == "before-preparation"
    assert first.payload["permissions_enabled"] == "false"
    first.payload["model"] = "caller-mutation"
    config.write_text("permissions:\n  enabled: true\n", encoding="utf-8")
    dotenv.write_text("MODEL_NAME=after-preparation\n", encoding="utf-8")
    second = await service.handle_config_request(request)
    assert second.payload["model"] == "after-preparation"
    assert second.payload["permissions_enabled"] == "true"
    assert routes._HANDLE_CONFIG is service.handle_config_request


@pytest.mark.asyncio
async def test_failed_optional_config_preparation_does_not_abort_other_services(monkeypatch) -> None:
    order = []

    def config():
        raise OSError("temporary configuration read failure")

    monkeypatch.setattr(routes, "_prepare_config_service", config)
    monkeypatch.setattr(routes, "_load_session_service", lambda: order.append("session"))
    monkeypatch.setattr(routes, "_load_project_service", lambda: order.append("project"))
    monkeypatch.setattr(routes, "_load_history_service", lambda: order.append("history"))
    router = routes.MethodRouter(Readiness(), object())
    await router._warm_control_services()
    assert order == ["session", "project", "history"]
