import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_OJ_MEMORY_MANAGER_MODULE = "openjiuwen.core.memory.lite.manager"


@contextlib.contextmanager
def _maybe_patch_aclose_memory_cache():
    import importlib

    mod = importlib.import_module(_OJ_MEMORY_MANAGER_MODULE)
    if hasattr(mod, "aclose_memory_manager_cache"):
        with patch(
            f"{_OJ_MEMORY_MANAGER_MODULE}.aclose_memory_manager_cache",
            AsyncMock(),
        ):
            yield
    else:
        yield

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime import agent_manager as agent_manager_module


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class FakeAgent:
    def __init__(self):
        self.reload_calls = []

    async def reload_agent_config(self, *args, **kwargs):
        if args:
            self.reload_calls.append({"args": args, "kwargs": kwargs})
        else:
            self.reload_calls.append(kwargs)

    async def start_interaction(self, session_id: str) -> None:
        """Match JiuWenSwarmDeepAdapter.start_interaction for session-pool tests."""
        _ = session_id
        return None


class FailingReloadAgent(FakeAgent):
    async def reload_agent_config(self, *args, **kwargs):
        await super().reload_agent_config(*args, **kwargs)
        raise RuntimeError("reload failed")


class FakeTeamManager:
    def __init__(self, channel_id, calls):
        self.channel_id = channel_id
        self.calls = calls

    async def update_evolution_config(self, config):
        self.calls.append((self.channel_id, config))


@pytest.mark.asyncio
async def test_reload_agents_config_limits_reload_to_explicit_channel_and_session(monkeypatch):
    manager = agent_manager_module.AgentManager()
    tui_agent = FakeAgent()
    web_agent = FakeAgent()
    manager.agents = {
        "tui": {"code": tui_agent},
        "web": {"agent": web_agent},
    }
    team_updates = []
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: FakeTeamManager(channel_id, team_updates),
    )

    config = {"models": {"defaults": []}}
    env = {"MODEL_NAME": "GLM-5"}
    await manager.reload_agents_config(
        config,
        env,
        target_channel_id="tui",
        target_session_id="tui_session_1",
    )

    assert tui_agent.reload_calls == [
        {
            "config_base": config,
            "env_overrides": env,
            "target_session_id": "tui_session_1",
        }
    ]
    assert web_agent.reload_calls == []
    assert team_updates == [("tui", config)]


@pytest.mark.asyncio
async def test_reload_agents_config_skips_duplicate_global_reload(monkeypatch):
    manager = agent_manager_module.AgentManager()
    agent = FakeAgent()
    manager.agents = {"web": {"agent": agent}}
    team_updates = []
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: FakeTeamManager(channel_id, team_updates),
    )

    config = {"models": {"defaults": [{"model_name": "deepseek"}]}}
    await manager.reload_agents_config(config, {})
    await manager.reload_agents_config({"models": {"defaults": [{"model_name": "deepseek"}]}}, {})

    assert len(agent.reload_calls) == 1
    assert team_updates == [("web", config)]


@pytest.mark.asyncio
async def test_reload_agents_config_retries_same_reload_after_team_update_failure(monkeypatch):
    manager = agent_manager_module.AgentManager()
    agent = FakeAgent()
    manager.agents = {"web": {"agent": agent}}

    class FlakyTeamManager:
        def __init__(self):
            self.calls = []

        async def update_evolution_config(self, config):
            self.calls.append(config)
            if len(self.calls) == 1:
                raise RuntimeError("temporary team update failure")

    team_manager = FlakyTeamManager()
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: team_manager,
    )

    config = {"models": {"defaults": [{"model_name": "deepseek"}]}}
    await manager.reload_agents_config(config, {})
    await manager.reload_agents_config({"models": {"defaults": [{"model_name": "deepseek"}]}}, {})

    assert len(agent.reload_calls) == 2
    assert team_manager.calls == [config, config]


