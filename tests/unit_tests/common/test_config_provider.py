import asyncio
import logging
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import config_provider as providers


@pytest.fixture(autouse=True)
def reset_provider():
    providers.reset_config_provider()
    config_module.clear_config_cache()
    yield
    providers.reset_config_provider()
    config_module.clear_config_cache()


def test_yaml_preserves_base(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "yaml")
    base = {"mcp": {"servers": [{"name": "local"}]}}
    result = asyncio.run(providers.resolve_agent_config(base))
    assert result.config is base
    assert result.policy is None


def test_plugin_is_used_in_yaml_mode(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "yaml")

    class Provider(providers.ConfigProvider):
        name = "injected"

        async def load_agent_config(self, ctx, *, base):
            assert ctx.agent_id == "agent-a"
            return providers.AgentConfigResult({"injected": True})

    providers.register_config_provider(Provider())
    result = asyncio.run(providers.resolve_agent_config({}, agent_id="agent-a"))
    assert result.config == {"injected": True}


@pytest.mark.parametrize("fails", [False, True])
def test_plugin_falls_back(monkeypatch, fails):
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "yaml")

    class Provider(providers.ConfigProvider):
        name = "fallback"

        async def load_agent_config(self, ctx, *, base):
            if fails:
                raise RuntimeError("unavailable")

    providers.register_config_provider(Provider())
    base = {"local": True}
    assert asyncio.run(providers.resolve_agent_config(base)).config is base


def test_community_enterprise_selection_falls_back(monkeypatch):
    monkeypatch.setattr(providers, "is_enterprise", lambda: False)
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "enterprise")
    assert providers.resolve_config_source() is providers.ConfigSource.YAML


def test_config_source_priority(monkeypatch):
    monkeypatch.setattr(providers, "is_enterprise", lambda: True)
    monkeypatch.setattr(
        config_module,
        "get_merged_config_dict",
        lambda: {"config": {"source": "enterprise"}},
    )
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "yaml")
    assert providers.resolve_config_source() is providers.ConfigSource.YAML

    monkeypatch.delenv("JIUWENSWARM_CONFIG_SOURCE")
    assert providers.resolve_config_source() is providers.ConfigSource.ENTERPRISE

    monkeypatch.setattr(config_module, "get_merged_config_dict", dict)
    assert providers.resolve_config_source() is providers.ConfigSource.ENTERPRISE


def test_process_provider_is_completed_resolved_normalized_and_cached(monkeypatch):
    calls = 0

    class Provider(providers.ConfigProvider):
        name = "process"

        def get_process_config(self):
            nonlocal calls
            calls += 1
            return {"injected": {"value": "before"}}

    monkeypatch.setattr(
        config_module,
        "resolve_env_vars",
        lambda config: {**config, "env_resolved": True},
    )
    monkeypatch.setattr(
        config_module,
        "_normalize_config",
        lambda config: config.update(normalized=True),
    )
    providers.register_config_provider(Provider())

    first = config_module.get_config()
    second = config_module.get_config()
    assert first is second
    assert first["injected"] == {"value": "before"}
    assert first["version"]
    assert first["env_resolved"] is True
    assert first["normalized"] is True
    assert calls == 1


def test_process_provider_exception_falls_back_to_yaml(monkeypatch, caplog):
    class Provider(providers.ConfigProvider):
        name = "broken"

        def get_process_config(self):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(
        config_module,
        "get_merged_config_dict",
        lambda: {"from_yaml": True},
    )
    monkeypatch.setattr(config_module, "resolve_env_vars", lambda config: config)
    monkeypatch.setattr(config_module, "_normalize_config", lambda config: None)
    providers.register_config_provider(Provider())

    logger = logging.getLogger("jiuwenswarm.common.config")
    logger.addHandler(caplog.handler)
    try:
        assert config_module.get_config() == {"from_yaml": True}
        assert "fallback to yaml" in caplog.text
    finally:
        logger.removeHandler(caplog.handler)


def test_set_config_uses_optional_provider_writer():
    written = []

    class Provider(providers.ConfigProvider):
        name = "writable"

        def write_process_config(self, config):
            written.append(config)

    providers.register_config_provider(Provider())
    config_module.set_config({"value": 1})
    assert written == [{"value": 1}]


def test_agent_context_includes_request_routing(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_CONFIG_SOURCE", "yaml")

    class Provider(providers.ConfigProvider):
        name = "routing"

        async def load_agent_config(self, ctx, *, base):
            assert ctx.routing == {
                "user_id": "user-a",
                "group_id": "group-a",
                "bot_id": "bot-a",
            }
            return providers.AgentConfigResult(base)

    request = SimpleNamespace(
        metadata={
            "user_id": "user-a",
            "routing": {"group_id": "group-a", "bot_id": "bot-a"},
        }
    )
    providers.register_config_provider(Provider())
    asyncio.run(providers.resolve_agent_config({}, request=request))
