"""条目级 ssl_verify：动态 token 网关取 token 时跳过证书校验（回归覆盖）。

``ssl_verify`` 与 ``dynamic_token_url`` 同处下发（``mcp_entry`` 透传键）、
**绑定生效**：仅动态 token 网关显式配置 ``False`` 时，取动态 token 的 HTTP
请求跳过证书校验；缺省 / 未配 dynamic_token_url 一律默认校验。
"""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace

import httpx
import pytest

from openjiuwen.core.foundation.tool.auth import auth_callback

from jiuwenswarm.common.mcp_config import build_mcp_server_config
from jiuwenswarm.common.mcp_dynamic_token_patch import (
    SSL_VERIFY_ENTRY_KEY,
    DynamicTokenAuthStrategy,
    DynamicTokenProvider,
    _registry_ssl_verify,
    _reset_dynamic_token_state_for_tests,
    apply_dispatched_credentials,
    apply_mcp_dynamic_token_patch,
    fetch_dynamic_token,
    mark_dynamic_token_server,
)


@pytest.fixture(autouse=True)
def _isolate_state():
    saved = dict(auth_callback.AuthStrategyRegistry._strategies)
    yield
    auth_callback.AuthStrategyRegistry._strategies.clear()
    auth_callback.AuthStrategyRegistry._strategies.update(saved)
    _reset_dynamic_token_state_for_tests()


def _dispatch_credentials() -> None:
    apply_dispatched_credentials(
        SimpleNamespace(
            agent_template={
                "data": {
                    "custom_config": [
                        {"client_id": "demo", "client_secret": "secret"}
                    ]
                }
            },
            resource_id="bot-a",
            ref_template_id="tpl-a",
        )
    )


def test_build_config_binds_ssl_verify_to_dynamic_token_url():
    """绑定收敛在唯一落点：配了 dynamic_token_url 才落 params。"""
    bound = build_mcp_server_config({
        "name": "gw",
        "transport": "streamable-http",
        "url": "https://gw/mcp",
        "dynamic_token_url": "https://kc.example/token",
        SSL_VERIFY_ENTRY_KEY: False,
    })
    assert bound is not None and bound.params.get(SSL_VERIFY_ENTRY_KEY) is False

    for partial in (
        # 未配 dynamic_token_url：不生效
        {"name": "gw", "transport": "streamable-http", "url": "https://gw/mcp",
         SSL_VERIFY_ENTRY_KEY: False},
        # 未显式 false：不生效
        {"name": "gw", "transport": "streamable-http", "url": "https://gw/mcp",
         "dynamic_token_url": "https://kc.example/token"},
        # 字符串 "false" 不是显式 false：不生效
        {"name": "gw", "transport": "streamable-http", "url": "https://gw/mcp",
         "dynamic_token_url": "https://kc.example/token",
         SSL_VERIFY_ENTRY_KEY: "false"},
    ):
        cfg = build_mcp_server_config(partial)
        assert cfg is not None and SSL_VERIFY_ENTRY_KEY not in cfg.params


def test_registry_carries_ssl_verify():
    """mark 登记 ssl_verify，strategy 按 server_id 现读（未标记默认校验）。"""
    assert mark_dynamic_token_server(
        "srv-1", "https://kc.example/token", ssl_verify=False
    )
    assert _registry_ssl_verify("srv-1") is False
    assert _registry_ssl_verify("srv-other") is True

    assert mark_dynamic_token_server("srv-1", "https://kc.example/token")
    assert _registry_ssl_verify("srv-1") is True


@pytest.mark.asyncio
async def test_strategy_passes_registered_ssl_verify_to_provider():
    _dispatch_credentials()
    apply_mcp_dynamic_token_patch()
    assert mark_dynamic_token_server(
        "srv-1", "https://kc.example/token", ssl_verify=False
    )
    auth_config = SimpleNamespace(tool_id="srv-1", config={})

    result = await DynamicTokenAuthStrategy().authenticate(auth_config)

    assert result.success is True
    provider = result.auth_data["auth_provider"]
    assert isinstance(provider, DynamicTokenProvider)
    assert provider._ssl_verify is False


def test_token_client_skips_verification_only_when_configured(monkeypatch):
    """取 token 的 httpx 客户端按 ssl_verify 构造（CERT_NONE / CERT_REQUIRED）。"""

    async def _fake_request_token(client, token_url, creds):
        return "tok"

    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_dynamic_token_patch._request_token", _fake_request_token
    )
    _dispatch_credentials()

    async def _verify_mode(ssl_verify: bool) -> ssl.VerifyMode:
        provider = DynamicTokenProvider(
            {}, {}, "https://kc.example/token", "srv-1", ssl_verify=ssl_verify
        )
        try:
            assert await provider._fetch_token() == "tok"
            transport = provider._client._transport_for_url(
                httpx.URL("https://kc.example/token")
            )
            return transport._pool._ssl_context.verify_mode
        finally:
            await provider.aclose()

    assert asyncio.run(_verify_mode(False)) == ssl.CERT_NONE
    assert asyncio.run(_verify_mode(True)) == ssl.CERT_REQUIRED


def test_fetch_dynamic_token_passes_verify(monkeypatch):
    """preflight 一次性取 token 路径：ssl_verify=False → verify=False。"""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return httpx.Response(200, text='{"access_token": "tok"}')

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    token = asyncio.run(
        fetch_dynamic_token(
            "https://kc.example/token",
            SimpleNamespace(client_id="demo", client_secret="secret"),
            ssl_verify=False,
        )
    )

    assert token == "tok"
    assert captured["verify"] is False