@pytest.mark.asyncio
async def test_reload_agents_config_reloads_when_agent_set_changes(monkeypatch):
    manager = agent_manager_module.AgentManager()
    first_agent = FakeAgent()
    second_agent = FakeAgent()
    manager.agents = {"web": {"first": first_agent}}
    team_updates = []
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: FakeTeamManager(channel_id, team_updates),
    )

    config = {"models": {"defaults": [{"model_name": "deepseek"}]}}
    await manager.reload_agents_config(config, {})
    manager.agents["web"]["second"] = second_agent
    await manager.reload_agents_config({"models": {"defaults": [{"model_name": "deepseek"}]}}, {})

    assert len(first_agent.reload_calls) == 2
    assert len(second_agent.reload_calls) == 1
    assert team_updates == [("web", config), ("web", config)]


@pytest.mark.asyncio
async def test_reload_agents_config_reloads_when_agent_instance_changes(monkeypatch):
    manager = agent_manager_module.AgentManager()
    old_agent = FakeAgent()
    new_agent = FakeAgent()
    manager.agents = {"web": {"agent": old_agent}}
    team_updates = []
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: FakeTeamManager(channel_id, team_updates),
    )

    config = {"models": {"defaults": [{"model_name": "deepseek"}]}}
    await manager.reload_agents_config(config, {})
    manager.agents["web"]["agent"] = new_agent
    await manager.reload_agents_config({"models": {"defaults": [{"model_name": "deepseek"}]}}, {})

    assert len(old_agent.reload_calls) == 1
    assert len(new_agent.reload_calls) == 1
    assert team_updates == [("web", config), ("web", config)]


@pytest.mark.asyncio
async def test_reload_agents_config_resolves_config_once_when_config_none(monkeypatch):
    manager = agent_manager_module.AgentManager()
    agent = FakeAgent()
    manager.agents = {"web": {"agent": agent}}
    team_updates = []
    config = {"models": {"defaults": [{"model_name": "from-file"}]}}
    get_config_calls = []
    monkeypatch.setattr(
        agent_manager_module,
        "get_config",
        lambda: get_config_calls.append(True) or config,
    )
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: FakeTeamManager(channel_id, team_updates),
    )

    await manager.reload_agents_config(None, {})

    assert get_config_calls == [True]
    assert agent.reload_calls == [{"config_base": config, "env_overrides": {}}]
    assert team_updates == [("web", config)]


@pytest.mark.asyncio
async def test_agent_reload_config_handler_passes_explicit_scope(monkeypatch):
    from jiuwenswarm.agents.harness import team as team_harness_module

    server = agent_ws_server_module.AgentWebSocketServer()
    calls = []
    stop_paused = AsyncMock(return_value=2)

    async def fake_reload(config, env, **kwargs):
        calls.append((config, env, kwargs))

    monkeypatch.setattr(server._agent_manager, "reload_agents_config", fake_reload)
    monkeypatch.setattr(
        team_harness_module,
        "stop_all_paused_team_session_runtimes_across_managers",
        stop_paused,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "ok": resp.ok,
            "payload": resp.payload,
        },
    )

    request = AgentRequest(
        request_id="reload-1",
        channel_id="cli",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params={
            "config": {"models": {"defaults": []}},
            "env": {},
            "target_channel_id": "tui",
            "target_session_id": "tui_session_1",
        },
    )

    ws = FakeWebSocket()
    await server._handle_agent_reload_config(ws, request, asyncio.Lock())

    assert calls == [
        (
            {"models": {"defaults": []}},
            {},
            {
                "target_channel_id": "tui",
                "target_session_id": "tui_session_1",
            },
        )
    ]
    stop_paused.assert_awaited_once_with(reason="agent.reload_config: ")


