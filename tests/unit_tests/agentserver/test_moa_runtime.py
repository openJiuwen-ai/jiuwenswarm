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
    JevProgressScorer, build_moa_runtime, moa_status, resolve_moa_settings,
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


def test_group_parameters_and_member_overrides_survive_compilation(moa_config):
    saved = deepcopy(moa_config)
    runtime = build_moa_runtime(moa_config, SELECTION)
    assert runtime.model_name == "model-a"
    assert runtime.aggregator.model_config.temperature == 0.5
    assert runtime.aggregator.model_config.max_tokens == 123
    assert [model.model_config.model_name for model in runtime.config.reference_models] == ["model-b", "model-c"]
    assert runtime.config.reference_models[0].model_config.temperature == 0.7
    assert runtime.config.reference_models[1].model_config.temperature == 0.5
    assert runtime.config.scorer._model is runtime.aggregator
    assert runtime.config.mode == "verifier_routed"
    assert runtime.config.verifier.interval == 3
    assert moa_config == saved


@pytest.mark.parametrize("jev_enabled", [False, True])
def test_single_enabled_member_uses_its_model_without_moa(moa_config, jev_enabled, monkeypatch):
    from openjiuwen.core.single_agent import AgentCard

    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    group = moa_config["models"]["groups"][0]
    group["routes"][0]["enabled"] = False
    group["routes"][2]["enabled"] = False
    moa_config["experimental"]["multi_model_collaboration"]["jev_enabled"] = jev_enabled
    if jev_enabled:
        moa_config["experimental"]["multi_model_collaboration"]["jev"] = {"api_key": "encrypted-jev-key"}
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_crypto_provider",
            lambda: SimpleNamespace(decrypt=lambda _: "test-jev-key"),
        )

    runtime = build_moa_runtime(moa_config, SELECTION)
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


def test_status_is_safe_and_archival_roles_never_control_verification(moa_config):
    assert moa_status(moa_config, SELECTION) == {
        "configured": True, "aggregator_model_name": "model-a", "aggregator_alias": "Alias a",
    }
    moa_config["moa"]["verifier"]["route_id"] = "rte_b"
    runtime = build_moa_runtime(moa_config, SELECTION)
    assert runtime.config.scorer._model is runtime.aggregator
    assert runtime.model_name == "model-a"


def test_jev_stored_credential_is_decrypted_only_in_runtime(moa_config, monkeypatch):
    moa_config["experimental"]["multi_model_collaboration"].update({"jev_enabled": True, "jev": {"api_key": "encrypted-value"}})
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: SimpleNamespace(decrypt=lambda _: "resolved-secret"))
    runtime = build_moa_runtime(moa_config, SELECTION)
    assert runtime.config.scorer._settings["api_key"] == "resolved-secret"
    assert moa_config["experimental"]["multi_model_collaboration"]["jev"]["api_key"] == "encrypted-value"
    public = moa_status(moa_config, SELECTION)
    assert public["configured"] is True
    assert "resolved-secret" not in str(public)


@pytest.mark.parametrize("change", ["group", "aggregator", "member", "interval"])
def test_stale_configuration_fails_before_an_invocation(moa_config, change):
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
        build_moa_runtime(moa_config, SELECTION)


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


def test_unrelated_work_reload_keeps_the_installed_moa_objects(moa_config, monkeypatch):
    from openjiuwen.core.single_agent import AgentCard
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    runtime = build_moa_runtime(moa_config, SELECTION)
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
def test_moa_validation_and_resolution_ignore_ordinary_selection(moa_config, monkeypatch, ordinary, profile):
    from jiuwenswarm.common.model_errors import ModelSelectionError
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    adapter = (JiuWenSwarmDeepAdapter if profile == "work" else JiuwenSwarmCodeAdapter)()
    runtime = build_moa_runtime(moa_config, SELECTION)
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


def test_moa_build_precedes_invalid_ordinary_default_group(moa_config, monkeypatch):
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
    selected = adapter._prepare_moa_for_build(moa_config)
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


def test_model_and_group_crud_update_the_next_moa_snapshot(moa_config, monkeypatch):
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
    installed = build_moa_runtime(state, SELECTION)
    config_module.upsert_model_resource({"model_id": "mdl_b", "model_client_config": {"api_key": "rotated-test-key"}})
    group = deepcopy(state["models"]["groups"][0])
    group["request_config"]["max_tokens"] = 777
    config_module.upsert_model_group_resource(group)
    refreshed = build_moa_runtime(state, SELECTION)
    assert refreshed.fingerprint != installed.fingerprint
    assert installed.aggregator.model_config.max_tokens == 123
    assert refreshed.aggregator.model_config.max_tokens == 777
    assert refreshed.config.reference_models[0].model_config.temperature == 0.7
    assert installed.config.reference_models[0].model_client_config.api_key != "rotated-test-key"
    assert refreshed.config.reference_models[0].model_client_config.api_key == "rotated-test-key"
    assert config_module.delete_model_resource("model_group", "mgp_moa", cron_references_checked=True)
    assert moa_status(state, SELECTION) == {"configured": False}
    with pytest.raises(Exception, match="unknown model_group_id"):
        build_moa_runtime(state, SELECTION)
    assert installed.aggregator.model_config.max_tokens == 123


