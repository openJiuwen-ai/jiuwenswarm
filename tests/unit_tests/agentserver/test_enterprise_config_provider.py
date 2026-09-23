from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.memory import config as memory_config
from jiuwenswarm.common import config_provider as providers
from jiuwenswarm.server.runtime import enterprise_config
from jiuwenswarm.server.runtime.enterprise_config.schemas import (
    EffectiveEnterpriseConfig,
    RoutingContext,
)


@pytest.fixture(autouse=True)
def reset_provider_state():
    providers.reset_config_provider()
    memory_config.clear_embed_config_db_cache()
    memory_config.clear_memory_config_db_cache()
    yield
    providers.reset_config_provider()
    memory_config.clear_embed_config_db_cache()
    memory_config.clear_memory_config_db_cache()


def _policy() -> EffectiveEnterpriseConfig:
    return EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="group-a", bot_id="bot-a", user_id="user-a"),
        models={
            "default_model": [
                {
                    "template_id": "model-template-a",
                    "template_name": "enterprise-default",
                    "api_base": "https://models.example.com/v1",
                    "api_key": "secret",
                    "model_id": "enterprise-model",
                    "model_provider": "OpenAI",
                    "parameters": {"temperature": 0.2},
                }
            ]
        },
        mcp=[],
    )


@pytest.mark.asyncio
async def test_default_enterprise_provider_merges_models_mcp_and_memory(
    monkeypatch,
):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    policy = _policy()

    async def load_policy(request, slots):
        assert request.request_id == "request-a"
        assert slots
        return policy

    monkeypatch.setattr(
        enterprise_config,
        "load_effective_enterprise_config",
        load_policy,
    )
    memory_config.apply_memory_config_payload(
        {"op": "upsert", "body": {"enabled": True}}
    )
    result = await providers.resolve_agent_config(
        {
            "react": {},
            "models": {},
            "memory": {"enabled": False},
            "mcp": {"servers": [{"name": "local"}]},
        },
        request=SimpleNamespace(request_id="request-a", metadata={}),
    )

    assert result.policy is policy
    assert result.config["models"]["default"]["model_client_config"]["model_name"] == (
        "enterprise-model"
    )
    assert result.config["memory"]["enabled"] is True
    assert result.config["mcp"]["servers"] == []


def test_missing_enterprise_policy_clears_local_mcp(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(providers, "is_enterprise", lambda: True)
    merged = providers._apply_enterprise_policy(
        {
            "mcp": {
                "servers": [
                    {
                        "name": "local",
                        "transport": "stdio",
                        "command": "echo",
                    }
                ]
            }
        },
        None,
    )
    assert merged["mcp"]["servers"] == []


@pytest.mark.asyncio
async def test_enterprise_edition_can_force_yaml_without_policy_merge(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "yaml")

    async def unexpected_load(*args, **kwargs):
        raise AssertionError("enterprise policy must not load in yaml mode")

    monkeypatch.setattr(
        enterprise_config,
        "load_effective_enterprise_config",
        unexpected_load,
    )
    base = {"mcp": {"servers": [{"name": "local"}]}}
    result = await providers.resolve_agent_config(
        base,
        request=SimpleNamespace(request_id="request-a", metadata={}),
    )
    assert result.config is base
    assert result.policy is None


@pytest.mark.asyncio
async def test_cached_policy_is_reused_without_request(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    policy = _policy()

    result = await providers.resolve_agent_config(
        {"react": {}, "models": {}, "mcp": {"servers": []}},
        cached_policy=policy,
    )

    assert result.policy is policy
    assert result.config["react"]["model_name"] == "enterprise-model"


@pytest.mark.asyncio
async def test_refresh_config_source_refreshes_enterprise_memory(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    calls = 0

    async def reload_memory():
        nonlocal calls
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(
        memory_config,
        "reload_memory_config_from_gateway_db",
        reload_memory,
    )
    await providers.refresh_config_source()
    assert calls == 1


@pytest.mark.asyncio
async def test_refresh_without_plugin_override_uses_enterprise_default(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    events = []

    class Provider(providers.ConfigProvider):
        name = "no-refresh"

    async def reload_memory():
        events.append("memory")

    monkeypatch.setattr(
        enterprise_config,
        "invalidate_enterprise_config_caches",
        lambda: events.append("enterprise-cache"),
    )
    monkeypatch.setattr(
        memory_config,
        "reload_memory_config_from_gateway_db",
        reload_memory,
    )
    providers.register_config_provider(Provider())

    await providers.refresh_config_source()

    assert events == ["enterprise-cache", "memory"]


@pytest.mark.asyncio
async def test_plugin_refresh_takes_precedence_over_enterprise_default(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    events = []

    class Provider(providers.ConfigProvider):
        name = "custom-refresh"

        async def refresh(self):
            events.append("plugin")

    monkeypatch.setattr(
        enterprise_config,
        "invalidate_enterprise_config_caches",
        lambda: events.append("enterprise-cache"),
    )
    providers.register_config_provider(Provider())

    await providers.refresh_config_source()

    assert events == ["plugin"]


@pytest.mark.asyncio
async def test_plugin_refresh_failure_falls_back_to_enterprise_default(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    events = []

    class Provider(providers.ConfigProvider):
        name = "broken-refresh"

        async def refresh(self):
            events.append("plugin")
            raise RuntimeError("refresh failed")

    async def reload_memory():
        events.append("memory")

    monkeypatch.setattr(
        enterprise_config,
        "invalidate_enterprise_config_caches",
        lambda: events.append("enterprise-cache"),
    )
    monkeypatch.setattr(
        memory_config,
        "reload_memory_config_from_gateway_db",
        reload_memory,
    )
    providers.register_config_provider(Provider())

    await providers.refresh_config_source()

    assert events == ["plugin", "enterprise-cache", "memory"]