@pytest.mark.asyncio
async def test_agent_reload_config_handler_skips_agent_manager_for_web_ui_scope(monkeypatch):
    from jiuwenswarm.agents.harness import team as team_harness_module

    server = agent_ws_server_module.AgentWebSocketServer()
    reload_agents = AsyncMock()
    stop_paused = AsyncMock(return_value=0)
    monkeypatch.setattr(server._agent_manager, "reload_agents_config", reload_agents)
    monkeypatch.setattr(
        team_harness_module,
        "stop_all_paused_team_session_runtimes_across_managers",
        stop_paused,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "ok": resp.ok,
            "payload": resp.payload,
        },
    )

    request = AgentRequest(
        request_id="reload-ui",
        channel_id="web",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params={
            "config": {"a2ui": {"enabled": True}},
            "env": {},
            "reload_scopes": ["web_ui"],
        },
    )

    ws = FakeWebSocket()
    await server._handle_agent_reload_config(ws, request, asyncio.Lock())

    reload_agents.assert_not_awaited()
    stop_paused.assert_not_awaited()
    assert json.loads(ws.sent[-1])["ok"] is True


@pytest.mark.asyncio
async def test_agent_reload_config_handler_applies_proactive_scope_without_agent_reload(monkeypatch):
    server = agent_ws_server_module.AgentWebSocketServer()
    reload_agents = AsyncMock()
    proactive_engine = MagicMock()
    server._proactive_engine = proactive_engine

    monkeypatch.setattr(server._agent_manager, "reload_agents_config", reload_agents)
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_config",
        lambda: {"proactive_recommendation": {"enabled": True}},
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "ok": resp.ok,
            "payload": resp.payload,
        },
    )

    request = AgentRequest(
        request_id="reload-proactive",
        channel_id="web",
        req_method=ReqMethod.AGENT_RELOAD_CONFIG,
        params={
            "config": {"proactive_recommendation": {"enabled": True}},
            "env": {},
            "reload_scopes": ["proactive"],
        },
    )

    ws = FakeWebSocket()
    await server._handle_agent_reload_config(ws, request, asyncio.Lock())

    reload_agents.assert_not_awaited()
    proactive_engine.reload_config.assert_called_once_with({"enabled": True})
    assert json.loads(ws.sent[-1])["ok"] is True


def test_deep_adapter_reload_session_scope_selects_only_target_session():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter()
    session_a = object()
    session_b = object()
    adapter._session_adapters = {
        "tui_session_a": session_a,
        "tui_session_b": session_b,
    }

    assert list(adapter._iter_session_adapters_for_reload("tui_session_b")) == [
        ("tui_session_b", session_b)
    ]
    assert list(adapter._iter_session_adapters_for_reload(None)) == [
        ("tui_session_a", session_a),
        ("tui_session_b", session_b),
    ]


@pytest.mark.asyncio
async def test_deep_adapter_global_reload_marks_sessions_stale_without_fanout(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    parent = JiuWenSwarmDeepAdapter()
    parent._instance = MagicMock()
    session_a = FakeAgent()
    session_b = FakeAgent()
    parent._session_adapters = {
        "session-a": session_a,
        "session-b": session_b,
    }

    async def _async_noop(*args, **kwargs):
        return None

    with (
        patch.object(interface_module, "clear_config_cache", MagicMock()),
        _maybe_patch_aclose_memory_cache(),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_handle_memory_rail_by_config", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_refresh_multimodal_configs", MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_model", MagicMock(return_value=object())),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_multimodal_tools_for_runtime", MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_paid_search_tool_for_runtime", MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_symphony_tools_for_runtime", MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_skill_retrieval_tools_for_runtime", MagicMock()),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_skill_retrieval_prompt_rail_for_runtime",
            AsyncMock(),
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_filesystem_rail_enabled_for_profile", MagicMock(return_value=True)),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "load_user_rails", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_get_current_agent_rails", MagicMock(return_value=[])),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_make_deep_agent_config", MagicMock(return_value=object())),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_active_evolution_review_agent_after_reload", MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_mcp_servers_for_runtime", _async_noop),
    ):
        await parent.reload_agent_config(
            {"react": {"agent_name": "main_agent"}, "browser": {"headless": True}},
            {},
        )

    assert session_a.reload_calls == []
    assert session_b.reload_calls == []


