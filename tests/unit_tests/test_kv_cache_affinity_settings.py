"""Affinity settings must commit model and policy changes at one write boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.kv_cache_affinity_config import get_affinity_status


def model(name="local", *, mode="affinity", provider="OpenAI", default=True):
    return {
        "model_id": "mdl_" + name, "is_default": default,
        "model_client_config": {
            "model_name": name, "api_base": "http://test-service/v1", "api_key": "test-key",
            "client_provider": provider,
            "extensions": {"kv_cache": {"mode": mode}, "thinking": {"mode": "auto"}},
        },
    }


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    data = {"models": {"defaults": [model()], "groups": []},
            "kv_cache_affinity_config": {"enable_kv_cache_affinity": False}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_module, "get_crypto_provider", lambda: None)
    return path


def read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_activation_preserves_connection(settings_file):
    before = read(settings_file)["models"]
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    after = read(settings_file)
    assert after["models"] == before
    assert get_affinity_status(after)["enabled"] is True


def test_incompatible_activation_does_not_write(settings_file):
    data = read(settings_file)
    data["models"]["defaults"][0] = model(mode="none")
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = settings_file.read_bytes()
    with pytest.raises(ValueError, match="Cannot enable"):
        config_module.update_kv_cache_affinity_enabled_in_config(True)
    assert settings_file.read_bytes() == before


def test_combined_model_and_activation_rejection_is_atomic(settings_file):
    before = settings_file.read_bytes()
    with pytest.raises(ValueError, match="Cannot enable"):
        config_module.update_default_models_in_config([model("cloud", mode="none")], affinity_enabled=True)
    assert settings_file.read_bytes() == before


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("invalid", [
    {"model_provider": "invalid-provider"},
    {"skill_retrieval_max_output_chars": "invalid"},
    {"moa_verifier_interval": "invalid"},
])
def test_invalid_config_set_does_not_save_affinity(settings_file, monkeypatch, enabled, invalid):
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    data = read(settings_file)
    data["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = not enabled
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(handlers, "get_config_raw", lambda: read(settings_file))
    before = settings_file.read_bytes()
    with pytest.raises(handlers.ConfigPanelBadRequest):
        handlers.apply_config_payload({"kv_cache_affinity_enabled": str(enabled).lower(), **invalid})
    assert settings_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_models", [True, False])
@pytest.mark.parametrize("invalid", [
    {"model_provider": "invalid-provider"},
    {"skill_retrieval_max_output_chars": "invalid"},
    {"moa_verifier_interval": "invalid"},
])
async def test_invalid_save_all_does_not_save_models_or_affinity(
    settings_file, monkeypatch, replace_models, invalid,
):
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    monkeypatch.setattr(handlers, "get_config_raw", lambda: read(settings_file))
    channel = SimpleNamespace(send_response=AsyncMock())
    reload_config = AsyncMock(return_value=True)
    params = {"config": {"kv_cache_affinity_enabled": "true", **invalid}}
    if replace_models:
        params["models"] = [{
            "model_name": "replacement", "api_base": "http://replacement/v1", "api_key": "test",
            "model_provider": "OpenAI", "is_default": True,
            "extensions": {"kv_cache": {"mode": "affinity"}},
        }]
    before = settings_file.read_bytes()
    await handlers.config_save_all_handler(
        channel, None, "invalid-settings", params, "session", on_config_saved=reload_config,
    )
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"
    assert settings_file.read_bytes() == before
    reload_config.assert_not_awaited()


@pytest.mark.parametrize("provider", ["OpenAI", "AscendAffinity"])
def test_unchanged_legacy_preset_can_preserve_its_declaration(settings_file, provider):
    data = read(settings_file)
    client = data["models"]["defaults"][0]["model_client_config"]
    client.update({"client_provider": provider, "vendor_key": "legacy", "plan": "custom_api"})
    data["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = True
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    config_module.upsert_model_resource({
        "model_id": "mdl_local", "alias": "Renamed",
        "model_client_config": {"extensions": client["extensions"]},
    })
    saved = read(settings_file)
    assert saved["models"]["defaults"][0]["alias"] == "Renamed"
    assert saved["models"]["defaults"][0]["model_client_config"] == client
    assert get_affinity_status(saved)["enabled"] is True


def test_revocation_preserves_other_extensions_and_closes_switch(settings_file):
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    config_module.upsert_model_resource({
        "model_id": "mdl_local",
        "model_client_config": {"extensions": {"kv_cache": {"mode": "none"}}},
    })
    data = read(settings_file)
    client = data["models"]["defaults"][0]["model_client_config"]
    assert client["extensions"]["thinking"] == {"mode": "auto"}
    assert client["extensions"]["kv_cache"]["mode"] == "none"
    assert get_affinity_status(data)["requested_enabled"] is False


def test_changing_endpoint_requires_new_declaration(settings_file):
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    config_module.upsert_model_resource({
        "model_id": "mdl_local", "model_client_config": {"api_base": "http://another-service/v1"},
    })
    assert get_affinity_status(read(settings_file))["requested_enabled"] is False
    assert read(settings_file)["models"]["defaults"][0]["model_client_config"]["extensions"]["kv_cache"]["mode"] == "none"


def test_unrelated_edit_preserves_activation(settings_file):
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    config_module.upsert_model_resource({"model_id": "mdl_local", "alias": "renamed"})
    config_module.upsert_model_resource(model("cloud", mode="none", default=False))
    assert get_affinity_status(read(settings_file))["enabled"] is True


@pytest.mark.parametrize("client_patch", [
    {"client_provider": "Anthropic"}, {"vendor_key": "cloud", "plan": "custom_api"},
])
def test_affinity_declaration_rejects_ineligible_service(settings_file, client_patch):
    before = settings_file.read_bytes()
    client = {**client_patch, "extensions": {"kv_cache": {"mode": "affinity"}}}
    with pytest.raises(ValueError, match="custom OpenAI-compatible"):
        config_module.upsert_model_resource({"model_id": "mdl_local", "model_client_config": client})
    assert settings_file.read_bytes() == before


def test_status_covers_configured_models_without_agentos_precedence(settings_file):
    data = read(settings_file)
    data["models"]["agentos"] = [model("os", mode="none", default=False)]
    status = get_affinity_status(data)
    assert status["scope"] == "configured"
    assert status["models"] == [{"name": "local", "capable": True}, {"name": "os", "capable": False}]
    assert status["can_enable"] is True
    assert "test-key" not in str(status)


@pytest.mark.asyncio
@pytest.mark.parametrize("capable", [True, False])
async def test_front_control_reader_exposes_effective_affinity_status(settings_file, monkeypatch, capable):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.control import config_service

    data = read(settings_file)
    data["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = True
    data["models"]["defaults"][0]["model_client_config"]["extensions"]["kv_cache"]["mode"] = (
        "affinity" if capable else "none"
    )
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: data)
    monkeypatch.setattr(config_service, "get_env_file", lambda: settings_file.parent / ".env")
    request = AgentRequest(request_id="affinity-read", channel_id="web", req_method=ReqMethod.CONFIG_GET, params={})
    response = await config_service.handle_config_request(request)
    assert response.ok is True
    assert response.payload["kv_cache_affinity_enabled"] == str(capable).lower()
    assert response.payload["kv_cache_affinity_can_enable"] is capable
    assert response.payload["kv_cache_affinity_status"] == get_affinity_status(data)


def test_legacy_provider_can_be_revoked(settings_file):
    data = read(settings_file)
    data["models"]["defaults"][0] = model(provider="AscendAffinity", mode="none")
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    config_module.upsert_model_resource({"model_id": "mdl_local", "model_client_config": {
        "extensions": {"kv_cache": {"mode": "none"}},
    }})
    data = read(settings_file)
    assert data["models"]["defaults"][0]["model_client_config"]["client_provider"] == "OpenAI"
    assert get_affinity_status(data)["enabled"] is False


def test_deleting_control_model_disables_affinity(settings_file):
    config_module.upsert_model_resource(model("cloud", mode="none", default=False))
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    config_module.delete_model_resource("model", "mdl_local")
    assert get_affinity_status(read(settings_file))["requested_enabled"] is False


@pytest.mark.asyncio
async def test_registered_save_rpc_rejects_without_partial_writes(settings_file, monkeypatch):
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    monkeypatch.setattr(handlers, "get_config_raw", lambda: read(settings_file))
    methods = {}
    channel = SimpleNamespace(register_method=lambda name, method: methods.update({name: method}), send_response=AsyncMock())
    handlers.register_config_set_handlers(channel)
    original = settings_file.read_bytes()
    await methods["config.save_all"](None, "request", {
        "config": {"kv_cache_affinity_enabled": "true"},
        "models": [{"model_name": "cloud", "api_base": "https://test.invalid/v1", "api_key": "test",
                    "model_provider": "OpenAI", "is_default": True}],
    }, "session")
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"
    assert settings_file.read_bytes() == original


@pytest.mark.asyncio
async def test_registered_resource_rpc_reports_auto_disable(settings_file, monkeypatch):
    from jiuwenswarm.common.config_panel import model_resource_handlers as handlers
    from jiuwenswarm.common.config_panel import models_handlers

    config_module.update_kv_cache_affinity_enabled_in_config(True)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: read(settings_file))
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.get_config", lambda: read(settings_file))
    reload_models = AsyncMock(return_value=True)
    monkeypatch.setattr(models_handlers, "_apply_models_change", reload_models)
    methods = {}
    channel = SimpleNamespace(register_method=lambda name, method: methods.update({name: method}), send_response=AsyncMock())
    handlers.register_model_resource_handlers(channel)
    await methods["models.upsert"](None, "request", {"model": {
        "model_id": "mdl_local", "model_client_config": {"extensions": {"kv_cache": {"mode": "none"}}},
    }}, "session")
    result = channel.send_response.call_args.kwargs
    assert result["ok"] is True
    assert result["payload"]["kv_cache_affinity_auto_disabled"] is True
    assert result["payload"]["kv_cache_affinity_status"]["enabled"] is False
    assert "kv_cache_affinity_enabled" in reload_models.call_args.kwargs["changed_keys"]
    assert get_affinity_status(read(settings_file))["requested_enabled"] is False


@pytest.mark.asyncio
async def test_legacy_replace_reloads_agent_policy_when_disabling(settings_file, monkeypatch):
    from jiuwenswarm.common.config_panel import models_handlers as handlers

    config_module.update_kv_cache_affinity_enabled_in_config(True)
    monkeypatch.setattr(handlers, "get_config_raw", lambda: read(settings_file))
    monkeypatch.setattr(handlers, "get_config", lambda: read(settings_file))
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    reload_models = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "_apply_models_change", reload_models)
    methods = {}
    channel = SimpleNamespace(register_method=lambda name, method: methods.update({name: method}), send_response=AsyncMock())
    handlers.register_models_handlers(channel)
    await methods["models.replace_all"](None, "request", {"models": [{
        "model_id": "mdl_local", "model_name": "local", "api_base": "http://test-service/v1",
        "api_key": "test-key", "model_provider": "OpenAI", "is_default": True,
        "extensions": {"kv_cache": {"mode": "none"}},
    }]}, "session")
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is True
    assert response["payload"]["kv_cache_affinity_auto_disabled"] is True
    keys = reload_models.call_args.kwargs["changed_keys"]
    assert handlers._models_reload_options(keys)["reload_scopes"] == ["agent_runtime", "model"]
    assert get_affinity_status(read(settings_file))["requested_enabled"] is False


@pytest.mark.parametrize("enabled,selected,expect_hint", [
    (True, "local", True), (False, "local", False), (True, "cloud", False),
])
def test_resolved_model_produces_the_expected_wire_body(enabled, selected, expect_hint):
    from agent_gateway.common.model_selection import ModelSelection
    from jiuwenswarm.common.kv_cache_affinity_config import build_application_kv_cache_affinity_config
    from openjiuwen.core.single_agent.kv_cache.kv_cache_react_model_call_hook import KVCacheModelCallHook
    from jiuwenswarm.common.model_catalog import ModelCatalog
    from jiuwenswarm.common.model_routing_registry import ModelSelectionResolver
    from jiuwenswarm.server.runtime.model_compiler_adapter import build_model_from_selection

    data = {"models": {"defaults": [model(default=False), model("cloud", mode="none")], "groups": []},
            "kv_cache_affinity_config": {"enable_kv_cache_affinity": enabled}}
    resolved = ModelSelectionResolver(ModelCatalog(data)).resolve(ModelSelection(type="model", id="mdl_" + selected))
    llm = build_model_from_selection(resolved)
    hook = KVCacheModelCallHook()
    runtime = hook.resolve_runtime(llm, build_application_kv_cache_affinity_config(data))
    kwargs = hook.build_invoke_kwargs(runtime=runtime, llm=llm, session=None,
                                     session_id="cache-session", parent_session_id="cache-parent", turn_num=1)
    body = llm._client._build_request_params(
        messages=[{"role": "user", "content": "hello"}], tools=None,
        temperature=None, top_p=None, model=selected, stop=None, max_tokens=None, stream=False, **kwargs,
    )
    assert ("agent_hint" in body) is expect_hint
    if expect_hint:
        assert body["agent_hint"]["session_id"] == "cache-session"
    else:
        assert "session_id" not in kwargs
        assert not any(key.startswith("kv_") for key in body)


def test_backup_affinity_admits_switch_and_default_changes_keep_it_on(settings_file):
    cloud = model("cloud", mode="none")
    local = model(default=False)
    data = read(settings_file)
    data["models"] = {"defaults": [cloud, local], "agentos": [model("os", mode="none", default=False)]}
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    assert get_affinity_status(read(settings_file))["enabled"] is True
    config_module.update_default_models_in_config([{**local, "is_default": True}, {**cloud, "is_default": False}])
    config_module.update_default_models_in_config([local, cloud])
    config_module.upsert_model_resource({"model_id": "mdl_cloud", "alias": "renamed cloud"})
    persisted = read(settings_file)
    config_module._normalize_config(persisted)
    assert get_affinity_status(persisted)["enabled"] is True
    assert persisted["models"]["defaults"][1]["model_client_config"]["extensions"]["kv_cache"]["mode"] == "none"


@pytest.mark.parametrize("remove", ["delete", "revoke"])
def test_only_removing_last_capable_model_disables(settings_file, remove):
    config_module.upsert_model_resource(model("second", default=False))
    config_module.upsert_model_resource(model("cloud", mode="none", default=False))
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    if remove == "delete":
        config_module.delete_model_resource("model", "mdl_local")
    else:
        config_module.upsert_model_resource({"model_id": "mdl_local", "model_client_config": {
            "extensions": {"kv_cache": {"mode": "none"}},
        }})
    assert get_affinity_status(read(settings_file))["enabled"] is True
    config_module.delete_model_resource("model", "mdl_second")
    assert get_affinity_status(read(settings_file))["requested_enabled"] is False


def test_disabling_preserves_model_capability(settings_file):
    config_module.update_kv_cache_affinity_enabled_in_config(True)
    before = read(settings_file)["models"]
    config_module.update_kv_cache_affinity_enabled_in_config(False)
    assert read(settings_file)["models"] == before
    assert get_affinity_status(read(settings_file))["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("capable", [True, False])
async def test_registered_switch_rpc_uses_latest_mixed_model_list(settings_file, monkeypatch, capable):
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    data = read(settings_file)
    data["models"] = {"defaults": [model("cloud", mode="none"), model(default=False,
        mode="affinity" if capable else "none")], "agentos": [model("os", mode="none", default=False)]}
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = settings_file.read_bytes()
    monkeypatch.setattr(handlers, "get_config_raw", lambda: read(settings_file))
    methods = {}
    channel = SimpleNamespace(register_method=lambda name, method: methods.update({name: method}), send_response=AsyncMock())
    reload_config = AsyncMock(return_value=True)
    handlers.register_config_set_handlers(channel, on_config_saved=reload_config)
    await methods["config.save_all"](None, "request", {
        "config": {"kv_cache_affinity_enabled": "true"},
    }, "session")
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is capable
    if capable:
        assert response["payload"]["kv_cache_affinity_status"]["enabled"] is True
        assert read(settings_file)["models"] == data["models"]
        assert get_affinity_status(read(settings_file))["enabled"] is True
        assert "kv_cache_affinity_enabled" in reload_config.call_args.args[0]
    else:
        assert response["code"] == "BAD_REQUEST"
        assert response["error"] == "Cannot enable KV cache affinity: No configured model declares KV cache affinity support"
        assert settings_file.read_bytes() == before
        reload_config.assert_not_awaited()
