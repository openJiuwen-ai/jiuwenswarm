# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MoA group compilation and verifier boundary contracts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.moa_runtime import (
    MoARuntime, build_moa_runtime, moa_status, resolve_moa_settings,
)


SELECTION = {"type": "model_group", "id": "mgp_moa"}


@pytest.fixture
def moa_config():
    return {
        "models": {
            "defaults": [{
                "model_id": f"mdl_{name}", "alias": f"Alias {name}",
                "is_default": name == "a",
                "model_client_config": {
                    "model_name": f"model-{name}", "client_provider": "OpenAI",
                    "api_base": "https://example.test/v1", "api_key": "private-credential",
                },
                "model_config_obj": {"temperature": 0.1, "max_tokens": 99},
            } for name in ("a", "b", "c")],
            "groups": [{
                "model_group_id": "mgp_moa", "enabled": True,
                "routes": [
                    {"route_id": "rte_a", "model_id": "mdl_a"},
                    {"route_id": "rte_b", "model_id": "mdl_b", "request_overrides": {"temperature": 0.7}},
                    {"route_id": "rte_c", "model_id": "mdl_c"},
                    {"route_id": "rte_missing", "model_id": "missing", "enabled": False},
                ],
                "request_config": {"temperature": 0.5, "max_tokens": 123},
            }],
        },
        "experimental": {"multi_model_collaboration_enabled": True, "multi_model_collaboration": {"jev_enabled": False, "verifier_interval": 3}},
        "moa": {"model_group_id": "old-group", "aggregator_route_id": "old-route", "verifier": {"type": "jev", "route_id": "old-route", "interval": 1}},
    }


@pytest.mark.asyncio
async def test_group_parameters_and_member_overrides_survive_compilation(moa_config):
    saved = deepcopy(moa_config)
    runtime = await build_moa_runtime(moa_config, SELECTION)
    assert runtime.model_name == "model-a"
    assert runtime.aggregator.model_config.temperature == 0.5
    assert runtime.aggregator.model_config.max_tokens == 123
    assert [model.model_config.model_name for model in runtime.config.reference_models] == ["model-b", "model-c"]
    assert runtime.config.reference_models[0].model_config.temperature == 0.7
    assert runtime.config.reference_models[1].model_config.temperature == 0.5
    assert runtime.config.scorer._model is runtime.aggregator
    assert runtime.config.mode == "verifier_routed"
    assert runtime.config.verifier.interval == 3
    assert runtime.config.verifier.n_evaluations == 3
    assert moa_config == saved


@pytest.mark.parametrize("jev_enabled", [False, True])
@pytest.mark.asyncio
async def test_single_enabled_member_uses_its_model_without_moa(moa_config, jev_enabled, monkeypatch):
    from openjiuwen.core.single_agent import AgentCard

    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    group = moa_config["models"]["groups"][0]
    group["routes"][0]["enabled"] = False
    group["routes"][2]["enabled"] = False
    moa_config["experimental"]["multi_model_collaboration"]["jev_enabled"] = jev_enabled
    if jev_enabled:
        monkeypatch.setenv("TYPESAFE_API_KEY", "encrypted-jev-key")
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_crypto_provider",
            lambda: SimpleNamespace(decrypt=lambda _: "test-jev-key"),
        )

    runtime = await build_moa_runtime(moa_config, SELECTION)
    assert runtime.model_name == "model-b"
    assert runtime.aggregator.model_config.temperature == 0.7
    assert runtime.config is None
    assert moa_status(moa_config, SELECTION)["configured"] is True

    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("single-member-group")
    adapter._moa_runtime = runtime
    adapter._session_instance_mode = "agent.work.normal"
    config = adapter._make_deep_agent_config(
        model=runtime.aggregator, config={}, config_base=moa_config,
        agent_card=AgentCard(id="single-member-group", name="main"), tool_cards=[], rails=[],
    )
    assert config.moa is None
    assert adapter._create_model({"models": {}}) is runtime.aggregator
    assert adapter._resolve_model_for_request(AgentRequest(
        request_id="single-member", session_id="single-member-group",
        params={"model_selection": SELECTION},
    )) is runtime.aggregator


@pytest.mark.asyncio
async def test_status_is_safe_and_archival_roles_never_control_verification(moa_config):
    assert moa_status(moa_config, SELECTION) == {
        "configured": True, "aggregator_model_name": "model-a", "aggregator_alias": "Alias a",
    }
    moa_config["moa"]["verifier"]["route_id"] = "rte_b"
    runtime = await build_moa_runtime(moa_config, SELECTION)
    assert runtime.config.scorer._model is runtime.aggregator
    assert runtime.model_name == "model-a"


