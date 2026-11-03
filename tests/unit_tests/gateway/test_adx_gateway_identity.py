# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.extensions.adx.config import load_router_config
from gateway.extensions.adx.router_client import AdxRouterClient


def _client(monkeypatch):
    monkeypatch.setenv("ADX_TEST_PLATFORM", "platform-key")
    return AdxRouterClient(load_router_config({"gateway": {"adx": {
        "ingress_url": "https://adx.example", "tenant": "appliance",
        "token_env": "ADX_TEST_PLATFORM",
    }}}))


@pytest.mark.parametrize("backend", ["adx_router", "agentos_router"])
async def test_extension_registers_only_when_adx_backend_is_selected(monkeypatch, backend):
    from gateway.extensions.adx import extension

    # A Yuanrong deployment must start without any ADX settings or credentials.
    settings = {"gateway": {"agent_client": {"type": backend}}}
    if backend == "adx_router":
        monkeypatch.setenv("ADX_TEST_PLATFORM", "platform-key")
        settings["gateway"]["adx"] = {
            "ingress_url": "https://adx.example", "tenant": "appliance", "token_env": "ADX_TEST_PLATFORM",
        }
    monkeypatch.setattr(extension, "get_config", lambda: settings)
    registered = []
    third_agents = []
    registry = SimpleNamespace(register_agent_server_client=registered.append,register_third_agent=third_agents.append)
    extensions = await extension.register_extensions(registry)
    if backend == "agentos_router":
        assert extensions == registered == []
        return
    assert len(extensions) == 1 and extensions == registered == third_agents
    assert extensions[0].metadata.id == "adx_router"
    assert isinstance(extensions[0].get_client(), AdxRouterClient)
    await extensions[0].shutdown()


@pytest.mark.parametrize("method", ["history.get", "chat.send", "command.goal"])
async def test_adx_cron_snapshot_is_turn_only_and_owner_scoped(monkeypatch, method):
    from openjiuwen_protocol.e2a.models import E2AEnvelope
    from gateway.message_handler.message_handler import MessageHandler

    client = _client(monkeypatch)
    jobs = [{"id": "alice-job", "user_id": "alice"}]
    controller = SimpleNamespace(list_jobs=AsyncMock(return_value=jobs))
    handler = object.__new__(MessageHandler)
    handler.agent_client = client
    handler._cron_controller = controller
    send = AsyncMock(return_value=SimpleNamespace(ok=True))
    monkeypatch.setattr(
        "gateway.message_handler.message_handler.send_agent_request_with_timeout", send,
    )
    try:
        await handler._sync_agentos_cron_jobs(E2AEnvelope(
            request_id="req-1", method=method, user_id="alice", channel="web", session_id="session-1",
        ))
        if method == "history.get":
            controller.list_jobs.assert_not_awaited()
            send.assert_not_awaited()
        else:
            controller.list_jobs.assert_awaited_once_with(user_id="alice")
            send.assert_awaited_once()
            snapshot = send.await_args.args[1]
            assert snapshot.method == "cron.jobs.sync"
            assert snapshot.user_id == "alice" and snapshot.params["jobs"] == jobs
    finally:
        await client.shutdown()


async def test_extension_reuses_agentos_business_authenticator(monkeypatch):
    from gateway.extensions.adx import extension

    monkeypatch.setenv("ADX_TEST_PLATFORM", "platform-key")
    created = []

    class FakeAuthenticator:
        def __init__(self, url, timeout):
            created.append((url, timeout, self))

    monkeypatch.setattr(extension, "AgentOSAuthenticator", FakeAuthenticator)
    monkeypatch.setattr(extension, "get_config", lambda: {"gateway": {
        "agent_client": {"type": "adx_router"},
        "agentos": {"auth_enabled": "true", "auth_service_url": "http://agentos-iam", "timeout": 7},
        "adx": {"ingress_url": "https://adx.example", "tenant": "appliance",
                "token_env": "ADX_TEST_PLATFORM"},
    }})
    registered = []
    extensions = await extension.register_extensions(SimpleNamespace(
        register_agent_server_client=registered.append,
        register_third_agent=lambda extension: None,
    ))
    try:
        assert created[:1] == [("http://agentos-iam", 7.0, created[0][2])]
        assert extensions[0].get_client()._auth_client is created[0][2]
    finally:
        await extensions[0].shutdown()
