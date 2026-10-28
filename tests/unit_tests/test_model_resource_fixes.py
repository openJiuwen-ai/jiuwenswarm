"""Regression coverage for the stable-ID model-pool contract."""

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_errors import ModelSelectionError
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver


@pytest.mark.asyncio
async def test_legacy_models_list_keeps_editable_key_but_hides_login_key(monkeypatch):
    from jiuwenswarm.common.auth.model_catalog import LOGIN_MODEL_SOURCE
    from jiuwenswarm.common.config_panel import models_handlers

    models = [{
        "model_id": "mdl_local",
        "model_client_config": {
            "model_name": "local", "api_base": "https://example.com/v1",
            "api_key": "test-local-key", "client_provider": "OpenAI",
        },
    }, {
        "model_id": "mdl_login", "source": LOGIN_MODEL_SOURCE,
        "model_client_config": {
            "model_name": "login", "api_base": "https://example.com/v1",
            "api_key": "test-login-key", "client_provider": "OpenAI",
        },
    }]
    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {"defaults": [], "groups": []}})
    monkeypatch.setattr(models_handlers, "get_default_models", lambda config: models)
    channel = SimpleNamespace(send_response=AsyncMock())
    await models_handlers.models_list_handler(channel, SimpleNamespace(), "r", {}, "s")
    response = channel.send_response.call_args.kwargs
    assert response["ok"]
    local, login = response["payload"]["models"]
    # Fields consumed by the unchanged frontend parser and connection-test payload.
    for row in (local, login):
        for field in ("model_name", "api_base", "api_key", "model_provider", "reasoning_level"):
            assert isinstance(row[field], str)
    assert local["api_key"] == "test-local-key"
    assert local["model_id"] == "mdl_local"
    assert local["has_api_key"]
    assert login["api_key"] == ""
    assert login["read_only"]
    assert "test-login-key" not in str(response)


@pytest.fixture
def catalog_state(monkeypatch):
    state = {"models": {
        "defaults": [{
            "model_id": "mdl_a", "alias": "A",
            "model_client_config": {"model_name": "same", "client_provider": "OpenAI", "api_key": "enc:old"},
        }, {
            "model_id": "mdl_b", "alias": "B",
            "model_client_config": {"model_name": "same", "client_provider": "OpenAI"},
        }],
        "agentos": [{"model_id": "mdl_os", "model_client_config": {"model_name": "remote"}}],
        "groups": [],
    }}

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
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.get_config", lambda: deepcopy(state))
    monkeypatch.setattr(ModelCatalog, "find_references", lambda *args, **kwargs: [])
    return state


def test_upsert_preserves_omitted_secret_and_encrypts_rotation(catalog_state, monkeypatch):
    crypto = SimpleNamespace(encrypt=lambda value: "enc:" + value, decrypt=lambda value: value.removeprefix("enc:"))
    monkeypatch.setattr(config_module, "get_crypto_provider", lambda: crypto)
    saved = config_module.upsert_model_resource({"model_id": "mdl_a", "alias": "renamed"})
    assert saved["model_client_config"]["api_key"] == "enc:old"
    saved = config_module.upsert_model_resource({"model_id": "mdl_a", "model_client_config": {"api_key": "new"}})
    assert saved["model_client_config"]["api_key"] == "enc:new"
    saved = config_module.upsert_model_resource({"model_id": "mdl_a", "model_client_config": {"api_key": "enc:new"}})
    assert saved["model_client_config"]["api_key"] == "enc:new"


@pytest.mark.parametrize("level", ["on", "off", "high", ""])
def test_reasoning_resource_round_trip_and_reset(catalog_state, level):
    import io

    import yaml
    from ruamel.yaml import YAML

    catalog_state["models"]["defaults"][0]["model_config_obj"] = {"reasoning_level": "high"}
    saved = config_module.upsert_model_resource({
        "model_id": "mdl_a", "model_config_obj": {"reasoning_level": level},
    })
    stream = io.StringIO()
    YAML().dump(saved, stream)
    loaded = yaml.safe_load(stream.getvalue())
    assert loaded["model_config_obj"]["reasoning_level"] == level
    catalog_state["models"]["defaults"][0] = loaded
    detail = ModelCatalog(catalog_state).get_public_model_detail("mdl_a")
    assert detail["model_config_obj"]["reasoning_level"] == level