def _answer(probabilities):
    return SimpleNamespace(answers={"progress": SimpleNamespace(type="choice", probabilities=probabilities)})


class _JevClient:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error
        self.closed = False
        self.entered = asyncio.Event()
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def system_one(self, state, questions):
        self.calls.append((state, questions))
        self.entered.set()
        if self.error is not None:
            raise self.error
        if self.answer is None:
            await asyncio.Event().wait()
        return self.answer


def _jev_scorer(client):
    return JevProgressScorer(
        api_key="write-only", api_base="https://example.test", model_name="jev",
        client_factory=lambda **_: client,
    )


@pytest.mark.asyncio
async def test_jev_uses_full_grade_distribution_and_closes_client():
    probabilities = {chr(65 + index): 0.0 for index in range(20)}
    probabilities.update({"A": 0.25, "T": 0.75})
    client = _JevClient(answer=_answer(probabilities))
    assert await _jev_scorer(client)("Task", ("Observed result",), 3) == 0.75
    assert len(client.calls) == 3
    assert len(client.calls[0][1]["progress"].criteria) == 20
    assert "strict, skeptical" in client.calls[0][1]["progress"].instructions
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "nan", "negative", "empty", "bool", "text"])
async def test_jev_invalid_distribution_is_a_failure(invalid):
    values = {chr(65 + index): 0.05 for index in range(20)}
    if invalid == "missing":
        values.pop("A")
    elif invalid == "nan":
        values["A"] = float("nan")
    elif invalid == "negative":
        values["A"] = -0.1
    elif invalid == "bool":
        values["A"] = True
    elif invalid == "text":
        values["A"] = "0.05"
    else:
        values = dict.fromkeys(values, 0.0)
    client = _JevClient(answer=_answer(values))
    with pytest.raises(ValueError):
        await _jev_scorer(client)("Task", ("Step",), 1)
    assert client.closed


@pytest.mark.asyncio
async def test_jev_cancel_and_transport_error_settle_requests_and_close():
    client = _JevClient()
    task = asyncio.create_task(_jev_scorer(client)("Task", ("Step",), 3))
    entered = asyncio.create_task(client.entered.wait())
    try:
        done, _ = await asyncio.wait(
            {task, entered}, timeout=2, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
            pytest.fail("JEV scorer completed before cancellation")
        assert entered in done, "JEV scorer did not enter the client"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        entered.cancel()
        await asyncio.gather(task, entered, return_exceptions=True)
    assert client.closed
    failed = _JevClient(error=RuntimeError("network failure"))
    with pytest.raises(RuntimeError, match="network"):
        await _jev_scorer(failed)("Task", ("Step",), 3)
    assert failed.closed


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
def test_moa_enforces_group_and_member_permissions(moa_config, forbidden_kind, forbidden_id):
    from jiuwenswarm.common.model_errors import ModelSelectionError

    with pytest.raises(ModelSelectionError, match="forbidden"):
        build_moa_runtime(moa_config, SELECTION, can_access=lambda kind, resource: (kind, resource) != (forbidden_kind, forbidden_id))


def test_first_enabled_route_is_aggregator_and_default_verifier(moa_config):
    routes = moa_config["models"]["groups"][0]["routes"]
    routes[0]["enabled"] = False
    runtime = build_moa_runtime(moa_config, {**SELECTION, "route_id": "rte_c"})
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


def test_jev_interval_and_default_change_affect_next_runtime(moa_config, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: None)
    before = build_moa_runtime(moa_config, SELECTION)
    moa_config["experimental"]["multi_model_collaboration"].update({"jev_enabled": True, "verifier_interval": 1, "jev": {"api_key": "jev-secret"}})
    routes = moa_config["models"]["groups"][0]["routes"]
    routes[0], routes[1] = routes[1], routes[0]
    after = build_moa_runtime(moa_config, SELECTION)
    assert before.model_name == "model-a" and before.config.verifier.interval == 3
    assert after.model_name == "model-b" and after.config.verifier.interval == 1
    assert isinstance(after.config.scorer, JevProgressScorer)
    assert before.fingerprint != after.fingerprint


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
