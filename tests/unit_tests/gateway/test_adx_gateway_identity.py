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
    registry = SimpleNamespace(register_agent_server_client=registered.append)
    extensions = await extension.register_extensions(registry)
    if backend == "agentos_router":
        assert extensions == registered == []
        return
    assert len(extensions) == 1 and extensions == registered
    assert isinstance(extensions[0].get_client(), AdxRouterClient)
    await extensions[0].shutdown()


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
    ))
    try:
        assert created[:1] == [("http://agentos-iam", 7.0, created[0][2])]
        assert extensions[0].get_client()._auth_client is created[0][2]
    finally:
        await extensions[0].shutdown()