@pytest.mark.parametrize("level, expected", [(True, "on"), (False, "off")])
def test_reasoning_resource_detail_recovers_legacy_yaml_booleans(catalog_state, level, expected):
    catalog_state["models"]["defaults"][0]["model_config_obj"] = {"reasoning_level": level}
    detail = ModelCatalog(catalog_state).get_public_model_detail("mdl_a")
    assert detail["model_config_obj"]["reasoning_level"] == expected
    assert catalog_state["models"]["defaults"][0]["model_config_obj"]["reasoning_level"] is level


@pytest.mark.parametrize("runtime", ["server", "gateway.embedded"])
@pytest.mark.parametrize("level, mode", [("on", "enabled"), ("off", "disabled"), (True, "enabled"), (False, "disabled")])
def test_stable_selection_translates_reasoning_before_core_compilation(catalog_state, runtime, level, mode):
    from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection

    entry = catalog_state["models"]["defaults"][0]
    entry["model_client_config"].update({"model_name": "Qwen3.8-Flash", "api_base": "https://example.com/v1"})
    entry["model_config_obj"] = {"reasoning_level": level, "temperature": 0.3}
    catalog_state["models"]["groups"] = [{
        "model_group_id": "mgp_reasoning", "enabled": True,
        "routes": [{"route_id": "rte_reasoning", "model_id": "mdl_a", "enabled": True}],
    }]
    resolver_type = import_module(f"jiuwenswarm.{runtime}.runtime.model_routing_registry").ModelSelectionResolver
    resolver = resolver_type(ModelCatalog(catalog_state))
    for selection in (
        ModelSelection(type="model", id="mdl_a"),
        ModelSelection(type="model_group", id="mgp_reasoning"),
    ):
        resolved = resolver.resolve(selection)
        model = resolved if selection.type == "model" else resolved.routes[0].model
        assert "reasoning_level" not in model.request_defaults
        assert model.request_defaults["reasoning"] == {"mode": mode}
        assert model.request_defaults["temperature"] == 0.3
        assert "reasoning_level" not in str(compile_model_selection(resolved).model_request_config.model_dump())
    assert entry["model_config_obj"]["reasoning_level"] == level


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["defaults", "agentos", "source", "model_source"])
async def test_model_get_returns_plaintext_key_only_for_user_configured_models(catalog_state, monkeypatch, source):
    from jiuwenswarm.common.auth.model_catalog import LOGIN_MODEL_SOURCE
    from jiuwenswarm.common.config_panel.model_resource_handlers import _resource_payload

    crypto = SimpleNamespace(decrypt=lambda value: value.removeprefix("enc:"))
    monkeypatch.setattr(config_module, "get_crypto_provider", lambda: crypto)
    entry = catalog_state["models"]["defaults"][0]
    entry["model_client_config"]["custom_headers"] = {"Authorization": "test-private-header"}
    if source == "agentos":
        catalog_state["models"]["agentos"].append(catalog_state["models"]["defaults"].pop(0))
    elif source != "defaults":
        entry[source] = LOGIN_MODEL_SOURCE
    before = deepcopy(catalog_state)

    payload = await _resource_payload("models.get", {"model_id": "mdl_a"})

    detail = payload["model"]
    client = detail["model_client_config"]
    if source == "defaults":
        assert client["api_key"] == "old"
        assert "model_client_config.api_key" not in detail["write_only_fields"]
    else:
        assert "api_key" not in client
        assert "model_client_config.api_key" in detail["write_only_fields"]
    assert "custom_headers" not in client
    assert "model_client_config.custom_headers" in detail["write_only_fields"]
    assert catalog_state == before


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_agentos_resource_is_read_only(catalog_state, operation):
    with pytest.raises(ModelSelectionError) as caught:
        if operation == "upsert":
            config_module.upsert_model_resource({"model_id": "mdl_os", "alias": "changed"})
        else:
            config_module.delete_model_resource("model", "mdl_os")
    assert caught.value.code == "MODEL_SELECTION_FORBIDDEN"


