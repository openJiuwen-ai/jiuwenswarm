from __future__ import annotations

import sys
from types import ModuleType

import pytest

from jiuwenswarm.common.model_config_validation import validate_models_config
from jiuwenswarm.common.model_errors import MODEL_SELECTION_DISABLED, ModelSelectionError
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_selection import ModelSelection, ResolvedModelGroup
from jiuwenswarm.server.runtime.model_routing_registry import ModelExecutionContext, ModelSelectionResolver


def _config():
    return {"models": {
        "defaults": [{
            "model_id": "mdl_a", "is_default": True,
            "model_client_config": {
                "model_name": "a", "client_provider": "OpenAI", "api_base": "x", "api_key": "secret",
                "endpoint_profile": "deepseek", "custom_headers": {"X-Test": "value"},
            },
            "model_detail": {"fallback_tag": "chat", "model_description": "primary model"},
            "model_config_obj": {"temperature": .7, "context_window": 100},
        }],
        "agentos": [{
            "model_id": "mdl_b",
            "model_client_config": {"model_name": "b", "client_provider": "OpenAI", "api_base": "y", "api_key": "backup"},
        }],
        "groups": [{
            "model_group_id": "mgp_a", "enabled": True, "is_default": True,
            "routes": [
                {"route_id": "primary", "model_id": "mdl_a"},
                {"route_id": "backup", "model_id": "mdl_b", "enabled": False},
            ],
            "request_config": {"temperature": .5}, "routing": {"strategy": "ordered-failover"},
        }],
    }}


def test_catalog_is_desensitized():
    catalog = ModelCatalog(_config())
    assert "api_key" not in catalog.list_public_models()[0]
    assert catalog.get_model("mdl_a")["entry"]["model_client_config"]["api_key"] == "secret"


def test_default_group_wins_and_keeps_route_order():
    resolved = ModelSelectionResolver(ModelCatalog(_config())).resolve(None)
    assert isinstance(resolved, ResolvedModelGroup)
    assert [route.route_id for route in resolved.routes] == ["primary", "backup"]
    assert resolved.routes[0].enabled is True
    assert resolved.routes[1].enabled is False
    assert resolved.routes[0].model.endpoint_profile == "deepseek"
    assert resolved.routes[0].model.fallback_tag == "chat"
    assert resolved.routes[0].model.model_description == "primary model"


def test_explicit_disabled_group_does_not_fall_back():
    config = _config()
    config["models"]["groups"][0]["enabled"] = False
    resolver = ModelSelectionResolver(ModelCatalog(config))
    with pytest.raises(ModelSelectionError) as caught:
        resolver.resolve(ModelSelection(type="model_group", id="mgp_a"))
    assert caught.value.code == MODEL_SELECTION_DISABLED


def test_validation_rejects_complete_catalog_conflicts_and_forbidden_fields():
    models = _config()["models"]
    models["groups"].append({**models["groups"][0], "model_group_id": "mgp_b"})
    models["groups"][0]["request_config"] = {"api_key": "bad"}
    errors = validate_models_config(models)
    assert any("default model group" in error for error in errors)
    assert any("api_key" in error for error in errors)


def test_permission_is_checked_for_every_route():
    resolver = ModelSelectionResolver(ModelCatalog(_config()))
    with pytest.raises(ModelSelectionError):
        resolver.resolve(ModelSelection(type="model_group", id="mgp_a"), ModelExecutionContext(
            can_access=lambda kind, resource_id: resource_id != "mdl_b"
        ))


def test_validation_rejects_invalid_routing_contract():
    models = _config()["models"]
    group = models["groups"][0]
    group["routes"][0]["enabled"] = "yes"
    group["routing"] = {"strategy": "tag-filtered", "strategy_kwargs": {}}

    errors = validate_models_config(models)

    assert any("enabled must be a boolean" in error for error in errors)
    assert any("fallback_tag must be a non-empty string" in error for error in errors)


def test_compiler_adapter_matches_final_core_dto(monkeypatch):
    compiled = []

    class CoreModel:
        def __init__(
            self, model_id, model_name, provider, api_key="", api_base="", source="defaults",
            endpoint_profile=None, fallback_tag=None, model_description=None,
            client_options=None, request_defaults=None,
        ):
            self.model_id = model_id
            self.endpoint_profile = endpoint_profile
            self.fallback_tag = fallback_tag
            self.model_description = model_description
            self.client_options = client_options

    class CoreRoute:
        def __init__(
            self, route_id, model, enabled=True, request_overrides=None,
            tpm=None, rpm=None, timeout=None,
        ):
            self.route_id = route_id
            self.model = model
            self.enabled = enabled
            self.request_overrides = request_overrides

    class CoreGroup:
        def __init__(self, model_group_id, routes, routing=None, request_config=None):
            self.model_group_id = model_group_id
            self.routes = routes
            self.routing = routing
            self.request_config = request_config

    routing_module = ModuleType("openjiuwen.core.foundation.llm.routing")
    compiler_module = ModuleType("openjiuwen.core.foundation.llm.routing.compiler")
    schema_module = ModuleType("openjiuwen.core.foundation.llm.routing.schema")
    class FakeCompiled:
        model_client_config = "client"
        model_request_config = "request"
    compiler_module.compile_model_selection = lambda selection: compiled.append(selection) or FakeCompiled()
    schema_module.ResolvedModel = CoreModel
    schema_module.ResolvedModelGroup = CoreGroup
    schema_module.ResolvedRoute = CoreRoute
    monkeypatch.setitem(sys.modules, routing_module.__name__, routing_module)
    monkeypatch.setitem(sys.modules, compiler_module.__name__, compiler_module)
    monkeypatch.setitem(sys.modules, schema_module.__name__, schema_module)

    from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection

    resolved = ModelSelectionResolver(ModelCatalog(_config())).resolve(None)
    client_cfg, request_cfg = compile_model_selection(resolved)
    assert client_cfg == "client"
    assert request_cfg == "request"
    core_group = compiled[0]
    assert core_group.model_group_id == "mgp_a"
    assert [route.route_id for route in core_group.routes] == ["primary", "backup"]
    assert core_group.routes[0].enabled is True
    assert core_group.routes[1].enabled is False
    assert core_group.routes[0].model.endpoint_profile == "deepseek"
    assert core_group.routes[0].model.fallback_tag == "chat"
    assert core_group.routes[0].model.model_description == "primary model"
    assert core_group.routes[0].model.client_options["custom_headers"] == {"X-Test": "value"}
