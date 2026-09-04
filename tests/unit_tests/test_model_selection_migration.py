from __future__ import annotations

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
            "model_client_config": {"model_name": "a", "client_provider": "OpenAI", "api_base": "x", "api_key": "secret"},
            "model_config_obj": {"temperature": .7, "context_window": 100},
        }],
        "agentos": [{
            "model_id": "mdl_b",
            "model_client_config": {"model_name": "b", "client_provider": "OpenAI", "api_base": "y", "api_key": "backup"},
        }],
        "groups": [{
            "model_group_id": "mgp_a", "enabled": True, "is_default": True,
            "routes": [{"route_id": "primary", "model_id": "mdl_a"}, {"route_id": "backup", "model_id": "mdl_b"}],
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