def _fake_deep_reload_model():
    return SimpleNamespace(
        model_client_config={"api_base": "https://example.test/v1", "api_key": "secret"},
        model_config={"model_name": "glm-5", "temperature": 0.95},
    )


def _real_deep_reload_model(api_base: str, model_name: str):
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

    return Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_key="secret",
            api_base=api_base,
            verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model_name=model_name),
    )


async def _reload_deep_adapter_config_for_test(previous_config, deep_config_factory):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    def _create_model(self, config_base):
        return _fake_deep_reload_model()

    async def _async_noop(*args, **kwargs):
        return None

    def _make_config(self, *, model, config, config_base, agent_card, tool_cards, rails):
        return deep_config_factory(model, agent_card, tool_cards, rails)

    configured_fields = []
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = MagicMock()
    adapter._instance._deep_config = previous_config

    def _configure(cfg):
        configured_fields.append((cfg.model, cfg.system_prompt))
        adapter._instance._deep_config = cfg

    adapter._instance.configure = MagicMock(side_effect=_configure)

    with (
        patch.object(interface_module, "clear_config_cache", MagicMock()),
        _maybe_patch_aclose_memory_cache(),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_handle_memory_rail_by_config", AsyncMock()),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_refresh_multimodal_configs",
            MagicMock(),
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_model", _create_model),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_multimodal_tools_for_runtime",
            MagicMock(),
        ),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_paid_search_tool_for_runtime",
            MagicMock(),
        ),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_symphony_tools_for_runtime",
            MagicMock(),
        ),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_skill_retrieval_tools_for_runtime",
            MagicMock(),
        ),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_skill_retrieval_prompt_rail_for_runtime",
            AsyncMock(),
        ),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_filesystem_rail_enabled_for_profile",
            MagicMock(return_value=True),
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "load_user_rails", AsyncMock()),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_get_current_agent_rails",
            MagicMock(return_value=[]),
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_make_deep_agent_config", _make_config),
        patch.object(
            interface_module.JiuWenSwarmDeepAdapter,
            "_sync_active_evolution_review_agent_after_reload",
            MagicMock(),
        ),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_sync_mcp_servers_for_runtime", _async_noop),
    ):
        await adapter.reload_agent_config(
            {"react": {"agent_name": "main_agent"}, "browser": {"headless": True}},
            {},
        )

    return adapter, configured_fields


def test_deep_adapter_rejects_invalid_default_model_even_when_other_cached_model_is_valid():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter()
    invalid_default = _real_deep_reload_model("https://api.example.com/v1", "bad-default")
    valid_other = _real_deep_reload_model("https://real.provider.test/v1", "good-model")
    adapter._model = invalid_default
    adapter._model_cache = {
        "bad-default#0": invalid_default,
        "good-model#0": valid_other,
    }
    adapter._model_name_to_keys = {
        "bad-default": ["bad-default#0"],
        "good-model": ["good-model#0"],
    }

    assert adapter._has_valid_model_config("") is False


def test_deep_adapter_resolve_model_for_request_fails_when_no_model_is_available():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        AgentRequest,
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter()
    request = AgentRequest(
        request_id="req-no-model",
        channel_id="test",
        params={"model_name": "missing"},
    )

    with pytest.raises(RuntimeError, match="No model configured"):
        adapter._resolve_model_for_request(request)