@pytest.mark.asyncio
async def test_jev_stored_credential_is_decrypted_only_in_runtime(moa_config, monkeypatch):
    moa_config["experimental"]["multi_model_collaboration"].update({"jev_enabled": True, "jev": {"api_key": "encrypted-value"}})
    monkeypatch.setenv("TYPESAFE_API_KEY", "shared-encrypted-value")
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: SimpleNamespace(decrypt=lambda _: "resolved-secret"))
    runtime = await build_moa_runtime(moa_config, SELECTION)
    assert runtime.config.jev.client._api_key == "resolved-secret"
    assert moa_config["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == "encrypted-value"
    public = moa_status(moa_config, SELECTION)
    assert public["configured"] is True
    assert "resolved-secret" not in str(public)
    await runtime.aclose()


@pytest.mark.parametrize("change", ["group", "aggregator", "member", "interval"])
@pytest.mark.asyncio
async def test_stale_configuration_fails_before_an_invocation(moa_config, change):
    if change == "group":
        moa_config["models"]["groups"][0]["enabled"] = False
    elif change == "aggregator":
        moa_config["models"]["groups"][0]["routes"][0]["model_id"] = "missing"
    elif change == "member":
        moa_config["models"]["groups"][0]["routes"][1]["model_id"] = "deleted"
        moa_config["models"]["groups"][0]["routes"][2]["enabled"] = False
    else:
        moa_config["experimental"]["multi_model_collaboration"]["verifier_interval"] = 0
    assert moa_status(moa_config, SELECTION) == {"configured": False}
    with pytest.raises(Exception):
        await build_moa_runtime(moa_config, SELECTION)


def test_fingerprint_tracks_member_credentials_and_requests(moa_config):
    _, _, original = resolve_moa_settings(moa_config, SELECTION)
    moa_config["models"]["groups"][0]["request_config"]["temperature"] = 0.8
    _, _, changed = resolve_moa_settings(moa_config, SELECTION)
    assert changed != original
    moa_config["models"]["defaults"][1]["model_client_config"]["api_key"] = "rotated"
    assert resolve_moa_settings(moa_config, SELECTION)[2] != changed


@pytest.mark.parametrize("client_options", [
    {"auth_mode": "none"},
    {"auth_mode": "custom_headers", "custom_headers": {"Authorization": "test-header"}},
    {"auth_mode": "openai_account_oauth"},
    {"api_mode": "responses"},
    {"client_provider": "OpenAIAccount"},
])
def test_group_members_with_alternative_auth_do_not_require_api_key(
    moa_config: dict, client_options: dict,
) -> None:
    member = moa_config["models"]["defaults"][1]["model_client_config"]
    member.update(client_options)
    member["api_key"] = ""

    _, group, _ = resolve_moa_settings(moa_config, SELECTION)

    assert group.routes[1].model.model_id == "mdl_b"
    assert group.routes[1].model.api_key == ""


@pytest.mark.parametrize("api_key", ["", "  "])
def test_group_member_with_api_key_auth_rejects_blank_credential(moa_config: dict, api_key: str) -> None:
    moa_config["models"]["defaults"][1]["model_client_config"]["api_key"] = api_key

    with pytest.raises(ValueError, match="MoA member rte_b has no API credential"):
        resolve_moa_settings(moa_config, SELECTION)


@pytest.mark.asyncio
async def test_unrelated_work_reload_keeps_the_installed_moa_objects(moa_config, monkeypatch):
    from openjiuwen.core.single_agent import AgentCard
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    runtime = await build_moa_runtime(moa_config, SELECTION)
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("moa-reload")
    adapter._moa_runtime = runtime
    adapter._session_instance_mode = "agent.work.normal"
    monkeypatch.setattr(adapter, "_build_subagents_with_general_purpose", lambda **_: [])
    reloaded = adapter._make_deep_agent_config(
        model=runtime.aggregator, config={}, config_base=moa_config,
        agent_card=AgentCard(id="moa-reload", name="main"), tool_cards=[], rails=[],
    )
    assert reloaded.moa is runtime.config
    assert reloaded.moa.scorer is runtime.config.scorer
    assert adapter._create_model({"models": {}}) is runtime.aggregator


@pytest.mark.parametrize("ordinary", [{"type": "model", "id": "deleted"}, "malformed", None])
@pytest.mark.parametrize("profile", ["work", "code"])
@pytest.mark.asyncio
async def test_moa_validation_and_resolution_ignore_ordinary_selection(moa_config, monkeypatch, ordinary, profile):
    from jiuwenswarm.common.model_errors import ModelSelectionError
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    adapter = (JiuWenSwarmDeepAdapter if profile == "work" else JiuwenSwarmCodeAdapter)()
    runtime = await build_moa_runtime(moa_config, SELECTION)
    adapter._moa_runtime = runtime
    saved_selection = AsyncMock(side_effect=ModelSelectionError("MODEL_SELECTION_NOT_FOUND", "deleted ordinary selection"))
    # Synchronous getter is deliberately unusable. Enhanced requests must not
    # consult it, even at the pre-execution configuration-validation boundary.
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection", saved_selection)
    request = AgentRequest(request_id="moa-stable", session_id="moa-stable", params={"model_selection": ordinary})
    original = deepcopy(request.params)
    assert adapter._model_config_error(request) is None
    assert adapter._resolve_model_for_request(request) is runtime.aggregator
    assert adapter._resolve_model_by_name("model_group:deleted:gone") is runtime.aggregator
    assert request.params == original
    saved_selection.assert_not_called()


@pytest.mark.asyncio
async def test_moa_build_precedes_invalid_ordinary_default_group(moa_config, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    moa_config["models"]["groups"].insert(0, {
        "model_group_id": "invalid-default", "is_default": True,
        "routes": [{"route_id": "gone", "model_id": "deleted"}],
    })
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance_overrides = {"moa_model_selection": SELECTION}
    adapter._session_instance_mode = "agent.work.normal"
    ordinary_build = AsyncMock(side_effect=AssertionError("ordinary model must not be constructed"))
    monkeypatch.setattr(adapter, "_create_model", ordinary_build)
    selected = await adapter._prepare_moa_for_build(moa_config)
    assert selected is adapter._moa_runtime.aggregator
    assert selected.model_config.model_name == "model-a"
    assert adapter._model_client_config is selected.model_client_config
    assert adapter._model_request_config is selected.model_config
    ordinary_build.assert_not_called()


@pytest.mark.asyncio
async def test_work_public_creation_uses_moa_before_ordinary_selection(moa_config, monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    moa_config["models"]["groups"].insert(0, {
        "model_group_id": "invalid-default", "is_default": True,
        "routes": [{"route_id": "gone", "model_id": "deleted"}],
    })
    moa_config["symphony"] = {"enabled": False}
    adapter = interface_deep.JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("moa-public-build")
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(moa_config))
    monkeypatch.setattr(interface_deep, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(interface_deep, "load_dotenv_runtime", MagicMock())
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_get_tool_cards", AsyncMock(return_value=[]))
    for name in ["_try_init_a2x_client", "_register_mcp_servers_from_config", "_load_active_packages", "_load_rsi_active_harness", "load_user_rails"]:
        monkeypatch.setattr(adapter, name, AsyncMock())
    for name in ["_refresh_multimodal_configs", "_seed_runtime_cwd", "_sync_a2x_runtime_state", "_ensure_cron_tools_registered"]:
        monkeypatch.setattr(adapter, name, MagicMock())
    monkeypatch.setattr(adapter, "_build_agent_rails", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(adapter, "_build_subagents_with_general_purpose", lambda **_kwargs: [])
    monkeypatch.setattr(adapter, "_create_sys_operation", lambda: object())
    ordinary_build = MagicMock(side_effect=AssertionError("invalid ordinary default must not build"))
    monkeypatch.setattr(adapter, "_create_model", ordinary_build)
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(deep_config=SimpleNamespace(model=kwargs["model"], moa=kwargs["moa"]), ensure_initialized=AsyncMock())

    monkeypatch.setattr(interface_deep, "create_deep_agent", factory)
    await adapter.create_instance({"channel_id": "web", "moa_model_selection": SELECTION}, mode="agent.work.normal")
    assert captured["model"] is adapter._moa_runtime.aggregator
    assert captured["moa"] is adapter._moa_runtime.config
    ordinary_build.assert_not_called()


@pytest.mark.asyncio
async def test_model_and_group_crud_update_the_next_moa_snapshot(moa_config, monkeypatch):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.common.model_catalog import ModelCatalog

    state = deepcopy(moa_config)
    state["models"]["groups"][0]["routes"].pop()  # API saves require a valid catalog.

    def update(mutator):
        candidate = deepcopy(state)
        result = mutator(candidate)
        if result is not None:
            state.clear()
            state.update(result)
        return deepcopy(state)

    monkeypatch.setattr(config_module, "update_config", update)
    monkeypatch.setattr(config_module, "get_config_raw", lambda: deepcopy(state))
    monkeypatch.setattr(config_module, "get_config", lambda: deepcopy(state))
    monkeypatch.setattr(config_module, "get_crypto_provider", lambda: None)
    monkeypatch.setattr(ModelCatalog, "find_references", lambda *_args, **_kwargs: [])
    installed = await build_moa_runtime(state, SELECTION)
    config_module.upsert_model_resource({"model_id": "mdl_b", "model_client_config": {"api_key": "rotated-test-key"}})
    group = deepcopy(state["models"]["groups"][0])
    group["request_config"]["max_tokens"] = 777
    config_module.upsert_model_group_resource(group)
    refreshed = await build_moa_runtime(state, SELECTION)
    assert refreshed.fingerprint != installed.fingerprint
    assert installed.aggregator.model_config.max_tokens == 123
    assert refreshed.aggregator.model_config.max_tokens == 777
    assert refreshed.config.reference_models[0].model_config.temperature == 0.7
    assert installed.config.reference_models[0].model_client_config.api_key != "rotated-test-key"
    assert refreshed.config.reference_models[0].model_client_config.api_key == "rotated-test-key"
    assert config_module.delete_model_resource("model_group", "mgp_moa", cron_references_checked=True)
    assert moa_status(state, SELECTION) == {"configured": False}
    with pytest.raises(Exception, match="unknown model_group_id"):
        await build_moa_runtime(state, SELECTION)
    assert installed.aggregator.model_config.max_tokens == 123


@pytest.mark.asyncio
async def test_config_status_rpc_uses_backend_directory(monkeypatch, moa_config):
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: moa_config)
    result = await ConfigAdapter().handle(AgentRequest(
        request_id="status", channel_id="web", req_method=ReqMethod.CONFIG_MOA_STATUS, params={"model_selection": SELECTION},
    ))
    assert result.ok and result.payload["configured"] is True
    assert "private-credential" not in str(result.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_web_status_rpc_dispatches_through_e2a(monkeypatch, moa_config, remote):
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: deepcopy(moa_config))
    registry = AdapterRegistry()
    registry.register(ConfigAdapter())
    assert registry.contains(ReqMethod.CONFIG_MOA_STATUS.value)
    received = []

    class Client:
        server_ready = True

        async def send_request(self, envelope):
            request = e2a_to_agent_request(envelope)
            assert request.req_method is ReqMethod.CONFIG_MOA_STATUS
            assert request.user_id == "authenticated-user"
            assert request.session_id == "chat"
            assert request.params == {"model_selection": SELECTION}
            received.append(request)
            return await registry.get(request.req_method.value).handle(request)

    client = Client() if remote else WebSocketAgentServerClient()
    if not remote:
        client._server_ready = True
        monkeypatch.setattr(client, "send_request", Client().send_request)
    channel = SimpleNamespace(
        channel_id="web", methods={}, send_response=AsyncMock(),
        on_connect=lambda _: None, on_disconnect=lambda _: None,
    )
    channel.register_method = lambda method, callback: channel.methods.update({method: callback})
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    assert "config.moa_status" in channel.methods
    await channel.methods["config.moa_status"](object(), "status", {"model_selection": SELECTION}, "chat", "authenticated-user")
    payload = channel.send_response.await_args.kwargs["payload"]
    assert payload == {"configured": True, "aggregator_model_name": "model-a", "aggregator_alias": "Alias a"}
    assert channel.send_response.await_args.kwargs["ok"] is True
    assert len(received) == 1
    assert "private-credential" not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_client", [False, True])
async def test_web_status_rpc_requires_available_agent_server(monkeypatch, missing_client):
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient

    def unexpected_config_read():
        raise AssertionError("Gateway must not read local MoA configuration")

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", unexpected_config_read)
    client = None if missing_client else WebSocketAgentServerClient()
    send_request = AsyncMock(side_effect=AssertionError("Unavailable client must not receive requests"))
    if client is not None:
        monkeypatch.setattr(client, "send_request", send_request)
    channel = SimpleNamespace(
        channel_id="web", methods={}, send_response=AsyncMock(),
        on_connect=lambda _: None, on_disconnect=lambda _: None,
    )
    channel.register_method = lambda method, callback: channel.methods.update({method: callback})
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    ws = object()
    await channel.methods["config.moa_status"](
        ws, "status", {"model_selection": SELECTION}, "chat", "authenticated-user",
    )
    channel.send_response.assert_awaited_once_with(
        ws, "status", ok=False, error="AgentServer is unavailable", code="SERVICE_UNAVAILABLE",
    )
    send_request.assert_not_awaited()


@pytest.mark.parametrize("forbidden_kind,forbidden_id", [("model_group", "mgp_moa"), ("model", "mdl_b")])
@pytest.mark.asyncio
async def test_moa_enforces_group_and_member_permissions(moa_config, forbidden_kind, forbidden_id):
    from jiuwenswarm.common.model_errors import ModelSelectionError

    with pytest.raises(ModelSelectionError, match="forbidden"):
        await build_moa_runtime(moa_config, SELECTION, can_access=lambda kind, resource: (kind, resource) != (forbidden_kind, forbidden_id))


@pytest.mark.asyncio
async def test_first_enabled_route_is_aggregator_and_default_verifier(moa_config):
    routes = moa_config["models"]["groups"][0]["routes"]
    routes[0]["enabled"] = False
    runtime = await build_moa_runtime(moa_config, {**SELECTION, "route_id": "rte_c"})
    assert runtime.model_name == "model-b"
    assert runtime.config.scorer._model is runtime.aggregator
    assert runtime.config.reference_models[0].model_config.model_name == "model-c"


@pytest.mark.parametrize("mode,is_cron", [("team", False), ("agent.work.normal", True)])
def test_team_and_cron_never_derive_moa(moa_config, mode, is_cron):
    from jiuwenswarm.server.runtime.moa_runtime import effective_moa_enabled
    assert effective_moa_enabled(moa_config, SELECTION, mode=mode, is_cron=is_cron) is False


def test_ordinary_model_and_old_flag_never_enable_moa(moa_config):
    from jiuwenswarm.server.runtime.moa_runtime import effective_moa_enabled
    assert effective_moa_enabled(moa_config, {"type": "model", "id": "mdl_a"}, mode="agent.work.normal") is False
    moa_config["experimental"]["multi_model_collaboration_enabled"] = False
    with pytest.raises(ValueError, match="ordinary model"):
        effective_moa_enabled(moa_config, SELECTION, mode="agent.work.normal")
    assert effective_moa_enabled(moa_config, None, mode="agent.work.normal") is False


@pytest.mark.asyncio
async def test_jev_interval_and_default_change_affect_next_runtime(moa_config, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "shared-jev-secret")
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    before = await build_moa_runtime(moa_config, SELECTION)
    moa_config["experimental"]["multi_model_collaboration"].update({"jev_enabled": True, "verifier_interval": 1, "jev": {"api_key": "jev-secret"}})
    routes = moa_config["models"]["groups"][0]["routes"]
    routes[0], routes[1] = routes[1], routes[0]
    after = await build_moa_runtime(moa_config, SELECTION)
    assert before.model_name == "model-a" and before.config.verifier.interval == 3
    assert after.model_name == "model-b" and after.config.jev.interval == 1
    assert after.config.scorer is None
    assert after.config.mode == "jev_routed"
    await after.aclose()
    assert before.fingerprint != after.fingerprint


@pytest.mark.parametrize("provider,base,model,key_env,endpoint", [
    ("typesafe", "https://api.typesafe.ai/v1", "jev-1.13.0", "TYPESAFE_API_KEY", "/systemone"),
    ("openrouter", "https://openrouter.ai/api/alpha", "typesafe/jev-1.13", "OPENROUTER_API_KEY", "/decisions"),
    ("custom", "https://jev.example.test/v1", "jev-preview", "JEV_CUSTOM_API_KEY", "/systemone"),
])
@pytest.mark.asyncio
async def test_shared_jev_config_reaches_actual_client_wire_format(
    moa_config, monkeypatch, provider, base, model, key_env, endpoint,
):
    import json

    import httpx
    from openjiuwen.core.foundation.llm.system_one.schema import ChoiceQuestion

    from jiuwenswarm.agents.harness.common.browser_config import browser_decision_state
    from jiuwenswarm.server.runtime.moa_progress import ProgressJevSystemOneClient

    monkeypatch.setenv(key_env, "shared-provider-secret")
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    moa_config["browser"] = {"decision": {
        "mode": "llm", "provider": provider, "api_base": base, "model": model,
    }}
    moa_config["experimental"]["multi_model_collaboration"].update({
        "jev_enabled": True, "jev": {"api_key": "obsolete-key", "model_name": "obsolete-model"},
    })
    projected = browser_decision_state(moa_config)
    settings = resolve_moa_settings(moa_config, SELECTION)[0]["jev"]
    assert settings["api_base"] == projected["decision_api_base"]
    assert settings["model_name"] == projected["decision_model"]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "model": model,
            "answers": {"progress": {
                "type": "choice", "choice": "T", "confidence": 1.0,
                "probabilities": {chr(65 + index): float(index == 19) for index in range(20)},
            }},
            "usage": {"input_tokens": 10, "output_tokens": 1},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.moa_progress.ProgressJevSystemOneClient",
            lambda **kwargs: ProgressJevSystemOneClient(http_client=client, **kwargs),
        )
        runtime = await build_moa_runtime(moa_config, SELECTION)
        try:
            assert runtime.config.mode == "jev_routed"
            assert runtime.config.scorer is None
            response = await runtime.config.jev.client.system_one(
                [{"role": "user", "content": "Read the page"}],
                {"progress": ChoiceQuestion(
                    instructions="Assess progress",
                    criteria={"A": "Incomplete", "T": "Complete"},
                )},
            )
            assert response.answers["progress"].choice == "T"
        finally:
            await runtime.aclose()
    assert len(requests) == 1
    assert str(requests[0].url) == base + endpoint
    assert requests[0].headers["Authorization"] == "Bearer shared-provider-secret"
    assert json.loads(requests[0].content)["model"] == model
    monkeypatch.setenv(key_env, "rotated-shared-key")
    assert resolve_moa_settings(moa_config, SELECTION)[2] != runtime.fingerprint


@pytest.mark.parametrize("requested,expected", [("model-b", "model:mdl_b"), ("Alias b", "model:mdl_b"), ("login-only-model", "login-only-model"), ("zen-free-model", "zen-free-model")])
@pytest.mark.parametrize("restored", [False, True])
def test_ordinary_legacy_model_wins_over_global_default_group(moa_config, monkeypatch, requested, expected, restored):
    from jiuwenswarm.common.model_catalog import ModelCatalog
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    moa_config["models"]["groups"][0]["is_default"] = True
    catalog = ModelCatalog(moa_config)
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", lambda: catalog)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection", lambda _: None)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = "agent.work.normal"
    adapter._is_cron_execution_session = lambda _: False
    adapter._session_stored_model_name = lambda _: requested if restored else ""
    request = AgentRequest(request_id="legacy", session_id="legacy", params={} if restored else {"model_name": requested})
    assert adapter._requested_model_name(request) == expected
    primary = object()
    if expected.startswith("model:"):
        monkeypatch.setattr(adapter, "_request_scoped_login_model", lambda *_: None)
        monkeypatch.setattr(adapter, "_resolve_stable_model", lambda selected, *args: primary if selected == expected else None)
    else:
        monkeypatch.setattr(adapter, "_request_scoped_login_model", lambda _, selected: primary if selected == expected else None)
        monkeypatch.setattr(adapter, "_resolve_stable_model", lambda *_: (_ for _ in ()).throw(AssertionError("login/free must not resolve a group")))
    assert adapter._resolve_model_for_request(request) is primary


@pytest.mark.parametrize("kind", ["team", "cron"])
def test_global_default_group_retains_upstream_team_and_cron_priority(moa_config, monkeypatch, kind):
    from jiuwenswarm.common.model_catalog import ModelCatalog
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    moa_config["models"]["groups"][0]["is_default"] = True
    catalog = ModelCatalog(moa_config)
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", lambda: catalog)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection", lambda _: None)
    adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = "team" if kind == "team" else "agent.work.normal"
    adapter._is_cron_execution_session = lambda _: kind == "cron"
    request = AgentRequest(request_id="legacy", session_id="legacy", params={"model_name": "model-b"})
    assert adapter._requested_model_name(request) == "model_group:mgp_moa"


