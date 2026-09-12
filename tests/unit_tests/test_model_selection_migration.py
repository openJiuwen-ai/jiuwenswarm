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


def test_compiler_adapter_matches_final_core_dto():
    """真实联调 openjiuwen-core 的 compile_model_selection（无 mock）。

    agent-core 可用时，adapter 必须把 jiuwenswarm 的 ResolvedSelection 透传
    给 core 编译，并正确解包 CompiledModelSelection 为
    (model_client_config, model_request_config)。当 agent-core 不可用时
    （ImportError），应抛出 MODEL_RUNTIME_UNAVAILABLE。
    """
    from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection

    resolved = ModelSelectionResolver(ModelCatalog(_config())).resolve(None)

    try:
        client_cfg, request_cfg = compile_model_selection(resolved)
    except ModelSelectionError as exc:
        # agent-core 未安装（ImportError 兜底）——只允许这一种失败
        assert exc.code == "MODEL_RUNTIME_UNAVAILABLE"
        pytest.skip("agent-core compiler unavailable; skipping real integration")
        return

    # 真实编译成功：client_cfg 是 ModelClientConfig，且组路由进入 intelli_router
    assert getattr(client_cfg, "client_provider", None) == "intelli_router"
    router = getattr(client_cfg, "intelli_router", None)
    assert router is not None
    assert router.model_group_id == "mgp_a"
    assert [route.route_id for route in router.deployments] == ["primary"]
    # 第 2 条路由 enabled=False，被 core 过滤，只留下 primary
    assert router.deployments[0].model_id == "mdl_a"
    assert router.deployments[0].provider == "deepseek"
    assert router.deployments[0].model_name == "a"
    assert request_cfg is not None