def test_deep_adapter_model_config_fingerprint_includes_legacy_react_model_fields():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    old_config = {
        "react": {
            "model_client_config": {
                "api_base": "https://real.provider.test/v1",
                "api_key": "secret",
            },
            "model_name": "old-model",
            "model_config_obj": {"temperature": 0.1},
        }
    }
    new_config = {
        "react": {
            "model_client_config": {
                "api_base": "https://real.provider.test/v1",
                "api_key": "secret",
            },
            "model_name": "new-model",
            "model_config_obj": {"temperature": 0.9},
        }
    }

    assert (
        JiuWenSwarmDeepAdapter._models_config_fingerprint(old_config)
        != JiuWenSwarmDeepAdapter._models_config_fingerprint(new_config)
    )


@pytest.mark.asyncio
async def test_deep_adapter_reload_omits_unchanged_model_and_system_prompt():
    from openjiuwen.harness import DeepAgentConfig

    def _new_config(model, agent_card, tool_cards, rails):
        return DeepAgentConfig(
            model=model,
            card=agent_card,
            system_prompt="identity prompt",
            tools=tool_cards,
            rails=rails,
        )

    adapter, configured_fields = await _reload_deep_adapter_config_for_test(
        DeepAgentConfig(
            model=_fake_deep_reload_model(),
            system_prompt="identity prompt",
        ),
        _new_config,
    )

    assert configured_fields == [(None, None)]
    assert adapter._instance._deep_config.model is not None
    assert adapter._instance._deep_config.system_prompt == "identity prompt"


@pytest.mark.asyncio
async def test_deep_adapter_reload_restores_omitted_fields_to_stored_config_object():
    from openjiuwen.harness import DeepAgentConfig

    def _new_config(model, agent_card, tool_cards, rails):
        return DeepAgentConfig(
            model=model,
            card=agent_card,
            system_prompt="identity prompt",
            tools=tool_cards,
            rails=rails,
        )

    previous_config = DeepAgentConfig(
        model=_fake_deep_reload_model(),
        system_prompt="identity prompt",
    )
    adapter, configured_fields = await _reload_deep_adapter_config_for_test(
        previous_config,
        _new_config,
    )

    assert configured_fields == [(None, None)]
    assert adapter._instance._deep_config.model is not None
    assert adapter._instance._deep_config.system_prompt == "identity prompt"


@pytest.mark.asyncio
async def test_deep_adapter_reload_keeps_fields_when_dependent_reload_inputs_change():
    from openjiuwen.harness import DeepAgentConfig

    def _new_config(model, agent_card, tool_cards, rails):
        return DeepAgentConfig(
            model=model,
            card=agent_card,
            system_prompt="identity prompt",
            context_engine_config={"changed": True},
            max_iterations=20,
            language="en",
            prompt_mode="code",
            tools=tool_cards,
            rails=rails,
        )

    _, configured_fields = await _reload_deep_adapter_config_for_test(
        DeepAgentConfig(
            model=_fake_deep_reload_model(),
            system_prompt="identity prompt",
            context_engine_config={"changed": False},
            max_iterations=15,
            language="cn",
            prompt_mode=None,
        ),
        _new_config,
    )

    assert len(configured_fields) == 1
    assert configured_fields[0][0] is not None
    assert configured_fields[0][1] == "identity prompt"