@pytest.mark.asyncio
async def test_status_and_single_member_never_construct_jev_transport(moa_config, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "shared-jev-secret")
    moa_config["experimental"]["multi_model_collaboration"].update({
        "jev_enabled": True, "jev": {"api_key": "test-key"},
    })
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    def unexpected_client(**_kwargs):
        raise AssertionError("Status and a single member must not create JEV transport")
    monkeypatch.setattr("jiuwenswarm.server.runtime.moa_progress.ProgressJevSystemOneClient", unexpected_client)
    assert moa_status(moa_config, SELECTION)["configured"] is True
    for route in moa_config["models"]["groups"][0]["routes"][1:]:
        route["enabled"] = False
    runtime = await build_moa_runtime(moa_config, SELECTION)
    assert runtime.config is None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_jev_factory_closes_client_on_configuration_failure(moa_config, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "shared-jev-secret")
    import openjiuwen.harness.moa as moa
    from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient

    moa_config["experimental"]["multi_model_collaboration"].update({
        "jev_enabled": True, "jev": {"api_key": "test-key"},
    })
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    closed = AsyncMock()
    original_close = JevSystemOneClient.aclose
    async def close(client):
        await closed()
        await original_close(client)
    monkeypatch.setattr(JevSystemOneClient, "aclose", close)
    def invalid_config(**_kwargs):
        raise ValueError("configuration failed")
    monkeypatch.setattr(moa, "MoAConfig", invalid_config)
    with pytest.raises(ValueError, match="configuration failed"):
        await build_moa_runtime(moa_config, SELECTION)
    closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_close_is_idempotent_and_settles_before_cancellation():
    entered, release = asyncio.Event(), asyncio.Event()
    async def close():
        entered.set()
        await release.wait()
    client = SimpleNamespace(aclose=AsyncMock(side_effect=close))
    runtime = MoARuntime(None, None, "fingerprint", "model", _jev_client=client)
    task = asyncio.create_task(runtime.aclose())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.aclose()
    client.aclose.assert_awaited_once()
    assert runtime._close_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ["work", "code"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_adapter_creation_failure_closes_owned_runtime(monkeypatch, adapter_kind, failure):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    adapter = JiuWenSwarmDeepAdapter() if adapter_kind == "work" else JiuwenSwarmCodeAdapter()
    runtime = SimpleNamespace(aclose=AsyncMock())
    async def fail(*_args, **_kwargs):
        adapter._moa_runtime = runtime
        raise failure()
    monkeypatch.setattr(adapter, "_create_instance", fail)
    monkeypatch.setattr(adapter, "_uses_smart_permission_lifecycle", lambda _: False)
    with pytest.raises(failure):
        await adapter.create_instance()
    runtime.aclose.assert_awaited_once()
    assert adapter._moa_runtime is None


@pytest.mark.asyncio
async def test_adapter_cleanup_closes_transport_when_other_cleanup_fails(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = JiuWenSwarmDeepAdapter()
    runtime = SimpleNamespace(aclose=AsyncMock())
    adapter._moa_runtime = runtime
    monkeypatch.setattr(adapter, "_cleanup_adapter_resources", AsyncMock(side_effect=RuntimeError("cleanup failed")))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await adapter.cleanup()
    runtime.aclose.assert_awaited_once()
    assert adapter._moa_runtime is None


@pytest.mark.asyncio
async def test_preparing_runtime_closes_old_transport_before_replacement(moa_config, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = JiuWenSwarmDeepAdapter()
    old = SimpleNamespace(aclose=AsyncMock())
    adapter._moa_runtime = old
    adapter._instance_overrides = {"moa_model_selection": SELECTION}
    adapter._session_instance_mode = "agent.work.normal"
    await adapter._prepare_moa_for_build(moa_config)
    old.aclose.assert_awaited_once()
    assert adapter._moa_runtime is not old
    await adapter._close_moa_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_session_start_failure_closes_uncached_child(moa_config, monkeypatch, failure):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    root = interface_deep.JiuWenSwarmDeepAdapter()
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child.mark_as_session_scoped("failed-start")
    runtime = MoARuntime(None, None, "fingerprint", "model", _jev_client=SimpleNamespace(aclose=AsyncMock()))
    async def create(*_args, **_kwargs):
        child._moa_runtime = runtime
    monkeypatch.setattr(root, "_new_session_scoped_adapter", lambda _: child)
    monkeypatch.setattr(root, "_load_skill_retrieval_session_profile", lambda _: None)
    monkeypatch.setattr(child, "restore_skill_retrieval_session", lambda *_: None)
    monkeypatch.setattr(child, "persist_skill_retrieval_session_profile", lambda: None)
    monkeypatch.setattr(child, "create_instance", create)
    monkeypatch.setattr(child, "start_interaction", AsyncMock(side_effect=failure()))
    monkeypatch.setattr(interface_deep, "load_persisted_external_harness_binding", lambda _: None)
    monkeypatch.setattr(interface_deep, "get_config", lambda: moa_config)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata", lambda _: {})
    with pytest.raises(failure):
        await root._get_or_create_session_adapter("failed-start", model_selection=SELECTION)
    runtime._jev_client.aclose.assert_awaited_once()
    assert "failed-start" not in root._session_adapters


@pytest.mark.asyncio
async def test_factory_cleanup_error_preserves_original_build_failure(moa_config, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "shared-jev-secret")
    import openjiuwen.harness.moa as moa
    from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient

    moa_config["experimental"]["multi_model_collaboration"].update({
        "jev_enabled": True, "jev": {"api_key": "test-key"},
    })
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    original_close = JevSystemOneClient.aclose
    async def failing_close(client):
        await original_close(client)
        raise RuntimeError("private provider failure")
    monkeypatch.setattr(JevSystemOneClient, "aclose", failing_close)
    def invalid_config(**_kwargs):
        raise ValueError("original configuration failed")
    monkeypatch.setattr(moa, "MoAConfig", invalid_config)
    with pytest.raises(ValueError, match="original configuration failed"):
        await build_moa_runtime(moa_config, SELECTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_adapter_cleanup_preserves_primary_failure_when_close_fails(monkeypatch, failure):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = JiuWenSwarmDeepAdapter()
    primary = failure("original cleanup failure")
    runtime = MoARuntime(None, None, "fingerprint", "model", _jev_client=SimpleNamespace(
        aclose=AsyncMock(side_effect=ValueError("private transport error")),
    ))
    adapter._moa_runtime = runtime
    monkeypatch.setattr(adapter, "_cleanup_adapter_resources", AsyncMock(side_effect=primary))
    with pytest.raises(failure) as caught:
        await adapter.cleanup()
    assert caught.value is primary
    assert runtime._close_task.done()
    runtime._jev_client.aclose.assert_awaited_once()
    assert adapter._moa_runtime is None


@pytest.mark.asyncio
async def test_runtime_close_preserves_cancellation_when_transport_close_fails():
    entered, release = asyncio.Event(), asyncio.Event()
    async def failing_close():
        entered.set()
        await release.wait()
        raise ValueError("private transport error")
    runtime = MoARuntime(None, None, "fingerprint", "model", _jev_client=SimpleNamespace(
        aclose=AsyncMock(side_effect=failing_close),
    ))
    task = asyncio.create_task(runtime.aclose())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._close_task.done()
    runtime._jev_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_close_propagates_transport_failure_without_primary_failure():
    runtime = MoARuntime(None, None, "fingerprint", "model", _jev_client=SimpleNamespace(
        aclose=AsyncMock(side_effect=ValueError("transport failed")),
    ))
    with pytest.raises(ValueError, match="transport failed"):
        await runtime.aclose()
    assert runtime._close_task.done()


@pytest.mark.parametrize("jev_enabled", [False, True])
@pytest.mark.asyncio
async def test_moa_rail_emits_transient_reference_and_verification_status(moa_config, monkeypatch, jev_enabled):
    from openjiuwen.core.foundation.llm import AssistantMessage, Model, UserMessage
    from openjiuwen.core.foundation.llm.system_one import ChoiceAnswer, JevSystemOneClient, SystemOneResponse, SystemOneUsage
    from openjiuwen.core.session import get_current_session, with_session
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs, ModelCallInputs
    from openjiuwen.harness.rails.moa_rail import MoARail

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
    moa_config["experimental"]["multi_model_collaboration"].update({
        "jev_enabled": jev_enabled, "verifier_interval": 1,
    })
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    monkeypatch.setattr(Model, "invoke", AsyncMock(return_value=AssistantMessage(content="<c1>D</c1>")))
    monkeypatch.setattr(JevSystemOneClient, "system_one", AsyncMock(return_value=SystemOneResponse(
        model="jev-test", usage=SystemOneUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        answers={"consult_reference": ChoiceAnswer(
            choice="CONSULT", confidence=0.9, probabilities={"CONSULT": 0.9, "CONTINUE": 0.1},
        )},
    )))
    runtime = await build_moa_runtime(moa_config, SELECTION)
    mailbox = runtime.progress.open()
    session = SimpleNamespace(get_session_id=lambda: "moa-progress", write_stream=AsyncMock())
    model = runtime.aggregator
    def replace_model(value):
        nonlocal model
        model = value
    agent = SimpleNamespace(get_llm=lambda: model, set_llm=replace_model, ability_manager=None)
    rail = MoARail(runtime.config)
    messages = [UserMessage(content="private task"), AssistantMessage(content="private answer")]
    context = AgentCallbackContext(
        agent=agent, session=session, inputs=InvokeInputs(query="private task"),
        context=SimpleNamespace(get_messages=lambda: messages),
    )

    @with_session()
    async def invoke_rail(session):
        await rail.before_invoke(context)
        try:
            context.inputs = ModelCallInputs(messages=messages, react_iteration=1)
            await rail.before_model_call(context)
            await rail.after_react_iteration(context)
        finally:
            await rail.after_invoke(context)

    previous_session = get_current_session()
    try:
        await invoke_rail(session=session)
        assert get_current_session() is previous_session
        session.write_stream.assert_not_called()
        assert {event["phase"] for event in mailbox.pending} == {"reference", "verification"}
        by_operation = {}
        for event in mailbox.pending:
            assert set(event) == {"operation_id", "phase", "status"}
            by_operation.setdefault(event["operation_id"], []).append(event["status"])
        assert len(by_operation) == 3
        assert all(statuses == ["started", "completed"] for statuses in by_operation.values())
    finally:
        await runtime.aclose()