def test_group_save_preserves_route_identity_and_group_timeouts(catalog_state):
    group = config_module.upsert_model_group_resource({
        "display_name": "pool", "routing": {
            "strategy": "ordered-failover", "timeout_seconds": 420,
            "stream_first_chunk_timeout": 280, "stream_idle_timeout": 90,
        },
        "routes": [{"model_id": "mdl_a"}, {"model_id": "mdl_a"}],
    })
    ids = [route["route_id"] for route in group["routes"]]
    assert len(set(ids)) == 2
    group["routes"].reverse()
    saved = config_module.upsert_model_group_resource(group)
    assert [route["route_id"] for route in saved["routes"]] == ids[::-1]
    assert saved["routing"] == {
        "timeout_seconds": 420,
        "stream_first_chunk_timeout": 280,
        "stream_idle_timeout": 90,
    }
    assert len(catalog_state["models"]["groups"]) == 1


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "bad", True])
def test_group_save_rejects_invalid_timeout(catalog_state, value):
    with pytest.raises(ModelSelectionError) as caught:
        config_module.upsert_model_group_resource({
            "display_name": "pool", "routes": [{"model_id": "mdl_a"}],
            "routing": {"timeout_seconds": value},
        })
    assert caught.value.code == "MODEL_GROUP_INVALID"


def test_group_save_accepts_numeric_timeout_from_environment(catalog_state):
    saved = config_module.upsert_model_group_resource({
        "display_name": "configured", "routes": [{"model_id": "mdl_a"}],
        "routing": {"timeout_seconds": "75"},
    })
    assert saved["routing"]["timeout_seconds"] == "75"


def test_groups_keep_independent_timeout_overrides(catalog_state):
    first = config_module.upsert_model_group_resource({
        "display_name": "fast", "routes": [{"model_id": "mdl_a"}],
        "routing": {"timeout_seconds": 75},
    })
    second = config_module.upsert_model_group_resource({
        "display_name": "slow", "routes": [{"model_id": "mdl_a"}],
        "routing": {"timeout_seconds": 420},
    })
    groups = {group["model_group_id"]: group for group in catalog_state["models"]["groups"]}
    assert groups[first["model_group_id"]]["routing"]["timeout_seconds"] == 75
    assert groups[second["model_group_id"]]["routing"]["timeout_seconds"] == 420


def test_only_explicit_default_edit_normalizes_flags(catalog_state):
    for entry in catalog_state["models"]["defaults"]:
        entry["is_default"] = True
    config_module.upsert_model_resource({"model_id": "mdl_b", "alias": "renamed"})
    assert all(entry["is_default"] for entry in catalog_state["models"]["defaults"])
    config_module.upsert_model_resource({"model_id": "mdl_b", "is_default": True})
    assert [entry["is_default"] for entry in catalog_state["models"]["defaults"]] == [False, True]


def test_all_disabled_group_rejected_at_save_and_execution(catalog_state):
    group = {"model_group_id": "mgp_a", "routes": [{"route_id": "rte_a", "model_id": "mdl_a", "enabled": False}]}
    with pytest.raises(ModelSelectionError) as save:
        config_module.upsert_model_group_resource(group)
    assert save.value.code == "MODEL_GROUP_INVALID"
    catalog_state["models"]["groups"] = [group]
    with pytest.raises(ModelSelectionError) as execution:
        ModelSelectionResolver(ModelCatalog(catalog_state)).resolve(ModelSelection(type="model_group", id="mgp_a"))
    assert execution.value.code == "MODEL_GROUP_INVALID"


@pytest.mark.parametrize("fields", [{}, {"routes": None}, {"routes": []}])
def test_group_without_routes_returns_validation_error(catalog_state, fields):
    before = deepcopy(catalog_state)
    with pytest.raises(ModelSelectionError) as caught:
        config_module.upsert_model_group_resource({"display_name": "empty", **fields})
    assert caught.value.code == "MODEL_GROUP_INVALID"
    assert catalog_state == before