@pytest.mark.asyncio
async def test_deep_adapter_existing_session_lazy_reload_once(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    parent = JiuWenSwarmDeepAdapter()
    parent._instance = MagicMock()
    session = FakeAgent()
    parent._session_adapters = {"session-a": session}
    pending_config = {"react": {"agent_name": "main_agent"}, "browser": {"headless": True}}
    parent._mark_session_adapters_stale_for_reload(pending_config, {"MODEL_NAME": "new-model"})

    first_lookup = await parent._get_or_create_session_adapter("session-a")
    second_lookup = await parent._get_or_create_session_adapter("session-a")

    assert first_lookup is session
    assert second_lookup is session
    assert len(session.reload_calls) == 1
    call = session.reload_calls[0]
    assert call["args"][0] == pending_config
    assert call["args"][1] == {"MODEL_NAME": "new-model"}
    assert call["kwargs"]["target_session_id"] == "session-a"
    assert parent._session_adapter_versions["session-a"] == 1


@pytest.mark.asyncio
async def test_deep_adapter_failed_lazy_reload_is_not_retried_immediately(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    parent = JiuWenSwarmDeepAdapter()
    parent._instance = MagicMock()
    session = FailingReloadAgent()
    parent._session_adapters = {"session-a": session}
    pending_config = {"react": {"agent_name": "main_agent"}, "browser": {"headless": True}}
    parent._mark_session_adapters_stale_for_reload(pending_config, {})

    await parent._get_or_create_session_adapter("session-a")
    await parent._get_or_create_session_adapter("session-a")

    assert len(session.reload_calls) == 1
    assert parent._session_adapter_versions.get("session-a", 0) == 0


@pytest.mark.asyncio
async def test_deep_adapter_new_session_applies_pending_reload(monkeypatch):
    """A session adapter created after a global reload must reflect the pending config.

    The new adapter is built from ``_session_instance_config`` (which may predate the
    reload), so ``_get_or_create_session_adapter`` must apply the pending
    ``config_base`` once before returning it.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    async def _async_noop(*args, **kwargs):
        return None

    parent = JiuWenSwarmDeepAdapter()
    parent._instance = MagicMock()
    # Simulate a prior global reload that left a pending config_base (version=1).
    pending_config = {"react": {"agent_name": "main_agent"}, "browser": {"headless": True}}
    parent._mark_session_adapters_stale_for_reload(pending_config, {"MODEL_NAME": "new-model"})
    pending_config["react"]["agent_name"] = "mutated_after_mark"
    assert parent._session_adapter_config_version == 1

    new_session = FakeAgent()
    new_session.create_instance = _async_noop

    with patch.object(
        interface_module.JiuWenSwarmDeepAdapter,
        "_new_session_scoped_adapter",
        MagicMock(return_value=new_session),
    ):
        adapter = await parent._get_or_create_session_adapter("session-new")

    assert adapter is new_session
    # The pending reload was applied exactly once to the freshly created adapter.
    assert len(new_session.reload_calls) == 1
    call = new_session.reload_calls[0]
    # FakeAgent packs positional args into {"args": ..., "kwargs": ...}.
    args = call.get("args", ())
    assert args[0] == {"react": {"agent_name": "main_agent"}, "browser": {"headless": True}}
    assert args[1] == {"MODEL_NAME": "new-model"}
    assert call["kwargs"]["target_session_id"] == "session-new"
    # Version is caught up so the next lookup does not reload again.
    assert parent._session_adapter_versions["session-new"] == 1


@pytest.mark.asyncio
async def test_deep_adapter_new_session_skips_reload_when_no_pending(monkeypatch):
    """Without a pending global reload, creating a new session adapter does not reload."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    async def _async_noop(*args, **kwargs):
        return None

    parent = JiuWenSwarmDeepAdapter()
    parent._instance = MagicMock()
    # No global reload ever happened: version is 0, pending config_base is None.
    assert parent._session_adapter_config_version == 0
    assert parent._pending_session_reload_config_base is None

    new_session = FakeAgent()
    new_session.create_instance = _async_noop

    with patch.object(
        interface_module.JiuWenSwarmDeepAdapter,
        "_new_session_scoped_adapter",
        MagicMock(return_value=new_session),
    ):
        adapter = await parent._get_or_create_session_adapter("session-fresh")

    assert adapter is new_session
    assert new_session.reload_calls == []
    # No version entry is recorded at version 0 (``_reload_session_adapter_if_stale``
    # short-circuits on ``0 >= 0``); the next global reload bumps the version and the
    # missing entry (defaulting to 0) will correctly trigger a lazy reload then.
    assert "session-fresh" not in parent._session_adapter_versions