@pytest.mark.parametrize("content", ['{"model_selection":', "null", "[]"])
@pytest.mark.asyncio
async def test_corrupt_session_does_not_block_delete(tmp_path, monkeypatch, content):
    from jiuwenswarm.common.config_panel import model_resource_handlers as handlers

    directory = tmp_path / "broken-session"
    directory.mkdir()
    (directory / "metadata.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.get_config", lambda: {"models": {
        "defaults": [{"model_id": "mdl_a", "model_client_config": {"model_name": "a"}}],
    }})
    deleted = []
    monkeypatch.setattr(handlers, "delete_model_resource", lambda *args, **kwargs: deleted.append(args) or True)
    channel = SimpleNamespace(send_response=AsyncMock())

    await handlers.model_resource_handler(
        channel, None, "r", {"model_id": "mdl_a"}, "s", operation="models.delete",
    )

    response = channel.send_response.call_args.kwargs
    assert response["ok"] is True
    assert deleted == [("model", "mdl_a")]


def test_corrupt_cron_reference_file_returns_explicit_error(tmp_path, monkeypatch):
    path = tmp_path / "cron.json"
    path.write_text('{"jobs":', encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_cron_jobs_path", lambda: path)
    with pytest.raises(ModelSelectionError) as caught:
        ModelCatalog({"models": {}}).find_references(ModelSelection(type="model", id="mdl_a"))
    assert caught.value.code == "MODEL_RUNTIME_UNAVAILABLE"
    assert "cron" in str(caught.value)


@pytest.mark.asyncio
async def test_shared_registration_and_reload(catalog_state, monkeypatch):
    from jiuwenswarm.common.config_panel.models_handlers import register_models_handlers
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import _ConfigRpcChannel

    channel = _ConfigRpcChannel("web")
    reload_models = AsyncMock(return_value=True)
    register_models_handlers(channel, on_config_saved=reload_models)
    assert {"models.get", "models.upsert", "models.delete", "models.references", "model_groups.list",
            "model_groups.upsert", "model_groups.delete", "session.selection.set"} <= channel.methods.keys()
    await channel.methods["models.upsert"](None, "request", {"model": {"model_id": "mdl_a", "alias": "fresh"}}, "s")
    assert channel.response["ok"]
    assert "api_key" not in channel.response["payload"]["model"]["model_client_config"]
    reload_models.assert_awaited_once()
    assert reload_models.call_args.kwargs["reload_options"]["reload_scopes"] == ["model"]


@pytest.mark.asyncio
async def test_config_adapter_registers_resource_methods(catalog_state):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter

    for method, params in [(ReqMethod.MODELS_GET, {"model_id": "mdl_a"}), (ReqMethod.MODEL_GROUPS_LIST, {})]:
        response = await ConfigAdapter().handle(AgentRequest(request_id="r", channel_id="web", req_method=method, params=params))
        assert response.ok, response.payload


@pytest.mark.parametrize("runtime", ["server.runtime", "gateway.embedded.runtime"])
def test_stale_metadata_snapshot_cannot_erase_selection(tmp_path, monkeypatch, runtime):
    metadata = import_module(f"jiuwenswarm.{runtime}.session.session_metadata")
    store = import_module(f"jiuwenswarm.{runtime}.session.model_selection_store")
    from jiuwenswarm.server.runtime.session.session_info import to_session_info

    monkeypatch.setattr(metadata, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(store, "get_agent_sessions_dir", lambda: tmp_path)
    original = {"session_id": "selection-race", "title": "before"}
    metadata._write_metadata_sync("selection-race", original)
    selection = {"type": "model_group", "id": "mgp_a", "route_id": "rte_b"}
    store.set_session_model_selection("selection-race", ModelSelection.model_validate(selection))
    latest = metadata._write_metadata_sync("selection-race", {**original, "title": "after"})
    assert latest["model_selection"] == selection
    assert latest["title"] == "after"
    assert to_session_info(latest)["model_selection"] == selection


@pytest.mark.parametrize("runtime", ["server.runtime", "gateway.embedded.runtime"])
def test_model_selection_accepts_cron_session_id(tmp_path, monkeypatch, runtime):
    metadata = import_module(f"jiuwenswarm.{runtime}.session.session_metadata")
    store = import_module(f"jiuwenswarm.{runtime}.session.model_selection_store")
    monkeypatch.setattr(metadata, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(store, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "__cron___1a1231c0004_53dd13fcf709"
    selection = ModelSelection(type="model_group", id="mgp_a", route_id="rte_b")

    store.set_session_model_selection(session_id, selection)

    assert store.get_session_model_selection(session_id) == selection
    with pytest.raises(ValueError, match="invalid session_id"):
        store.set_session_model_selection("../escape", selection)


def test_gateway_resolver_rejects_all_disabled_routes(catalog_state):
    from jiuwenswarm.gateway.embedded.runtime.model_routing_registry import ModelSelectionResolver as GatewayResolver

    catalog_state["models"]["groups"] = [{
        "model_group_id": "mgp_a",
        "routes": [{"route_id": "rte_a", "model_id": "mdl_a", "enabled": False}],
    }]
    with pytest.raises(ModelSelectionError) as caught:
        GatewayResolver(ModelCatalog(catalog_state)).resolve(ModelSelection(type="model_group", id="mgp_a"))
    assert caught.value.code == "MODEL_GROUP_INVALID"


def test_hot_edit_recompiles_same_selection(catalog_state, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    compiled = []

    def build(resolved):
        model = object()
        compiled.append((resolved, model))
        return model

    monkeypatch.setattr("jiuwenswarm.server.runtime.model_compiler_adapter.build_model_from_selection", build)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    first = adapter._resolve_stable_model("model:mdl_a")
    assert adapter._resolve_stable_model("model:mdl_a") is first
    catalog_state["models"]["defaults"][0]["model_client_config"]["api_base"] = "changed"
    assert adapter._resolve_stable_model("model:mdl_a") is not first
    assert len(compiled) == 2


@pytest.mark.asyncio
async def test_reference_check_fails_closed_on_store_error(catalog_state, monkeypatch):
    from jiuwenswarm.common.config_panel.model_resource_handlers import find_resource_references

    store = SimpleNamespace(list_jobs=AsyncMock(side_effect=RuntimeError("etcd unavailable")), aclose=AsyncMock())
    monkeypatch.setattr("jiuwenswarm.runtime.cron.factory.create_gateway_cron_store", AsyncMock(return_value=store))
    with pytest.raises(RuntimeError, match="etcd unavailable"):
        await find_resource_references(ModelCatalog(catalog_state), ModelSelection(type="model", id="mdl_a"))
    store.aclose.assert_awaited_once()


def test_legacy_replace_removes_referenced_model(catalog_state, monkeypatch):
    from jiuwenswarm.common.model_catalog import SelectionReference

    monkeypatch.setattr(ModelCatalog, "find_references", lambda *args, **kwargs: [SelectionReference("session", "s")])
    config_module.update_default_models_in_config([catalog_state["models"]["defaults"][1]])
    assert [entry["model_id"] for entry in catalog_state["models"]["defaults"]] == ["mdl_b"]


def test_resource_delete_ignores_session_references(catalog_state, monkeypatch):
    monkeypatch.setattr(ModelCatalog, "find_references", lambda *args, **kwargs: pytest.fail("reference scan ran"))
    assert config_module.delete_model_resource("model", "mdl_a")
    assert [entry["model_id"] for entry in catalog_state["models"]["defaults"]] == ["mdl_b"]


def test_delete_model_prunes_group_routes_instead_of_failing(catalog_state):
    catalog_state["models"]["groups"] = [{
        "model_group_id": "mgp_a",
        "display_name": "Coding Pool",
        "routes": [
            {"route_id": "rte_a", "model_id": "mdl_a"},
            {"route_id": "rte_b", "model_id": "mdl_b"},
        ],
    }, {
        "model_group_id": "mgp_only_a",
        "display_name": "Solo",
        "routes": [{"route_id": "rte_c", "model_id": "mdl_a"}],
    }]
    assert config_module.delete_model_resource("model", "mdl_a")
    assert [entry["model_id"] for entry in catalog_state["models"]["defaults"]] == ["mdl_b"]
    assert catalog_state["models"]["groups"] == [{
        "model_group_id": "mgp_a",
        "display_name": "Coding Pool",
        "routes": [{"route_id": "rte_b", "model_id": "mdl_b"}],
    }]


def test_deleted_session_selection_uses_current_default(catalog_state, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    catalog_state["models"]["defaults"] = [catalog_state["models"]["defaults"][1]]
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._session_instance_agent_definition = None
    monkeypatch.setattr(adapter, "_session_stored_model_name", lambda _sid: "")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection",
        lambda _sid: ModelSelection(type="model", id="mdl_a"),
    )
    request = AgentRequest(request_id="r", session_id="s", params={})
    assert adapter._requested_model_name(request) == "model:mdl_b"


@pytest.mark.parametrize("cron", [True, False])
def test_deleted_explicit_selection_only_falls_back_for_cron(catalog_state, cron):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_cron_execution = cron
    request = AgentRequest(request_id="r", session_id="", params={
        "model_selection": {"type": "model", "id": "deleted"},
    })
    assert adapter._requested_model_name(request) == ("model:mdl_a" if cron else "model:deleted")


def test_deleted_session_route_uses_current_default(catalog_state, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    catalog_state["models"]["groups"] = [{
        "model_group_id": "mgp_a", "routes": [{"route_id": "current", "model_id": "mdl_a"}],
        "is_default": True,
    }]
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    monkeypatch.setattr(adapter, "_session_stored_model_name", lambda _sid: "")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection",
        lambda _sid: ModelSelection(type="model_group", id="mgp_a", route_id="deleted"),
    )
    request = AgentRequest(request_id="r", session_id="s", params={})
    assert adapter._requested_model_name(request) == "model_group:mgp_a"


def test_session_then_spec_then_default_group_priority(catalog_state, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._session_instance_agent_definition = {"model_selection": {"type": "model", "id": "mdl_b"}}
    monkeypatch.setattr(adapter, "_session_stored_model_name", lambda sid: "")
    request = AgentRequest(request_id="r", session_id="s", params={"model_name": "same"})
    getter = "jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection"
    monkeypatch.setattr(getter, lambda sid: ModelSelection(type="model", id="mdl_a"))
    assert adapter._requested_model_name(request) == "model:mdl_a"
    monkeypatch.setattr(getter, lambda sid: None)
    assert adapter._requested_model_name(request) == "model:mdl_b"
    adapter._session_instance_agent_definition = None
    with pytest.raises(ModelSelectionError, match="ambiguous"):
        adapter._requested_model_name(request)
    catalog_state["models"]["groups"] = [{"model_group_id": "mgp_a", "is_default": True}]
    assert adapter._requested_model_name(request) == "model_group:mgp_a"
    request.params["model_selection"] = {"type": "model", "id": "mdl_b"}
    assert adapter._requested_model_name(request) == "model:mdl_b"


@pytest.mark.asyncio
async def test_cron_migration_scopes_user_and_uses_store(catalog_state):
    from jiuwenswarm.common.model_migration import migrate_legacy_cron_selections

    jobs = [
        SimpleNamespace(id="own", user_id="u1", model_selection=None, model_name="A"),
        SimpleNamespace(id="other", user_id="u2", model_selection=None, model_name="A"),
        SimpleNamespace(id="ambiguous", user_id="u1", model_selection=None, model_name="same"),
        SimpleNamespace(id="done", user_id="u1", model_selection={"type": "model", "id": "mdl_b"}, model_name="A"),
    ]
    store = SimpleNamespace(list_jobs=AsyncMock(return_value=jobs), update_job=AsyncMock())
    assert await migrate_legacy_cron_selections(store, catalog=ModelCatalog(catalog_state), user_id="u1") == 1
    store.update_job.assert_awaited_once_with("own", {
        "model_selection": {"type": "model", "id": "mdl_a"}, "_expected_legacy_model_name": "A",
    })


def test_migration_does_not_overwrite_newer_selection(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata as metadata

    monkeypatch.setattr(metadata, "get_agent_sessions_dir", lambda: tmp_path)
    selection = {"type": "model", "id": "mdl_b"}
    metadata._write_metadata_sync("migration-race", {"session_id": "migration-race"})
    metadata.update_session_model_selection("migration-race", selection)
    metadata.update_session_model_selection(
        "migration-race", {"type": "model", "id": "mdl_a"}, expected_selection=(False, None),
    )
    import json
    assert json.loads((tmp_path / "migration-race" / "metadata.json").read_text())["model_selection"] == selection


@pytest.mark.asyncio
async def test_strict_file_store_rejects_corruption(tmp_path):
    from jiuwenswarm.runtime.cron.store import FileCronJobStore

    path = tmp_path / "cron.json"
    path.write_text("broken")
    store = FileCronJobStore(path)
    assert await store.list_jobs() == []
    with pytest.raises(ValueError):
        await store.list_jobs(strict=True)
